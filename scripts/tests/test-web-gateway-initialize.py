#!/usr/bin/env python3
"""Проверить первичное создание секретов 140 и сохранность при повторном запуске."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from argon2 import PasswordHasher
from passlib.hash import pbkdf2_sha512
import yaml


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "automation/ansible/roles/web_gateway/files/initialize.py"


def load_initializer():
    """Загрузить сценарий установки без запуска его точки входа."""
    spec = importlib.util.spec_from_file_location("web_gateway_init", SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test() -> None:
    """Сверить пароли, пары OIDC и сохранение уникальных данных."""
    module = load_initializer()
    with TemporaryDirectory() as root:
        state = Path(root) / "persistent"
        for dirname in ["authelia", "homarr", "secrets", "management"]:
            (state / dirname).mkdir(parents=True, mode=0o700)
        ephemeral = Path(root) / "run"
        module.initialize(state, "admin@zsergey.ru", ephemeral)

        database = yaml.safe_load((state / "authelia/users_database.yml").read_text())
        user = database["users"]["admin"]
        operator_file = state / "management/operator-admin.json"
        credential = json.loads(operator_file.read_text(encoding="utf-8"))
        first_password = credential["password"]
        assert credential["username"] == "admin"
        assert credential["password_hash"] == user["password"]
        assert PasswordHasher().verify(user["password"], first_password)
        assert stat.S_IMODE(operator_file.stat().st_mode) == 0o600
        assert not (ephemeral / "initial-admin-password").exists()
        assert user["groups"] == ["admins"]
        secrets_dir = state / "secrets"
        homarr_data = dict(
            line.split("=", 1)
            for line in (secrets_dir / "homarr.env").read_text().splitlines()
        )
        assert len(homarr_data["SECRET_ENCRYPTION_KEY"]) == 64
        assert pbkdf2_sha512.verify(
            homarr_data["AUTH_OIDC_CLIENT_SECRET"],
            (secrets_dir / "authelia-homarr-client-secret-hash").read_text().strip(),
        )
        assert pbkdf2_sha512.verify(
            (secrets_dir / "immich-client-secret").read_text().strip(),
            (secrets_dir / "authelia-immich-client-secret-hash").read_text().strip(),
        )
        before = {
            path.relative_to(state): path.read_bytes()
            for path in state.rglob("*") if path.is_file()
        }
        module.initialize(state, "admin@zsergey.ru", ephemeral)
        after = {
            path.relative_to(state): path.read_bytes()
            for path in state.rglob("*") if path.is_file()
        }
        assert before == after, "Повторная установка изменила файлы пользователей или секреты"

        # Гость предыдущей версии: перенос только действующего временного пароля.
        operator_file.unlink()
        ephemeral.mkdir(mode=0o700)
        legacy_password = ephemeral / "initial-admin-password"
        legacy_password.write_text(first_password + "\n", encoding="utf-8")
        os.chmod(legacy_password, 0o600)
        module.initialize(state, "admin@zsergey.ru", ephemeral)
        assert json.loads(operator_file.read_text())["password"] == first_password
        assert not legacy_password.exists(), "Временная копия должна исчезнуть после сохранения"
        # Потерянный пароль не восстанавливается из хеша и не перевыпускается.
        operator_file.unlink()
        module.initialize(state, "admin@zsergey.ru", ephemeral)
        assert not operator_file.exists()
        # Изменённый пароль нельзя переносить из старого временного файла.
        legacy_password.write_text("incorrect-password\n", encoding="utf-8")
        os.chmod(legacy_password, 0o600)
        module.initialize(state, "admin@zsergey.ru", ephemeral)
        assert not operator_file.exists()
        legacy_password.unlink()

        (secrets_dir / "immich-client-secret").unlink()
        try:
            module.initialize(state, "admin@zsergey.ru", ephemeral)
        except RuntimeError:
            pass
        else:
            raise AssertionError("Отсутствующий секрет должен блокировать запуск, а не пересоздаваться")
        assert not (secrets_dir / "immich-client-secret").exists()
    print("140: инициализация и повторный запуск сохраняют пользователей и ключи")


if __name__ == "__main__":
    test()
