#!/usr/bin/env python3
"""Изменение пользователей не теряет остальных, не раскрывает хеши и защищает admin."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from argon2 import PasswordHasher
import yaml

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "automation/ansible/roles/web_gateway/files/user-accounts.py"


def module():
    spec = importlib.util.spec_from_file_location("user_accounts_140", FILE)
    assert spec and spec.loader
    target = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(target)
    return target


def expect_error(fn):
    try:
        fn()
    except (ValueError, RuntimeError):
        return
    raise AssertionError("Операция должна быть отклонена")


def test():
    mod = module()
    with TemporaryDirectory() as temp:
        root = Path(temp)
        (root / "authelia").mkdir()
        (root / "secrets").mkdir()
        (root / "secrets/.initialize.lock").write_text("ready")
        users = root / "authelia/users_database.yml"
        users.write_text(yaml.safe_dump({"users": {"admin": {
            "email": "admin@example.org", "displayname": "Главный",
            "password": "existing-hash", "groups": ["admins"],
        }}}), encoding="utf-8")
        users.chmod(0o600)

        with patch.object(mod.os.path, "ismount", return_value=True):
            original = users.read_bytes()
            expect_error(lambda: mod.change(root, "disable", "admin"))
            assert users.read_bytes() == original
            expect_error(lambda: mod.change(root, "groups", "admin", groups="family"))
            assert users.read_bytes() == original
            expect_error(lambda: mod.change(root, "add", "anna",
                                         email="admin@example.org", groups="family"))
            assert users.read_bytes() == original
            password = mod.change(root, "add", "anna",
                                  email="anna@example.org", groups="family")
            people = yaml.safe_load(users.read_text())["users"]
            assert people["admin"]["password"] == "existing-hash"
            assert PasswordHasher().verify(people["anna"]["password"], password)
            assert people["anna"]["groups"] == ["family"]
            assert users.stat().st_mode & 0o777 == 0o600
            before = users.read_bytes()
            expect_error(lambda: mod.change(root, "add", "anna",
                                         email="anna@example.org", groups="family"))
            assert users.read_bytes() == before
            changed = mod.change(root, "password", "anna")
            assert changed != password
            people = yaml.safe_load(users.read_text())["users"]
            assert PasswordHasher().verify(people["anna"]["password"], changed)
            mod.change(root, "groups", "anna", groups="admins,family")
            mod.change(root, "disable", "anna")
            people = yaml.safe_load(users.read_text())["users"]
            assert people["anna"]["disabled"] is True
            assert people["anna"]["groups"] == ["admins", "family"]
            mod.change(root, "enable", "anna")
            assert yaml.safe_load(users.read_text())["users"]["anna"]["disabled"] is False
            expect_error(lambda: mod.change(root, "groups", "anna", groups="root"))
            expect_error(lambda: mod.change(root, "password", "admin"))
        with patch.object(mod.os.path, "ismount", return_value=False):
            expect_error(lambda: mod.change(root, "add", "igor",
                                         email="igor@example.org", groups="family"))
    print("140: учётные записи сохраняются, последний администратор защищён")


if __name__ == "__main__":
    test()
