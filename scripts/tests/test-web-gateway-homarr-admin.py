#!/usr/bin/env python3
"""Испытать первичное создание Homarr без настоящих серверов и чужих данных."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "automation/ansible/roles/web_gateway/files/configure-homarr-admin.py"
WIDGETS = ROOT / "infrastructure/guests/140-web-gateway/widgets"


def load():
    spec = importlib.util.spec_from_file_location("homarr_board", SCRIPT)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT NOT NULL);
    CREATE TABLE groupPermission(group_id TEXT NOT NULL,permission TEXT NOT NULL);
    CREATE TABLE onboarding(step TEXT NOT NULL);
    CREATE TABLE board (id TEXT PRIMARY KEY,name TEXT UNIQUE,is_public INTEGER DEFAULT 0,
                        creator_id TEXT);
    CREATE TABLE boardGroupPermission (
        board_id TEXT,group_id TEXT,permission TEXT,
        FOREIGN KEY(board_id) REFERENCES board(id));
    CREATE TABLE boardUserPermission(board_id TEXT,user_id TEXT,permission TEXT);
    CREATE TABLE layout(
        id TEXT PRIMARY KEY,name TEXT,board_id TEXT,column_count INTEGER,breakpoint INTEGER,role TEXT,
        FOREIGN KEY(board_id) REFERENCES board(id));
    CREATE TABLE section (
        id TEXT PRIMARY KEY,board_id TEXT,kind TEXT,x_offset INTEGER,y_offset INTEGER,
        FOREIGN KEY(board_id) REFERENCES board(id));
    CREATE TABLE item (
        id TEXT PRIMARY KEY,board_id TEXT,kind TEXT,options TEXT,advanced_options TEXT,
        FOREIGN KEY(board_id) REFERENCES board(id));
    CREATE TABLE item_layout (
        item_id TEXT,section_id TEXT,layout_id TEXT,x_offset INTEGER,y_offset INTEGER,
        width INTEGER,height INTEGER,
        FOREIGN KEY(item_id) REFERENCES item(id),
        FOREIGN KEY(section_id) REFERENCES section(id),
        FOREIGN KEY(layout_id) REFERENCES layout(id));
    CREATE TABLE custom_widget_v2_definition (
        id TEXT PRIMARY KEY,name TEXT,description TEXT,sources TEXT,requests TEXT,
        options TEXT,template TEXT,enabled INTEGER,creator_id TEXT);
    CREATE TABLE custom_widget_v2_secret (
        definition_id TEXT,source_id TEXT,kind TEXT,encrypted_value TEXT,updated_at INTEGER,
        FOREIGN KEY(definition_id) REFERENCES custom_widget_v2_definition(id));
    INSERT INTO onboarding VALUES('finish');
    INSERT INTO "group" VALUES('group-admin','admins');
    INSERT INTO groupPermission VALUES('group-admin','admin');
    INSERT INTO board VALUES('original','dashboard',0,NULL);
    """)


def should_fail(operation) -> None:
    try:
        operation()
    except (RuntimeError, ValueError, sqlite3.DatabaseError):
        return
    raise AssertionError("Действие без полномочий или данных должно быть отклонено")


def test() -> None:
    manager = load()
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "homarr/db").mkdir(parents=True)
        (root / "secrets").mkdir()
        db_path = root / "homarr/db/db.sqlite"
        token = root / "secrets/administration-token"
        env = root / "secrets/homarr.env"
        token.write_text("test-administration-" + "A" * 48, encoding="utf-8")
        env.write_text("SECRET_ENCRYPTION_KEY=" + "ab" * 32, encoding="utf-8")
        token.chmod(0o600)
        env.chmod(0o600)
        with sqlite3.connect(db_path) as db:
            schema(db)
        def deploy():
            return manager.initialize(db_path, WIDGETS, token, env, persistent_root=root)

        with patch.object(manager.os.path, "ismount", return_value=False):
            should_fail(deploy)
        with patch.object(manager.os.path, "ismount", return_value=True):
            result = deploy()
            assert "три виджета" in result
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM board").fetchone()[0] == 2
                assert db.execute("SELECT name FROM board WHERE id='original'").fetchone()[0] == "dashboard"
                assert db.execute("SELECT is_public FROM board WHERE id=?", (manager.BOARD_ID,)).fetchone()[0] == 0
                assert db.execute("SELECT group_id,permission FROM boardGroupPermission").fetchall() == [("group-admin", "full")]
                assert db.execute("SELECT COUNT(*) FROM custom_widget_v2_definition").fetchone()[0] == 3
                assert db.execute("SELECT COUNT(*) FROM item WHERE kind='customApi'").fetchone()[0] == 3
                assert db.execute("SELECT COUNT(*) FROM item_layout").fetchone()[0] == 6
                keys = db.execute("SELECT encrypted_value FROM custom_widget_v2_secret").fetchall()
                assert len(keys) == 2
                for (secret,) in keys:
                    ciphertext, iv = secret.split(".")
                    decryptor = Cipher(algorithms.AES(bytes.fromhex("ab" * 32)),
                                       modes.CBC(bytes.fromhex(iv))).decryptor()
                    padded = decryptor.update(bytes.fromhex(ciphertext)) + decryptor.finalize()
                    unpadder = padding.PKCS7(128).unpadder()
                    clear = unpadder.update(padded) + unpadder.finalize()
                    assert clear.decode() == token.read_text().strip()
                assert db.execute("PRAGMA foreign_key_check").fetchall() == []
                db.execute("UPDATE custom_widget_v2_definition SET name='Своя подпись' "
                           "WHERE id='web_gateway_140_users'")
                db.commit()
            assert "сохранены" in deploy()
            with sqlite3.connect(db_path) as db:
                assert db.execute(
                    "SELECT name FROM custom_widget_v2_definition WHERE id='web_gateway_140_users'"
                ).fetchone()[0] == "Своя подпись"
                db.execute("INSERT INTO boardGroupPermission VALUES (?,?,?)",
                           (manager.BOARD_ID, "group-admin", "view"))
                db.commit()
            should_fail(deploy)
            with sqlite3.connect(db_path) as db:
                db.execute("DELETE FROM boardGroupPermission WHERE permission='view'")
                db.execute("DELETE FROM board WHERE id=?", (manager.BOARD_ID,))
                db.commit()
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM custom_widget_v2_definition").fetchone()[0] == 3
                db.execute("DELETE FROM custom_widget_v2_definition")
                db.execute("DELETE FROM item")
                db.execute("DELETE FROM layout")
                db.execute("DELETE FROM section")
                db.execute("DELETE FROM groupPermission")
                db.commit()
            should_fail(deploy)
            with sqlite3.connect(db_path) as db:
                assert db.execute("SELECT COUNT(*) FROM board").fetchone()[0] == 1
                assert db.execute("SELECT COUNT(*) FROM item").fetchone()[0] == 0
        assert (root / "homarr/db/db.sqlite").exists()
    print("140: Homarr создаётся один раз, ключ шифруется, права и данные сохраняются")


if __name__ == "__main__":
    test()
