#!/usr/bin/env python3
"""Проверить машинные файлы и шаблоны веб-шлюза 140 без Docker и секретов."""

from __future__ import annotations

from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]
GUEST_DIR = ROOT / "infrastructure/guests/140-web-gateway"
ROLE = ROOT / "automation/ansible/roles/web_gateway"
PLAYBOOK = ROOT / "automation/ansible/playbooks/configure-guest.yml"


def require(condition: bool, message: str) -> None:
    """Остановить проверку при нарушении контракта."""
    if not condition:
        raise AssertionError(message)


def load(path: Path) -> dict:
    """Загрузить YAML с отображением точного пути ошибки."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise AssertionError(f"{path}: {exc}") from exc
    require(isinstance(data, dict), f"{path}: ожидался YAML-объект")
    return data


def test() -> None:
    """Проверить маршруты, постоянное состояние, контейнеры и роль."""
    guest = load(GUEST_DIR / "guest.yaml")
    provision = load(GUEST_DIR / "provision.yaml")
    checks = load(GUEST_DIR / "status.yaml")
    storage = provision["pve_storage"]
    web = provision["web_gateway"]

    require(guest["vmid"] == provision["guest_vmid"] == checks["guest_vmid"] == 140,
            "VMID машинных описаний различается")
    require(guest["profile"] == "debian-lxc-docker", "140 должен быть LXC Docker")
    require("portal" not in provision, "140 использует Homarr, а не Homepage")
    require(storage["volumes"] == [{
        "name": "infra_state",
        "size_gb": 40,
        "mountpoint": "/mnt/bindmounts/infra-state",
    }], "Нельзя создавать новый том вместо общего infra_state")
    require(storage["mounts"] == [{
        "slot": "mp0",
        "volume": "infra_state",
        "directory": "140-web-gateway",
        "target": "/var/lib/web-gateway",
        "read_only": False,
    }], "Постоянное состояние должно иметь отдельное подключение mp0")
    require(web["storage"]["allow_missing_mount"] is False,
            "Нельзя запускать приложения без постоянного тома")
    require(web["paths"]["persistent_state"] in provision["persistence"]["backup_required"],
            "Не перечислен обязательный каталог резервирования")

    environment = Environment(
        loader=FileSystemLoader(str(ROLE / "templates")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    environment.filters["bool"] = bool
    variables = {"provision": provision, "web_gateway_routes": web["caddy"]["routes"]}

    compose = yaml.safe_load(environment.get_template("docker-compose.yml.j2").render(**variables))
    services = compose["services"]
    require(set(services) == {"caddy", "authelia", "homarr"},
            "Состав Docker Compose отличается от контракта")
    for name, expected in [
        ("caddy", web["caddy"]),
        ("authelia", web["authelia"]),
        ("homarr", web["homarr"]),
    ]:
        require(services[name]["image"] == expected["image"],
                f"Неверный образ {name}")
        require("latest" not in services[name]["image"],
                f"Образ {name} должен иметь закреплённую версию")
    require(not any(
        "/var/run/docker.sock" in volume
        for service in services.values()
        for volume in service.get("volumes", [])
    ), "Контейнеры 140 не должны получать Docker socket")
    require(all(
        "0.0.0.0:" not in str(port)
        for port in services["homarr"]["ports"]
    ), "Homarr нельзя публиковать в домашнюю сеть отдельным портом")
    require(any(
        str(port).startswith("127.0.0.1:")
        for port in services["homarr"]["ports"]
    ), "Для диагностики Homarr допускается только локальный порт")
    require(set(services["authelia"]["environment"]) >= {
        "AUTHELIA_SESSION_SECRET_FILE",
        "AUTHELIA_STORAGE_ENCRYPTION_KEY_FILE",
        "AUTHELIA_IDENTITY_PROVIDERS_OIDC_HMAC_SECRET_FILE",
        "AUTHELIA_IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET_FILE",
    }, "Authelia должна получать секреты из защищённых файлов")
    require(services["homarr"]["env_file"] == [web["paths"]["secrets"] + "/homarr.env"],
            "Homarr должен использовать защищённый файл параметров")

    routes = environment.get_template("sites.generated.caddy.j2").render(**variables)
    require("home.zsergey.ru" in routes and "auth.zsergey.ru" in routes,
            "Нет основных доменов")
    require("photos.zsergey.ru" not in routes and "ai.zsergey.ru" not in routes,
            "Непроверенные службы не должны публиковаться по умолчанию")

    authelia = environment.get_template("authelia-configuration.yml.j2").render(**variables)
    require("{{ secret " in authelia,
            "Authelia должна получать JWKS и хеш OIDC-клиента из файлов")
    require("client_secret:" in authelia and "authorization_policy: two_factor" in authelia,
            "Нужен защищённый OIDC-вход")
    require("/data/db.sqlite3" in authelia,
            "SQLite Authelia должна лежать на постоянном томе")

    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    require(isinstance(tasks, list), "Роль Ansible должна содержать список задач")
    # Проверяем порядок: mountpoint должен предшествовать созданию каталогов.
    text = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    require(text.find("mountpoint") < text.find("state: directory"),
            "Необходимо проверить реальное подключение до создания каталогов")
    require("no_log: true" in text and "force: false" in text,
            "Нельзя раскрывать секреты или перезаписывать сохранённые маршруты")
    require("authelia.validate" not in text,
            "Неожиданная временная команда")


    status_names = {item["container"] for item in checks["checks"] if item["type"] == "docker"}
    compose_names = {item["container_name"] for item in services.values()}
    require(status_names == compose_names,
            "status.yaml должен проверять контейнеры из Compose")

    playbook = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    require("name: web_gateway" in str(playbook),
            "Общая настройка Linux должна подключать роль web_gateway")
    print("140: машинные YAML и шаблоны согласованы")


if __name__ == "__main__":
    test()
