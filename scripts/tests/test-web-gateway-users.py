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
            # Реальная почта не требуется: при создании используется адрес
            # файлового уведомителя, не предназначенный для доставки писем.
            password = mod.change(root, "add", "anna", groups="family")
            people = yaml.safe_load(users.read_text())["users"]
            assert people["admin"]["password"] == "existing-hash"
            assert PasswordHasher().verify(people["anna"]["password"], password)
            assert people["anna"]["groups"] == ["family"]
            assert people["anna"]["email"] == "anna@users.zsergey.ru"
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
            mod.change(root, "edit", "anna", groups="family,guests", displayname="Анна")
            people = yaml.safe_load(users.read_text())["users"]
            assert people["anna"]["displayname"] == "Анна"
            assert people["anna"]["groups"] == ["family", "guests"]
            expect_error(lambda: mod.change(root, "edit", "admin", groups="family", displayname="Главный"))
            expect_error(lambda: mod.change(root, "edit", "anna", groups="", displayname=""))
            mod.change(root, "disable", "anna")
            people = yaml.safe_load(users.read_text())["users"]
            assert people["anna"]["disabled"] is True
            assert people["anna"]["groups"] == ["family", "guests"]
            mod.change(root, "enable", "anna")
            assert yaml.safe_load(users.read_text())["users"]["anna"]["disabled"] is False
            expect_error(lambda: mod.change(root, "groups", "anna", groups="root"))
            expect_error(lambda: mod.change(root, "password", "admin"))
            expect_error(lambda: mod.change(root, "delete", "admin", revoke_totp=lambda _: None))
            expect_error(lambda: mod.change(root, "delete", "anna"))
            revoked = []
            def failed_revoke(username):
                revoked.append(username)
                raise RuntimeError("Authelia недоступна")
            old = users.read_bytes()
            expect_error(lambda: mod.change(root, "delete", "anna", revoke_totp=failed_revoke))
            assert users.read_bytes() == old
            assert revoked == ["anna"]
            mod.change(root, "delete", "anna", revoke_totp=revoked.append)
            assert "anna" not in yaml.safe_load(users.read_text())["users"]
            assert revoked == ["anna", "anna"]
            assert "admin" in yaml.safe_load(users.read_text())["users"]
        with patch.object(mod.os.path, "ismount", return_value=False):
            expect_error(lambda: mod.change(root, "add", "igor",
                                         email="igor@example.org", groups="family"))
    print("140: учётные записи сохраняются, последний администратор защищён")


if __name__ == "__main__":
    test()
