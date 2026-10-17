#!/usr/bin/env python3
"""Согласованность развёртывания 140: две страницы, Authelia и Caddy."""
from __future__ import annotations

import ast
from pathlib import Path
import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure/guests/140-web-gateway"
ROLE = ROOT / "automation/ansible/roles/web_gateway"


def check() -> None:
    guest = yaml.safe_load((GUEST / "guest.yaml").read_text(encoding="utf-8"))
    provision = yaml.safe_load((GUEST / "provision.yaml").read_text(encoding="utf-8"))
    status = yaml.safe_load((GUEST / "status.yaml").read_text(encoding="utf-8"))
    web = provision["web_gateway"]
    assert guest["vmid"] == provision["guest_vmid"] == status["guest_vmid"] == 140
    assert guest["profile"] == "debian-lxc-docker"
    assert provision["pve_storage"]["mounts"] == [{
        "slot":"mp0","volume":"infra_state","directory":"140-web-gateway",
        "target":"/var/lib/web-gateway","read_only":False,
    }]
    assert web["storage"]["allow_missing_mount"] is False
    assert web["portal"]["enabled"] is True
    assert web["portal"]["authentication"] == "authelia"
    assert web["portal"]["second_factor"] is True
    assert web["portal"]["administration"]["groups"] == ["admins"]
    assert web["portal"]["administration"]["private_networks"] is True
    assert "homarr" not in web and "portal" not in provision
    assert set(web["authelia"]["authentication"]["groups"]) == {"admins","family","guests"}
    assert web["authelia"]["oidc"]["clients"] == ["immich"]
    assert web["paths"]["secrets"].startswith(web["paths"]["persistent_state"] + "/")
    assert web["paths"]["persistent_state"] in provision["persistence"]["backup_required"]

    environment=Environment(
        loader=FileSystemLoader(str(ROLE/"templates")),
        undefined=StrictUndefined, keep_trailing_newline=True,
    )
    environment.filters["bool"]=bool
    context={"provision":provision,"web_gateway_routes":web["caddy"]["routes"]}
    compose=yaml.safe_load(environment.get_template("docker-compose.yml.j2").render(**context))
    assert set(compose["services"]) == {"caddy","authelia"}
    assert "homarr" not in compose["services"]
    assert compose["services"]["caddy"]["image"] == "caddy:2.11.7"
    assert compose["services"]["authelia"]["image"] == "authelia/authelia:4.39.28"
    assert any("/run/portal:ro" in volume for volume in compose["services"]["caddy"]["volumes"])
    assert any("/etc/caddy/portal:ro" in volume for volume in compose["services"]["caddy"]["volumes"])
    for service in compose["services"].values():
        assert not any("docker.sock" in str(v) for v in service.get("volumes", []))
    assert all("7575" not in str(x) for x in compose["services"]["caddy"]["ports"])

    caddy=environment.get_template("Caddyfile.j2").render(**context)
    routes=environment.get_template("sites.generated.caddy.j2").render(**context)
    assert "remote_ip 192.168.0.0/16" in caddy
    assert "forward_auth authelia:9091" in routes
    assert "@admin_ui_lan" in routes and "@admin_api_lan" in routes
    assert "header_up X-Portal-Verified admin" in routes
    assert "rewrite * /portal-home.html" in routes
    assert "rewrite * /portal-admin.html" in routes
    assert "http://homarr:7575" not in routes
    assert "script-src 'self'" in routes
    auth=environment.get_template("authelia-configuration.yml.j2").render(**context)
    access=yaml.safe_load(auth.split("\nidentity_providers:",1)[0])["access_control"]["rules"]
    assert access[0]["subject"] == ["group:admins"]
    assert access[0]["policy"] == "two_factor"
    assert access[1]["policy"] == "deny" and access[1]["resources"] == access[0]["resources"]
    assert access[2]["policy"] == "two_factor"

    tasks=(ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    sync=(ROLE / "tasks/sync.yml").read_text(encoding="utf-8")
    assert tasks.index("Проверить настоящее подключение постоянного тома") < tasks.index("Установить готовые страницы")
    assert tasks.index("Заменить только старые назначения Homarr") < tasks.index("Получить действующие настройки доменов")
    assert tasks.index("Запустить веб-шлюз") < tasks.index("Запустить закрытый интерфейс домашней")
    assert "docker" in tasks and "--remove-orphans" in tasks
    # Раздел Homarr удалён из provision.yaml: обращение к нему роняет Ansible.
    assert "provision.web_gateway.homarr" not in tasks
    assert all(key in tasks for key in (
        "portal-web.py","migrate-portal-routes.py","portal-home.html",
        "portal-admin.html","portal.css","portal.js","home-apps.py",
        "web-gateway-portal.service","admin-api.py","initialize-totp.py",
    ))
    assert "home-apps.py" in sync
    assert "infra_state" in provision["pve_storage"]["volumes"][0]["name"]
    files={r["path"] for r in status["checks"] if r["type"]=="file"}
    assert "/etc/web-gateway/portal/portal-home.html" in files
    assert "/etc/web-gateway/portal/portal-admin.html" in files
    containers={r["container"] for r in status["checks"] if r["type"]=="docker"}
    assert containers=={
        compose["services"]["caddy"]["container_name"],
        compose["services"]["authelia"]["container_name"],
    }
    for name in ("portal-web.py","migrate-portal-routes.py","admin-api.py","initialize.py"):
        source=ROLE / "files" / name
        ast.parse(source.read_text(encoding="utf-8"),filename=str(source))
    print("140: Caddy и Authelia на двух страницах, mp0 защищён, Homarr не запускается")


if __name__ == "__main__":
    check()
