#!/usr/bin/env python3
"""Список приложений домашней страницы из включённых разрешённых маршрутов."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import tempfile

import yaml

NAMES = {
    "immich": ("Фотографии", "Immich", "📷"),
    "home-assistant": ("Умный дом", "Home Assistant", "🏠"),
    "open-webui": ("ИИ-помощник", "Open WebUI", "🤖"),
}
INTERNAL = frozenset({"homarr", "authelia"})
DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.zsergey\.ru\Z")


class CatalogueError(Exception):
    """Неверное постоянное состояние; не публиковать незнакомые адреса."""


def build_apps(routes: object, policy: dict) -> dict:
    if not isinstance(routes, list):
        raise CatalogueError("Список маршрутов должен быть списком")
    caddy = policy.get("web_gateway", {}).get("caddy", {})
    configured = caddy.get("routes")
    if not isinstance(configured, list):
        raise CatalogueError("Не найден проверенный перечень назначений")
    allowed = {route.get("domain"): route for route in configured if isinstance(route, dict)}
    result = []
    seen = set()
    for route in routes:
        if not isinstance(route, dict):
            raise CatalogueError("Повреждена запись маршрута")
        domain = route.get("domain")
        if not isinstance(domain, str) or domain not in allowed or not DOMAIN.fullmatch(domain) or domain in seen:
            raise CatalogueError("Неизвестный или повторяющийся маршрут")
        seen.add(domain)
        spec = allowed[domain]
        if route.get("service") != spec.get("service"):
            raise CatalogueError("Назначение маршрута отличается от утверждённого")
        if not isinstance(route.get("enabled"), bool):
            raise CatalogueError("У маршрута нет состояния включения")
        if not route["enabled"] or spec.get("service") in INTERNAL:
            continue
        if domain == "check.zsergey.ru":
            continue
        if route.get("upstream") != spec.get("upstream") or not isinstance(route.get("upstream"), str):
            raise CatalogueError("Адрес приложения не совпадает с описанием")
        if route.get("access") not in {"protected", "private", "app_auth"}:
            raise CatalogueError("Доступ к приложению не подтверждён")
        service = spec["service"]
        name, subtitle, emoji = NAMES.get(service, (service.replace("-", " ").title(), service, "🔗"))
        result.append({"name": name, "description": subtitle, "icon": emoji, "url": "https://" + domain})
    return {"schema_version": 1, "apps": sorted(result, key=lambda a: a["name"])}


def update(state: Path, policy_path: Path) -> bool:
    if state.is_symlink() or not os.path.ismount(state):
        raise CatalogueError("Постоянный том 140 не подключён")
    routes_path = state / "management/routes.yaml"
    destination = state / "management/catalog/apps.json"
    if routes_path.is_symlink() or not routes_path.is_file() or policy_path.is_symlink() or not policy_path.is_file():
        raise CatalogueError("Нет сохранённых маршрутов или утверждённых правил")
    if not destination.parent.is_dir() or destination.parent.is_symlink() or destination.is_symlink():
        raise CatalogueError("Отсутствует закрытый каталог Homarr")
    routes = yaml.safe_load(routes_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    content = (json.dumps(build_apps(routes, policy), ensure_ascii=False, indent=2) + "\n").encode()
    if destination.exists() and destination.read_bytes() == content:
        return False
    fd, tmp = tempfile.mkstemp(prefix=".apps-", dir=destination.parent)
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, destination)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=Path("/var/lib/web-gateway"))
    parser.add_argument("--policy", type=Path, default=Path("/etc/web-gateway/caddy/routes-policy.json"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Требуются права администратора гостя 140")
    try:
        update(args.state, args.policy)
        print("140: список доступных приложений обновлён")
        return 0
    except (CatalogueError, ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, "140: приложения не обновлены: " + str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
