#!/usr/bin/env python3
"""Проверить первичное создание секретов 140 и сохранность при повторном запуске."""

from __future__ import annotations

import importlib.util
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
        for dirname in ["authelia", "homarr", "secrets"]:
            (state / dirname).mkdir(parents=True, mode=0o700)
        ephemeral = Path(root) / "run"
        module.initialize(state, "admin@zsergey.ru", ephemeral)

        database = yaml.safe_load((state / "authelia/users_database.yml").read_text())
        user = database["users"]["admin"]
        first_password = (ephemeral / "initial-admin-password").read_text().strip()
        assert PasswordHasher().verify(user["password"], first_password)
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
