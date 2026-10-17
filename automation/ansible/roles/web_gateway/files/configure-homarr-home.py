#!/usr/bin/env python3
"""Создать виджет приложений на обычной закрытой доске Homarr 2.5.2.

Не изменять пользовательские приложения, доски, названия или раскладку.
Добавить только отсутствующий управляемый виджет и право чтения для everyone.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

WIDGET_ID = "web_gateway_140_home_apps"
ITEM_ID = WIDGET_ID + "_item"
REQUIRED_TABLES = {
    "group": {"id", "name", "home_board_id"},
    "groupPermission": {"group_id", "permission"},
    "board": {"id", "name", "is_public"},
    "boardGroupPermission": {"board_id", "group_id", "permission"},
    "boardUserPermission": {"board_id"},
    "layout": {"id", "board_id", "column_count", "role"},
    "section": {"id", "board_id", "kind", "x_offset"},
    "item": {"id", "board_id", "kind", "options", "advanced_options"},
    "item_layout": {"item_id", "section_id", "layout_id", "x_offset", "y_offset", "width", "height"},
    "custom_widget_v2_definition": {
        "id", "name", "description", "sources", "requests", "options", "template",
    },
    "onboarding": {"step"},
}


class HomeError(Exception):
    """Некорректное состояние Homarr без раскрытия содержимого его базы."""


def encode(value: object) -> str:
    return json.dumps({"json": value}, ensure_ascii=False, separators=(",", ":"))


def load_widget(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise HomeError("Не найдено описание домашних приложений")
    data = json.loads(path.read_text(encoding="utf-8"))
    if (
        data.get("$schema") != "homarr-custom-widget-v2"
        or data.get("sources") != {
            "default": {
                "name": "Домашние приложения",
                "baseUrl": "http://caddy:8081",
                "networkScope": "private",
                "auth": "none",
            },
        }
        or set(data.get("requests", {})) != {"apps"}
        or data["requests"]["apps"].get("path") != "/apps.json"
        or not isinstance(data.get("template"), str) or not data["template"]
        or data.get("options") != {}
    ):
        raise HomeError("Описание домашнего виджета не соответствует Homarr 2.5.2")
    return data


def validate(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise HomeError("Проверка связей Homarr отключена")
    for name, needed in REQUIRED_TABLES.items():
        columns = {row[1] for row in db.execute('PRAGMA table_info("' + name + '")')}
        if not needed <= columns:
            raise HomeError("Неожиданная структура Homarr 2.5.2: " + name)
    step = db.execute("SELECT step FROM onboarding ORDER BY rowid LIMIT 1").fetchone()
    if step is None or step[0] != "finish":
        raise HomeError("Первоначальная настройка Homarr ещё не завершена")


def prepare(db_path: Path, widget_path: Path, state: Path) -> str:
    if state.is_symlink() or not os.path.ismount(state):
        raise HomeError("Постоянный том гостя 140 не подключён")
    if db_path.is_symlink() or not db_path.is_file():
        raise HomeError("Постоянная база Homarr отсутствует")
    widget = load_widget(widget_path)
    with sqlite3.connect("file:" + str(db_path) + "?mode=rw", uri=True, timeout=20) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        validate(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            members = db.execute(
                'SELECT id,home_board_id FROM "group" WHERE name=?', ("everyone",)
            ).fetchall()
            if len(members) != 1 or not members[0][1]:
                raise HomeError("Нет общей домашней доски для пользователей Homarr")
            group_id, board_id = members[0]
            board = db.execute("SELECT is_public FROM board WHERE id=?", (board_id,)).fetchone()
            if board is None or board[0] != 0:
                raise HomeError("Домашняя доска должна оставаться закрытой для неавторизованных")
            rights = {row[0] for row in db.execute(
                "SELECT permission FROM boardGroupPermission WHERE board_id=? AND group_id=?",
                (board_id, group_id),
            )}
            if rights - {"view"}:
                raise HomeError("Общая группа не должна изменять домашнюю страницу")
            if not rights:
                db.execute(
                    "INSERT INTO boardGroupPermission (board_id,group_id,permission) VALUES (?,?,?)",
                    (board_id, group_id, "view"),
                )

            definition = db.execute(
                "SELECT id FROM custom_widget_v2_definition WHERE id=?", (WIDGET_ID,)
            ).fetchone()
            existing = db.execute("SELECT board_id,kind FROM item WHERE id=?", (ITEM_ID,)).fetchone()
            if definition or existing:
                if not definition or not existing or existing[1] != "customApi":
                    raise HomeError("Домашний виджет Homarr частично повреждён")
                # Администратор мог переместить плитку или переименовать виджет.
                # Повторный deploy не восстанавливает старое расположение.
                db.commit()
                return "Homarr: домашняя страница существует, пользовательские плитки сохранены"

            section = db.execute(
                "SELECT id FROM section WHERE board_id=? AND kind='empty' AND x_offset=0 "
                "ORDER BY rowid LIMIT 1", (board_id,),
            ).fetchone()
            if section is None:
                section_id = WIDGET_ID + "_section"
                db.execute(
                    "INSERT INTO section (id,board_id,kind,x_offset,y_offset) VALUES (?,?,?,0,0)",
                    (section_id, board_id, "empty"),
                )
            else:
                section_id = section[0]
            layouts = db.execute(
                "SELECT id,role,column_count FROM layout WHERE board_id=? "
                "AND role IN ('base','mobile')", (board_id,),
            ).fetchall()
            if not {"base", "mobile"} <= {row[1] for row in layouts}:
                raise HomeError("Не найдены оба штатных размера домашней доски Homarr")
            db.execute(
                "INSERT INTO custom_widget_v2_definition "
                "(id,name,description,sources,requests,options,template,enabled,creator_id) "
                "VALUES (?,?,?,?,?,?,?,1,NULL)",
                (WIDGET_ID, widget["name"], widget.get("description"),
                 encode(widget["sources"]), encode(widget["requests"]),
                 encode(widget["options"]), widget["template"]),
            )
            db.execute(
                "INSERT INTO item (id,board_id,kind,options,advanced_options) "
                "VALUES (?,?,?,?,?)",
                (ITEM_ID, board_id, "customApi",
                 encode({"definitionId": WIDGET_ID, "refreshInterval": 30,
                         "configuration": {}, "configurationVersion": 1}), encode({})),
            )
            for layout_id, role, column_count in layouts:
                max_y = db.execute(
                    "SELECT COALESCE(MAX(y_offset+height),0) FROM item_layout "
                    "WHERE section_id=? AND layout_id=?",
                    (section_id, layout_id),
                ).fetchone()[0]
                db.execute(
                    "INSERT INTO item_layout "
                    "(item_id,section_id,layout_id,x_offset,y_offset,width,height) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (ITEM_ID, section_id, layout_id, 0, max_y,
                     min(column_count, 10 if role == "base" else 3), 10),
                )
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise HomeError("Не удалось сохранить раскладку домашней страницы")
            db.commit()
            return "Homarr: обычная страница приложений настроена автоматически"
        except Exception:
            db.rollback()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--widget", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/web-gateway"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Для настройки Homarr нужны права администратора 140")
    try:
        print(prepare(args.db, args.widget, args.state))
        return 0
    except (HomeError, OSError, ValueError, sqlite3.DatabaseError) as exc:
        print("140: домашняя страница не настроена: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
