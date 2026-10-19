#!/usr/bin/env python3
"""Резервирование 140: проверять полноту SQLite без настоящих накопителей."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "scripts/backup/backup-140.py"


def load():
    spec = importlib.util.spec_from_file_location("backup_140", SOURCE)
    assert spec and spec.loader
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


def make_db(filename: Path):
    filename.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(filename) as db:
        db.execute("CREATE TABLE entries (name TEXT)")
        db.execute("INSERT INTO entries VALUES ('existing-record')")
        db.commit()


def test():
    script = load()
    with TemporaryDirectory() as temp:
        root = Path(temp)
        state = root / "140-web-gateway"
        state.mkdir()
        for item in ("authelia/db.sqlite3", "homarr/db/db.sqlite"):
            make_db(state / item)
        (state / "secrets").mkdir()
        (state / "secrets/.initialized").write_text("v1")
        (state / "authelia/users_database.yml").write_text("users: {}")
        (state / "management/admin-socket").mkdir(parents=True)
        with __import__("socket").socket(__import__("socket").AF_UNIX) as sock:
            socket_file = state / "management/admin-socket/control.sock"
            sock.bind(str(socket_file))
            staging = root / "working"
            staging.mkdir()
            script.consistent_copy(state, staging)
            saved = staging / "140-web-gateway"
            for name in ("authelia/db.sqlite3", "homarr/db/db.sqlite"):
                with sqlite3.connect(saved / name) as db:
                    row = db.execute("SELECT name FROM entries").fetchone()
                    assert row == ("existing-record",)
            assert not (saved / "management/admin-socket/control.sock").exists()
            assert (staging / "manifest.json").exists()
        # Чистая установка без Homarr также должна резервироваться.
        (state / "homarr/db/db.sqlite").unlink()
        staging_new = root / "new-portal"
        staging_new.mkdir()
        script.consistent_copy(state, staging_new)
        saved_new = staging_new / "140-web-gateway"
        assert (saved_new / "authelia/db.sqlite3").is_file()
        assert not (saved_new / "homarr/db/db.sqlite").exists()
        import json
        manifest = json.loads((staging_new / "manifest.json").read_text())
        assert manifest["sqlite_databases"] == ["authelia/db.sqlite3"]
        (state / "authelia/links").symlink_to(root)
        try:
            staging2 = root / "stage2"
            staging2.mkdir()
            script.consistent_copy(state, staging2)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Резервирование не должно следовать за ссылками")
    print("140: копия Authelia обязательна, прежняя база Homarr необязательна")


if __name__ == "__main__":
    test()
