#!/usr/bin/env python3
"""Проверить начальную настройку Immich 340 без обращения к работающему серверу."""

from __future__ import annotations

import copy
import importlib.util
import json
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

            with patch.object(module, "api", side_effect=fake_api):
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
    test_first_setup()
    print("OK: Immich initial setup contract")
