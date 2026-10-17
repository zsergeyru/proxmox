#!/usr/bin/env python3
"""Проверить обычную страницу Homarr и обновление ссылок без пользовательских потерь."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "automation/ansible/roles/web_gateway/files"
GUEST = ROOT / "infrastructure/guests/140-web-gateway"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPTS / name)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def rejection(fn, *errors):
    try:
        fn()
    except errors:
        return
    raise AssertionError("Операция должна быть запрещена")


def database(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE "group" (id TEXT PRIMARY KEY,name TEXT,home_board_id TEXT);
        CREATE TABLE groupPermission(group_id TEXT,permission TEXT);
        CREATE TABLE onboarding(step TEXT);
        CREATE TABLE board(id TEXT PRIMARY KEY,name TEXT,is_public INTEGER,creator_id TEXT);
        CREATE TABLE boardGroupPermission(board_id TEXT,group_id TEXT,permission TEXT,
            FOREIGN KEY(board_id) REFERENCES board(id));
        CREATE TABLE boardUserPermission(board_id TEXT,user_id TEXT,permission TEXT);
        CREATE TABLE layout(id TEXT PRIMARY KEY,name TEXT,board_id TEXT,
            column_count INTEGER,breakpoint INTEGER,role TEXT);
        CREATE TABLE section(id TEXT PRIMARY KEY,board_id TEXT,kind TEXT,
            x_offset INTEGER,y_offset INTEGER);
        CREATE TABLE item(id TEXT PRIMARY KEY,board_id TEXT,kind TEXT,options TEXT,advanced_options TEXT);
        CREATE TABLE item_layout(item_id TEXT,section_id TEXT,layout_id TEXT,
            x_offset INTEGER,y_offset INTEGER,width INTEGER,height INTEGER,
            FOREIGN KEY(item_id) REFERENCES item(id),
            FOREIGN KEY(section_id) REFERENCES section(id),
            FOREIGN KEY(layout_id) REFERENCES layout(id));
        CREATE TABLE custom_widget_v2_definition(id TEXT PRIMARY KEY,name TEXT,
            description TEXT,sources TEXT,requests TEXT,options TEXT,template TEXT,
            enabled INTEGER,creator_id TEXT);
        INSERT INTO onboarding VALUES ('finish');
        INSERT INTO board VALUES ('home','dashboard',0,NULL);
        INSERT INTO "group" VALUES ('everyone-id','everyone','home');
        INSERT INTO "group" VALUES ('admins-id','admins',NULL);
        INSERT INTO groupPermission VALUES ('admins-id','admin');
        INSERT INTO layout VALUES ('mobile','Mobile','home',3,0,'mobile');
        INSERT INTO layout VALUES ('base','Base','home',10,768,'base');
        INSERT INTO section VALUES ('root','home','empty',0,0);
        INSERT INTO item VALUES ('personal','home','app','{"json":{"title":"Личное"}}','{"json":{}}');
        INSERT INTO item_layout VALUES ('personal','root','base',0,0,2,2);
        INSERT INTO item_layout VALUES ('personal','root','mobile',0,0,3,2);
        """)


def test_page() -> None:
    module = load("configure-homarr-home.py")
    widget_path = GUEST / "widgets/home-apps.json"
    with TemporaryDirectory() as tmp:
        state = Path(tmp)
        db_path = state / "db.sqlite"
        database(db_path)
        with patch.object(module.os.path, "ismount", return_value=False):
            rejection(lambda: module.prepare(db_path, widget_path, state), module.HomeError)
        with patch.object(module.os.path, "ismount", return_value=True):
            assert "автоматически" in module.prepare(db_path, widget_path, state)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 2
                assert db.execute("SELECT COUNT(*) FROM item_layout").fetchone()[0] == 4
                assert db.execute("SELECT permission FROM boardGroupPermission").fetchone() == ("view",)
                assert db.execute("SELECT is_public FROM board WHERE id='home'").fetchone() == (0,)
                assert db.execute("SELECT options FROM item WHERE id='personal'").fetchone()[0] == '{"json":{"title":"Личное"}}'
                db.execute("UPDATE item_layout SET y_offset=15 WHERE item_id=?", (module.ITEM_ID,))
                db.execute("UPDATE custom_widget_v2_definition SET name='Мои приложения' WHERE id=?",
                           (module.WIDGET_ID,))
                db.commit()
            assert "сохранены" in module.prepare(db_path, widget_path, state)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT name FROM custom_widget_v2_definition WHERE id=?",
                                  (module.WIDGET_ID,)).fetchone() == ("Мои приложения",)
                assert all(row[0] == 15 for row in db.execute(
                    "SELECT y_offset FROM item_layout WHERE item_id=?", (module.ITEM_ID,)
                ))
                db.execute("UPDATE board SET is_public=1 WHERE id='home'")
                db.commit()
            rejection(lambda: module.prepare(db_path, widget_path, state), module.HomeError)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT is_public FROM board WHERE id='home'").fetchone() == (1,)


def test_catalogue() -> None:
    module = load("home-apps.py")
    prov = yaml.safe_load((GUEST / "provision.yaml").read_text(encoding="utf-8"))
    rows = prov["web_gateway"]["caddy"]["routes"]
    policy = {"web_gateway": {"caddy": {"routes": rows}}}
    result = module.build_apps(rows, policy)
    assert result == {"schema_version": 1, "apps": []}
    changed = [dict(row) for row in rows]
    photo = next(row for row in changed if row["domain"] == "photos.zsergey.ru")
    photo["upstream"] = "http://192.168.1.40:2283"
    photo["enabled"] = True
    policy["web_gateway"]["caddy"]["routes"] = changed
    assert module.build_apps(changed, policy)["apps"] == [
        {"name": "Фотографии", "description": "Immich", "icon": "📷",
         "url": "https://photos.zsergey.ru"}
    ]
    check = next(row for row in changed if row["domain"] == "check.zsergey.ru")
    check["enabled"] = True
    assert len(module.build_apps(changed, policy)["apps"]) == 1
    changed[-1]["domain"] = "external.example.com"
    rejection(lambda: module.build_apps(changed, policy), module.CatalogueError)
    changed[-1] = dict(rows[-1])
    with TemporaryDirectory() as tmp:
        state = Path(tmp)
        (state / "management/catalog").mkdir(parents=True)
        routes = state / "management/routes.yaml"
        routes.write_text(yaml.safe_dump(rows))
        cfg = state / "policy.json"
        cfg.write_text(json.dumps({"web_gateway": {"caddy": {"routes": rows}}}))
        with patch.object(module.os.path, "ismount", return_value=True):
            assert module.update(state, cfg) is True
            assert module.update(state, cfg) is False
            assert json.loads((state / "management/catalog/apps.json").read_text())["apps"] == []
        with patch.object(module.os.path, "ismount", return_value=False):
            rejection(lambda: module.update(state, cfg), module.CatalogueError)


if __name__ == "__main__":
    test_page()
    test_catalogue()
    print("140: обычная доска создаётся один раз, пользовательские плитки остаются, список безопасен")
