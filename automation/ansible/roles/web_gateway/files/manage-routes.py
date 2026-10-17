#!/usr/bin/env python3
"""Безопасное изменение заранее описанных маршрутов Caddy гостя 140.

Разрешены только утверждённые в Git назначения. Проверка Caddy обязательна;
при ошибке возвращаются прежние файлы и действующая конфигурация.
"""
from __future__ import annotations

import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit

from jinja2 import Environment, StrictUndefined
import yaml

STATE = Path("/var/lib/web-gateway")
CONFIG = Path("/etc/web-gateway/caddy")
POLICY = CONFIG / "routes-policy.json"
TEMPLATE = CONFIG / "sites.generated.caddy.j2"
CONTAINER = "web-gateway-caddy"
ALLOWED_ACCESS = frozenset({"protected", "private", "app_auth"})


def _refresh_apps(state: Path, config: Path) -> None:
    """Синхронизировать домашние ссылки после подтверждённого Caddy reload."""
    script = Path(__file__).with_name("home-apps.py")
    result = subprocess.run(
        ["python3", str(script), "--state", str(state),
         "--policy", str(config / "routes-policy.json")],
        capture_output=True, check=False, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Не удалось обновить домашние приложения")


def _file(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("Не найден или повреждён обязательный файл: " + path.name)


def _atomic(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise RuntimeError("Недопустимая ссылка вместо файла: " + path.name)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".route-write-")
    try:
        os.fchmod(fd, 0o640 if path.suffix == ".yaml" else 0o644)
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def _command(args: list[str]) -> None:
    result = subprocess.run(args, capture_output=True, check=False, timeout=30)
    if result.returncode:
        # Вывод Caddy может содержать внутренние адреса; не показываем его через API.
        raise RuntimeError("Caddy отклонил настройки или не ответил: " + args[3])


DOMAIN = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+zsergey\.ru\Z")
LABEL = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9 _.-]{1,60}\Z")


def _private_upstream(value: object, policy: dict) -> bool:
    if not isinstance(value, str) or len(value) > 160:
        return False
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username or url.password or url.path or url.query or url.fragment
                or not url.port or url.port > 65535):
            return False
        ip = ipaddress.ip_address(url.hostname)
        networks = policy["web_gateway"]["caddy"]["private_networks"]
        return (ip.version == 4 and not ip.is_loopback and not ip.is_multicast
                and not ip.is_unspecified and
                any(ip in ipaddress.ip_network(net, strict=False) for net in networks))
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def _validate_routes(routes: object, policy: dict) -> list[dict]:
    """Проверить полный список перед изменением Caddy."""
    if not isinstance(routes, list):
        raise RuntimeError("Список доменов повреждён")
    allowed = {r["domain"]: r for r in policy["web_gateway"]["caddy"]["routes"]}
    public = set(policy["web_gateway"]["caddy"]["public_domains"])
    seen: set[str] = set()
    result = []
    for row in routes:
        if not isinstance(row, dict) or not isinstance(row.get("domain"), str):
            raise RuntimeError("Неверная запись маршрута")
        domain = row["domain"]
        if domain in seen or not DOMAIN.fullmatch(domain):
            raise RuntimeError("Неверное или повторяющееся имя домена")
        seen.add(domain)
        if type(row.get("enabled")) is not bool:
            raise RuntimeError("Неверное значение включения")
        baseline = allowed.get(domain)
        mode = row.get("access", "public" if domain in public else "protected")
        if baseline is None:
            if (row.get("service") != "custom" or mode not in {"protected", "private"}
                    or not _private_upstream(row.get("upstream"), policy)
                    or not isinstance(row.get("name"), str) or not LABEL.fullmatch(row["name"])
                    or set(row) != {"domain", "service", "upstream", "name", "enabled", "access"}):
                raise RuntimeError("Новый домен должен указывать на внутренний адрес")
        else:
            if row.get("service") != baseline.get("service"):
                raise RuntimeError("Нельзя менять назначение домена из Git")
            if row.get("upstream") is not None and row["upstream"] != baseline.get("upstream"):
                raise RuntimeError("Адрес приложения должен определяться только в Git")
            if domain in public:
                if not row["enabled"] or mode != "public":
                    raise RuntimeError("Нельзя отключить основной вход или домашнюю страницу")
            elif mode not in ALLOWED_ACCESS:
                raise RuntimeError("Открытый доступ для этого домена запрещён")
            if mode == "app_auth" and (domain != "photos.zsergey.ru" or baseline.get("service") != "immich"):
                raise RuntimeError("Самостоятельный вход разрешён только для Immich")
            if row["enabled"]:
                expected = baseline.get("upstream")
                if not isinstance(expected, str) or not re.fullmatch(r"http://[a-z0-9.-]+:[0-9]{2,5}", expected):
                    raise RuntimeError("Нет заранее утверждённого адреса приложения")
                if mode == "app_auth" and not policy.get("allow_immich_activation", False):
                    raise RuntimeError("Immich сначала должен пройти отдельную проверку входа")
        result.append(dict(row))
    if not public.issubset(seen):
        raise RuntimeError("Отсутствует обязательный домен")
    return result

def _render(routes: list[dict], policy: dict, template: Path) -> bytes:
    env = Environment(autoescape=False, undefined=StrictUndefined, keep_trailing_newline=True)
    env.filters["bool"] = bool
    result = env.from_string(template.read_text(encoding="utf-8")).render(
        web_gateway_routes=routes, provision=policy,
    )
    if not result.strip():
        raise RuntimeError("Пустые настройки Caddy")
    return result.encode("utf-8")


def apply_route(state: Path, config: Path, action: str, domain: str, *,
                enabled: bool = False, access: str = "protected",
                upstream: str | None = None, name: str | None = None) -> None:
    """Создать, изменить или удалить домен с проверкой и полным откатом."""
    if action not in {"create", "update", "delete"}:
        raise ValueError("Неизвестное действие")
    if not os.path.ismount(state) or state.is_symlink():
        raise RuntimeError("Постоянный том не подключён")
    if not isinstance(domain, str) or not DOMAIN.fullmatch(domain):
        raise ValueError("Неверное имя домена")
    if action != "delete" and (type(enabled) is not bool or access not in ALLOWED_ACCESS):
        raise ValueError("Укажите разрешённый режим доступа и включения")
    stored = state / "management/routes.yaml"
    generated = config / "sites.generated.caddy"
    main = config / "Caddyfile"
    template = config / "sites.generated.caddy.j2"
    policy_file = config / "routes-policy.json"
    lockfile = state / "management/.routes.lock"
    for path in (stored, generated, main, template, policy_file):
        _file(path)
    if lockfile.is_symlink():
        raise RuntimeError("Недопустимая блокировка")
    with lockfile.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        policy = json.loads(policy_file.read_text(encoding="utf-8"))
        original_stored, original_generated = stored.read_bytes(), generated.read_bytes()
        rows = _validate_routes(yaml.safe_load(original_stored), policy)
        declared = {r["domain"] for r in policy["web_gateway"]["caddy"]["routes"]}
        selected = next((entry for entry in rows if entry["domain"] == domain), None)
        if action == "create":
            if selected is not None or domain in declared:
                raise ValueError("Домен уже существует")
            rows.append({"domain": domain, "service": "custom", "name": name,
                         "upstream": upstream, "enabled": enabled, "access": access})
        elif action == "delete":
            if selected is None or domain in declared:
                raise ValueError("Удалять можно только пользовательские домены")
            rows.remove(selected)
        else:
            if selected is None:
                raise ValueError("Домен не найден")
            if domain in policy["web_gateway"]["caddy"]["public_domains"]:
                raise ValueError("Основной вход и домашнюю страницу нельзя менять")
            selected["enabled"], selected["access"] = enabled, access
            if domain not in declared:
                if upstream is not None: selected["upstream"] = upstream
                if name is not None: selected["name"] = name
            elif upstream is not None or name is not None:
                raise ValueError("Назначения из Git нельзя менять")
        rows = _validate_routes(rows, policy)
        candidate = _render(rows, policy, template)
        yaml_bytes = yaml.safe_dump(rows, allow_unicode=True, sort_keys=False).encode()
        # Кандидат читается через уже подключённый контейнер Caddy без изменения
        # работающей конфигурации. Изображения страницы и секреты не используются.
        proposed_site = config / ".candidate-routes.caddy"
        proposed_main = config / ".candidate-Caddyfile"
        if proposed_site.is_symlink() or proposed_main.is_symlink():
            raise RuntimeError("Подмена проверочных файлов")
        base = main.read_text(encoding="utf-8")
        if base.count("import sites.generated.caddy") != 1:
            raise RuntimeError("Неожиданный основной файл Caddy")
        try:
            _atomic(proposed_site, candidate)
            _atomic(proposed_main, base.replace(
                "import sites.generated.caddy", "import .candidate-routes.caddy"
            ).encode())
            _command(["docker", "exec", CONTAINER, "caddy", "validate",
                      "--config", "/etc/caddy/.candidate-Caddyfile"])
            if yaml_bytes == original_stored and candidate == original_generated:
                return
            try:
                _atomic(stored, yaml_bytes)
                _atomic(generated, candidate)
                _command(["docker", "exec", CONTAINER, "caddy", "reload",
                          "--config", "/etc/caddy/Caddyfile"])
                _refresh_apps(state, config)
            except (OSError, RuntimeError):
                _atomic(stored, original_stored)
                _atomic(generated, original_generated)
                _command(["docker", "exec", CONTAINER, "caddy", "reload",
                          "--config", "/etc/caddy/Caddyfile"])
                _refresh_apps(state, config)
                raise RuntimeError("Новые настройки отклонены; прежние возвращены")
        finally:
            proposed_site.unlink(missing_ok=True)
            proposed_main.unlink(missing_ok=True)


def change_route(state: Path, config: Path, domain: str, enabled: bool, access: str) -> None:
    """Совместимость с прежним действием переключения маршрута."""
    apply_route(state, config, "update", domain, enabled=enabled, access=access)
