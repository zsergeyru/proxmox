#!/usr/bin/env python3
"""Проверить начальную настройку Immich 340 без обращения к работающему серверу."""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "automation/ansible/roles/photo_archive/files/initial_setup.py"
SPEC = importlib.util.spec_from_file_location("immich_initial_setup", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)

CONFIG = {
    "admin": {"email": "admin@zsergey.ru", "name": "Администратор"},
    "users": [
        {"email": "mail@zsergey.ru", "name": "Основной пользователь"},
        {"email": "zsveta@elsoft35.ru", "name": "Семейный пользователь"},
    ],
    "library": {"name": "Семейный фотоархив", "import_path": "/external/originals"},
    "scan_cron": "0 * * * *",
}


def test_first_setup() -> None:
    """Создание, повторный запуск и восстановление с половины настройки."""
    for interrupt in (False, True):
        for existing_users in (False, True):
            with tempfile.TemporaryDirectory() as tmp:
                state = Path(tmp) / "persistent"
                original = Path(tmp) / "originals"
                original.mkdir()
                server = {
                    "users": {},
                    "libraries": [],
                    "partners": set(),
                    "config": {
                        "library": {
                            "scan": {"enabled": True, "cronExpression": "0 0 * * *"},
                            "watch": {"enabled": True},
                        },
                        "machineLearning": {"enabled": True},
                        "ffmpeg": {"transcode": "required"},
                        "job": {
                            key: {"concurrency": 5}
                            for key in (
                                "thumbnailGeneration", "metadataExtraction", "library",
                                "videoConversion",
                            )
                        },
                        "other": {"keep": True},
                    },
                    "calls": [],
                    "interrupt": interrupt,
                }

                if existing_users:
                    server["users"] = {
                        "admin@zsergey.ru": {
                            "id": "admin-id", "email": "admin@zsergey.ru",
                            "isAdmin": True, "password": "manual-admin-password",
                        },
                        "mail@zsergey.ru": {
                            "id": "id-mail", "email": "mail@zsergey.ru",
                            "isAdmin": False, "password": "changed-by-user",
                        },
                    }
                server["admin_resets"] = 0

                def fake_reset_admin(email: str, password: str):
                    assert email == CONFIG["admin"]["email"]
                    assert existing_users
                    server["admin_resets"] += 1
                    server["users"][email]["password"] = password

                def fake_api(base, method, path, payload=None, token=""):
                    assert base == "http://127.0.0.1:2283"
                    server["calls"].append((method, path))
                    if path == "/auth/admin-sign-up":
                        if server["users"]:
                            # v3.3.1 отклоняет повторную регистрацию кодом HTTP 400.
                            raise module.ApiError(method, path, 400)
                        server["users"][payload["email"]] = {
                            "id": "admin-id", "email": payload["email"],
                            "isAdmin": True, "password": payload["password"],
                        }
                        return {"id": "admin-id"}
                    if path == "/auth/login":
                        entry = server["users"].get(payload["email"])
                        if not entry or entry["password"] != payload["password"]:
                            raise module.ApiError(method, path, 401)
                        return {"accessToken": "token", "isAdmin": entry["isAdmin"]}
                    assert token == "token"
                    if path == "/admin/users":
                        if method == "GET":
                            return copy.deepcopy(list(server["users"].values()))
                        server["users"][payload["email"]] = {
                            "id": "id-" + payload["email"], "email": payload["email"],
                            "isAdmin": False, "password": payload["password"],
                        }
                        return server["users"][payload["email"]]
                    if path == "/libraries":
                        if method == "GET":
                            return copy.deepcopy(server["libraries"])
                        library = {**payload, "id": "library-id"}
                        server["libraries"].append(library)
                        return library
                    if path == "/partners?direction=shared-by":
                        return [
                            {"id": user_id} for user_id in server["partners"]
                        ]
                    if path == "/partners":
                        if server["interrupt"]:
                            server["interrupt"] = False
                            raise module.ApiError(method, path, 503)
                        server["partners"].add(payload["sharedWithId"])
                        return {"id": payload["sharedWithId"]}
                    if path == "/admin/config":
                        if method == "GET":
                            return copy.deepcopy(server["config"])
                        server["config"] = copy.deepcopy(payload)
                        return payload
                    raise AssertionError((method, path))

                with (
                    patch.object(module, "api", side_effect=fake_api),
                    patch.object(module, "reset_existing_admin", side_effect=fake_reset_admin),
                ):
                    if interrupt:
                        try:
                            module.initialize(
                                "http://127.0.0.1:2283", CONFIG, state, original
                            )
                        except module.ApiError as exc:
                            assert exc.code == 503
                        else:
                            raise AssertionError("Ожидалось прерывание")
                        assert not (state / "initial-setup.done").exists()
                    assert module.initialize(
                        "http://127.0.0.1:2283", CONFIG, state, original
                    )
                    calls = len(server["calls"])
                    assert not module.initialize(
                        "http://127.0.0.1:2283", CONFIG, state, original
                    )
                    assert len(server["calls"]) == calls

                users = server["users"]
                assert server["admin_resets"] == int(existing_users)
                if existing_users:
                    # При уже существующем пользователе не меняем пароль,
                    # даже если он изменил его до этого deploy.
                    assert users["mail@zsergey.ru"]["password"] == "changed-by-user"
                assert set(users) == {
                    CONFIG["admin"]["email"],
                    *[user["email"] for user in CONFIG["users"]],
                }
                assert len(server["libraries"]) == 1
                assert server["libraries"][0]["importPaths"] == ["/external/originals"]
                assert len(server["partners"]) == 2
                assert server["config"]["library"]["scan"]["cronExpression"] == "0 * * * *"
                assert server["config"]["library"]["watch"]["enabled"] is False
                assert server["config"]["machineLearning"]["enabled"] is False
                assert server["config"]["ffmpeg"]["transcode"] == "disabled"
                assert server["config"]["other"] == {"keep": True}
                for setting in server["config"]["job"].values():
                    assert setting["concurrency"] == 1
                passwords_file = state / "secrets" / "immich-initial-users.json"
                assert passwords_file.stat().st_mode & 0o777 == 0o600
                passwords = json.loads(passwords_file.read_text(encoding="utf-8"))
                assert len(passwords) == 3
                assert all(len(password) >= 32 for password in passwords.values())
                assert (state / "initial-settings.done").exists()
                assert (state / "initial-setup.done").exists()



def test_admin_cli_uses_tty_and_checks_email() -> None:
    """Штатная интерактивная команда: сброс только верного администратора."""
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "fake-docker.py"
        fake.write_text(
            "import os,sys\\n"
            "email = os.environ['TEST_ADMIN_EMAIL']\\n"
            "print('Found Admin:')\\n"
            "print('- ID=123')\\n"
            "print('- Email=' + email, flush=True)\\n"
            "print('? Please choose a new password (optional) ', end='', flush=True)\\n"
            "password = input()\\n"
            "print('? Invalidate existing sessions? ', end='', flush=True)\\n"
            "confirmation = input()\\n"
            "if password != os.environ['TEST_ADMIN_PASSWORD'] or confirmation != 'y':\\n"
            "    sys.exit(2)\\n"
            "print('The admin password has been updated.', flush=True)\\n",
            encoding="utf-8",
        )
        real_popen = module.subprocess.Popen

        def cli_popen(command, **kwargs):
            assert command == [
                "docker", "exec", "-it", "-e", "TERM=xterm",
                "immich_server", "immich-admin", "reset-admin-password",
            ]
            return real_popen([sys.executable, "-u", str(fake)], **kwargs)

        with (
            patch.object(module.subprocess, "Popen", side_effect=cli_popen),
            patch.dict(
                module.os.environ,
                {"TEST_ADMIN_EMAIL": "admin@zsergey.ru",
                 "TEST_ADMIN_PASSWORD": "a-secret-admin-password"},
            ),
        ):
            module.reset_existing_admin("admin@zsergey.ru", "a-secret-admin-password")
        with (
            patch.object(module.subprocess, "Popen", side_effect=cli_popen),
            patch.dict(module.os.environ, {"TEST_ADMIN_EMAIL": "other@example.net"}),
        ):
            try:
                module.reset_existing_admin("admin@zsergey.ru", "secret")
            except RuntimeError as exc:
                assert "не совпадает" in str(exc)
            else:
                raise AssertionError("Смена пароля другого администратора разрешена")



def test_config_payload() -> None:
    """Описание из Ansible должно быть JSON без буквального \\n в конце."""
    role = ROOT / "automation/ansible/roles/photo_archive/tasks/main.yml"
    tasks = yaml.safe_load(role.read_text(encoding="utf-8"))
    task = next(
        item for item in tasks
        if item.get("name") == "Сохранить описание начальной настройки без паролей"
    )
    content = task["ansible.builtin.copy"]["content"]
    assert content == "{{ provision.photo_archive.initial_setup | to_nice_json(ensure_ascii=False) }}", (
        "Ansible добавляет лишние символы после JSON"
    )


if __name__ == "__main__":
    test_config_payload()
    test_admin_cli_uses_tty_and_checks_email()
    test_first_setup()
    print("OK: Immich initial setup contract")
