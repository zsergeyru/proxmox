#!/usr/bin/env python3
"""Однократное создание закрытой административной доски Homarr v2.5.2.

Версия Homarr закреплена. Операция работает только на подключённом mp0,
проверяет таблицы и полномочия группы admins, создаёт новые записи одной
транзакцией и не меняет ранее созданные пользовательские доски.
Штатный интерфейс Homarr не позволяет создавать виджеты до появления
первого вошедшего администратора; поэтому это ограниченная локальная
операция первоначальной установки, НЕ общий редактор базы Homarr.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

BOARD_ID = "web_gateway_140_administration"
BOARD_NAME = "Администрирование"
WIDGETS = (
    ("authelia-users.json", "web_gateway_140_users"),
    ("caddy-routes.json", "web_gateway_140_routes"),
    ("proxmox-guests.json", "web_gateway_140_guests"),
)
SCHEMA = {
    "board": {"id", "name", "is_public"},
    "group": {"id", "name"},
    "groupPermission": {"group_id", "permission"},
    "boardGroupPermission": {"board_id", "group_id", "permission"},
    "boardUserPermission": {"board_id"},
    "layout": {"id", "name", "board_id", "column_count", "breakpoint", "role"},
    "section": {"id", "board_id", "kind"},
    "item": {"id", "board_id", "kind", "options", "advanced_options"},
    "item_layout": {"item_id", "section_id", "layout_id", "x_offset", "y_offset", "width", "height"},
    "custom_widget_v2_definition": {
        "id", "name", "description", "sources", "requests", "options", "template",
    },
    "custom_widget_v2_secret": {
        "definition_id", "source_id", "kind", "encrypted_value", "updated_at",
    },
}


class SetupError(Exception):
    """Ожидаемый отказ без вывода паролей и ключей."""


def require_mount(root: Path) -> None:
    if root.is_symlink() or not os.path.ismount(root):
        raise SetupError("Постоянный том гостя 140 не подключён")


def read_secret(path: Path, *, hex_key: bool = False) -> str:
    if path.is_symlink() or not path.is_file() or (path.stat().st_mode & 0o077):
        raise SetupError("Закрытый файл отсутствует или права доступа слишком открыты")
    value = path.read_text(encoding="utf-8").strip()
    if hex_key:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            raise SetupError("Неверный ключ шифрования Homarr")
    elif len(value) < 40:
        raise SetupError("Недействительный ключ управления")
    return value


def read_homarr_key(env: Path) -> str:
    if env.is_symlink() or not env.is_file() or (env.stat().st_mode & 0o077):
        raise SetupError("Закрытые настройки Homarr отсутствуют")
    matches = [line.partition("=")[2].strip() for line in
               env.read_text(encoding="utf-8").splitlines()
               if line.startswith("SECRET_ENCRYPTION_KEY=")]
    if len(matches) != 1 or not re.fullmatch(r"[0-9a-fA-F]{64}", matches[0]):
        raise SetupError("Нет корректного единственного ключа шифрования Homarr")
    return matches[0]


def encrypt_bearer(token: str, key_hex: str) -> str:
    # Совместимо с packages/common/src/encryption.ts Homarr 2.5.2:
    # AES-256-CBC + PKCS#7, ciphertext.hex + '.' + iv.hex.
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    padded = padder.update(token.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(bytes.fromhex(key_hex)), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return ciphertext.hex() + "." + iv.hex()


def encode(value: object) -> str:
    # Формат SuperJSON.stringify без метаданных для простых значений JSON.
    return json.dumps({"json": value}, ensure_ascii=False, separators=(",", ":"))


def load_definitions(directory: Path) -> list[tuple[str, dict]]:
    result = []
    for filename, definition_id in WIDGETS:
        path = directory / filename
        if path.is_symlink() or not path.is_file():
            raise SetupError("Нет определения виджета " + filename)
        try:
            widget = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SetupError("Повреждено описание виджета " + filename) from exc
        if (
            widget.get("$schema") != "homarr-custom-widget-v2"
            or set(widget.get("sources", {})) != {"default"}
            or widget["sources"]["default"].get("baseUrl") != "http://caddy:8081"
            or widget["sources"]["default"].get("networkScope") != "private"
            or not isinstance(widget.get("template"), str) or not widget["template"]
            or not isinstance(widget.get("requests"), dict)
            or not isinstance(widget.get("options"), dict)
        ):
            raise SetupError("Неожиданное описание виджета " + filename)
        expected_auth = "none" if filename == "proxmox-guests.json" else "bearer"
        if widget["sources"]["default"].get("auth") != expected_auth:
            raise SetupError("Неверный режим доступа виджета " + filename)
        result.append((definition_id, widget))
    return result


def verify_schema(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        raise SetupError("Связи постоянной базы Homarr не проверяются")
    for name, required in SCHEMA.items():
        columns = {row[1] for row in db.execute('PRAGMA table_info("' + name + '")')}
        if not required <= columns:
            raise SetupError("Структура Homarr не соответствует закреплённой версии 2.5.2: " + name)
    row = db.execute('SELECT step FROM onboarding ORDER BY rowid LIMIT 1').fetchone()
    if row is None or row[0] != "finish":
        raise SetupError("Первоначальная настройка Homarr ещё не завершена")


def verify_private_board(db: sqlite3.Connection, group_id: str) -> None:
    board = db.execute(
        'SELECT is_public FROM board WHERE id=?', (BOARD_ID,)
    ).fetchone()
    if board is None or board[0] != 0:
        raise SetupError("Административная доска отсутствует или стала открытой")
    permissions = set(db.execute(
        'SELECT group_id, permission FROM boardGroupPermission WHERE board_id=?',
        (BOARD_ID,),
    ).fetchall())
    other_user_access = db.execute(
        'SELECT COUNT(*) FROM boardUserPermission WHERE board_id=?', (BOARD_ID,)
    ).fetchone()[0]
    if permissions != {(group_id, "full")} or other_user_access:
        raise SetupError("Доступ к административной доске отличается от admins")


def initialize(db_path: Path, widgets_dir: Path, token_file: Path,
               env_file: Path, *, persistent_root: Path) -> str:
    require_mount(persistent_root)
    if db_path.is_symlink() or not db_path.is_file():
        raise SetupError("Не найдена постоянная база Homarr")
    definitions = load_definitions(widgets_dir)
    token = read_secret(token_file)
    homarr_key = read_homarr_key(env_file)
    with sqlite3.connect("file:" + str(db_path) + "?mode=rw", uri=True, timeout=20) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=20000")
        verify_schema(db)
        db.execute("BEGIN IMMEDIATE")
        try:
            admins = db.execute(
                'SELECT g.id FROM "group" g JOIN groupPermission p ON p.group_id=g.id '
                'WHERE g.name=? AND p.permission=?',
                ("admins", "admin"),
            ).fetchall()
            if len(admins) != 1:
                raise SetupError("Группа admins не имеет административных прав в Homarr")
            admin_group_id = admins[0][0]
            old = db.execute("SELECT id FROM board WHERE name=? OR id=?", (BOARD_NAME, BOARD_ID)).fetchall()
            if old:
                if len(old) != 1 or old[0][0] != BOARD_ID:
                    raise SetupError("Имя доски занято другими данными")
                verify_private_board(db, admin_group_id)
                return "Административная страница Homarr уже создана; настройки сохранены"

            # Права, определения и размещение создаются одной транзакцией.
            db.execute(
                "INSERT INTO board (id,name,is_public,creator_id) VALUES (?,?,0,NULL)",
                (BOARD_ID, BOARD_NAME),
            )
            db.execute(
                "INSERT INTO boardGroupPermission (board_id,group_id,permission) VALUES (?,?,?)",
                (BOARD_ID, admin_group_id, "full"),
            )
            base_id = BOARD_ID + "_base"
            mobile_id = BOARD_ID + "_mobile"
            section_id = BOARD_ID + "_root"
            for layout_id, title, columns, breakpoint, role in (
                (mobile_id, "Mobile", 3, 0, "mobile"),
                (base_id, "Base", 10, 768, "base"),
            ):
                db.execute(
                    "INSERT INTO layout (id,name,board_id,column_count,breakpoint,role) "
                    "VALUES (?,?,?,?,?,?)",
                    (layout_id, title, BOARD_ID, columns, breakpoint, role),
                )
            db.execute(
                "INSERT INTO section (id,board_id,kind,x_offset,y_offset) VALUES (?,?,?,0,0)",
                (section_id, BOARD_ID, "empty"),
            )
            for i, (definition_id, widget) in enumerate(definitions):
                item_id = definition_id + "_item"
                db.execute(
                    "INSERT INTO custom_widget_v2_definition "
                    "(id,name,description,sources,requests,options,template,enabled,creator_id) "
                    "VALUES (?,?,?,?,?,?,?,1,NULL)",
                    (
                        definition_id, widget["name"], widget.get("description"),
                        encode(widget["sources"]), encode(widget["requests"]),
                        encode(widget["options"]), widget["template"],
                    ),
                )
                if widget["sources"]["default"]["auth"] == "bearer":
                    db.execute(
                        "INSERT INTO custom_widget_v2_secret "
                        "(definition_id,source_id,kind,encrypted_value,updated_at) "
                        "VALUES (?,?,?,?,unixepoch())",
                        (definition_id, "default", "apiKey", encrypt_bearer(token, homarr_key)),
                    )
                db.execute(
                    "INSERT INTO item (id,board_id,kind,options,advanced_options) "
                    "VALUES (?,?,?,?,?)",
                    (
                        item_id, BOARD_ID, "customApi",
                        encode({"definitionId": definition_id, "refreshInterval": 30,
                                "configuration": {}, "configurationVersion": 1}),
                        encode({}),
                    ),
                )
                for layout_id, width, x, y, height in (
                    (base_id, 3, i * 3, 0, 9),
                    (mobile_id, 3, 0, i * 9, 9),
                ):
                    db.execute(
                        "INSERT INTO item_layout "
                        "(item_id,section_id,layout_id,x_offset,y_offset,width,height) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (item_id, section_id, layout_id, x, y, width, height),
                    )
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise SetupError("Ошибка ссылок при создании административной страницы")
            db.commit()
            return "Homarr: закрытая страница и три виджета созданы для admins"
        except Exception:
            db.rollback()
            raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--widgets", type=Path, required=True)
    parser.add_argument("--token", type=Path, required=True)
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/web-gateway"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("доступно только администратору гостя 140")
    try:
        print(initialize(args.db, args.widgets, args.token, args.env,
                         persistent_root=args.state))
        return 0
    except (SetupError, OSError, sqlite3.DatabaseError, ValueError) as exc:
        print("140: настройка Homarr не завершена: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
