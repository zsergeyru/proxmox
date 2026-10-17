#!/usr/bin/env python3
"""Проверки ограничений, проверки конфигурации и возврата маршрутов Caddy."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = ROOT / "automation/ansible/roles/web_gateway/files"


def load():
    spec = importlib.util.spec_from_file_location("routes_140", FILES / "manage-routes.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rejects(call):
    try:
        call()
    except (ValueError, RuntimeError):
        return
    raise AssertionError("Небезопасное изменение должно быть запрещено")


def test():
    manager = load()
    with TemporaryDirectory() as temp:
        state = Path(temp) / "state"
        config = Path(temp) / "caddy"
        (state / "management").mkdir(parents=True)
        config.mkdir()
        old = [
            {"domain": "home.zsergey.ru", "service": "homarr",
             "upstream": "http://homarr:7575", "access": "public", "enabled": True},
            {"domain": "auth.zsergey.ru", "service": "authelia",
             "upstream": "http://authelia:9091", "access": "public", "enabled": True},
            {"domain": "ai.zsergey.ru", "service": "open-webui",
             "upstream": "http://ai-control:3000", "access": "protected", "enabled": False},
            {"domain": "photos.zsergey.ru", "service": "immich",
             "access": "app_auth", "enabled": False},
        ]
        routes = state / "management/routes.yaml"
        routes.write_text(yaml.safe_dump(old), encoding="utf-8")
        routes.chmod(0o640)
        active = config / "sites.generated.caddy"
        active.write_text("old config", encoding="utf-8")
        active.chmod(0o644)
        (config / "Caddyfile").write_text("import sites.generated.caddy\n:8080 { respond \"ok\" }\n")
        template = ROOT / "automation/ansible/roles/web_gateway/templates/sites.generated.caddy.j2"
        (config / "sites.generated.caddy.j2").write_text(template.read_text(), encoding="utf-8")
        policy = {
            "web_gateway": {
                "caddy": {"routes": old, "public_domains": ["home.zsergey.ru", "auth.zsergey.ru"],
                          "private_networks": ["192.168.0.0/16"]},
                "authelia": {"public_url": "https://auth.zsergey.ru"},
                "homarr": {"public_url": "https://home.zsergey.ru"},
            },
            "allow_immich_activation": False,
        }
        (config / "routes-policy.json").write_text(json.dumps(policy), encoding="utf-8")
        with patch.object(manager.os.path, "ismount", return_value=True):
            rejects(lambda: manager.change_route(state, config, "home.zsergey.ru", False, "private"))
            rejects(lambda: manager.change_route(state, config, "new.zsergey.ru", True, "protected"))
            rejects(lambda: manager.change_route(state, config, "photos.zsergey.ru", True, "app_auth"))
            with (
                patch.object(manager.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run,
                patch.object(manager, "_refresh_apps") as refresh,
            ):
                manager.change_route(state, config, "ai.zsergey.ru", True, "private")
                assert refresh.call_count == 1
                assert run.call_count == 2
                assert run.call_args_list[0].args[0][4] == "validate"
                assert run.call_args_list[1].args[0][4] == "reload"
            updated = yaml.safe_load(routes.read_text())
            assert next(row for row in updated if row["domain"] == "ai.zsergey.ru")["enabled"]
            assert "ai.zsergey.ru" in active.read_text()
            assert ".candidate-routes.caddy" not in str(active.read_text())
            assert not (config / ".candidate-Caddyfile").exists()
            assert not (config / ".candidate-routes.caddy").exists()
            # Сохранённое состояние и применённая конфигурация должны совпадать.
            before_yaml, before_caddy = routes.read_bytes(), active.read_bytes()
            statuses = [0, 1, 0]
            def runner(*args, **kwargs):
                return SimpleNamespace(returncode=statuses.pop(0))
            with (
                patch.object(manager.subprocess, "run", side_effect=runner),
                patch.object(manager, "_refresh_apps") as refresh,
            ):
                rejects(lambda: manager.change_route(state, config, "ai.zsergey.ru", False, "protected"))
                assert refresh.call_count == 1
            assert routes.read_bytes() == before_yaml
            assert active.read_bytes() == before_caddy
            assert not (config / ".candidate-Caddyfile").exists()
            assert not (config / ".candidate-routes.caddy").exists()
            # Если не удалось обновить домашние ссылки, вернуть Caddy и маршруты.
            with (
                patch.object(manager.subprocess, "run", return_value=SimpleNamespace(returncode=0)),
                patch.object(manager, "_refresh_apps",
                             side_effect=[RuntimeError("catalogue"), None]) as refresh,
            ):
                rejects(lambda: manager.change_route(state, config, "ai.zsergey.ru", False, "protected"))
                assert refresh.call_count == 2
            assert routes.read_bytes() == before_yaml
            assert active.read_bytes() == before_caddy
        with patch.object(manager.os.path, "ismount", return_value=False):
            rejects(lambda: manager.change_route(state, config, "ai.zsergey.ru", False, "protected"))
    print("140: изменение маршрута откатывается при ошибке Caddy")


if __name__ == "__main__":
    test()
