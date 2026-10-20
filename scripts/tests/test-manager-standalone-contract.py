#!/usr/bin/env python3
"""Сквозной контракт первоначального запуска 910 без Semaphore.

Проверяются только действующие настройки; сохранённые переходные файлы
не являются источниками развёртывания или критерием готовности.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure/guests/910-infra-manager"
COMPOSE = GUEST / "rootfs/opt/infra-manager/compose"
ANSIBLE = ROOT / "automation/ansible/roles/infra_manager/tasks"


def read_yaml(path: Path) -> dict:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(result, dict), path
    return result


def verify() -> None:
    provision = read_yaml(GUEST / "provision.yaml")
    assert provision["schema_version"] == 1
    assert provision["guest_vmid"] == 910
    assert provision["portal"]["local"]["enabled"] is False
    docker = provision["docker"]["services"]
    assert set(docker) == {"runtime", "openbao", "web"}
    runtime = docker["runtime"]
    assert runtime["mode"] == "standalone"
    assert "database" not in runtime and "base_image" not in runtime
    assert runtime["container_name"] == "infra-runtime"
    assert runtime["tools"]["ansible"] == "2.19.4"
    assert runtime["tools"]["opentofu"] == "1.12.6"
    assert runtime["tools"]["packer"] == "1.15.4"
    assert len(runtime["tools"]["packer_image"].split("@sha256:")[-1]) == 64
    assert not any("semaphore" in item.lower() for item in runtime["mounts"])
    assert "/run/infra-manager/secrets:/run/infra-manager/secrets:ro" in runtime["mounts"]
    web = docker["web"]
    assert web["enabled"] is True
    assert web["scheme"] == "https" and web["port"] == 8443
    assert web["auth"] == "openbao-userpass"
    openbao = docker["openbao"]
    assert openbao["storage"] == "raft" and openbao["auto_initialize"] is False

    state = provision["persistence"]["target_layout"]
    assert state["host_root"] == "/mnt/bindmounts/infra-manager"
    assert state["pve_only"]["mounted_into_guest"] is False
    assert state["access"]["mode"] == "ro"
    assert state["state"]["mode"] == "rw"
    assert "operation-records" in state["state"]["contains"]
    assert "openbao-raft" in state["state"]["contains"]
    assert not any("semaphore" in repr(x).lower() for x in state["state"]["bindings"])
    assert "/mnt/persistent-state/operations" in provision["persistence"]["backup_required"]

    compose = read_yaml(COMPOSE / "docker-compose.yml")["services"]
    assert set(compose) == {"runtime", "openbao", "web"}
    assert compose["runtime"]["build"]["dockerfile"] == "runtime-next/Dockerfile"
    assert "env_file" not in compose["runtime"]
    assert "/mnt/persistent-state/openbao:/openbao/file" in compose["openbao"]["volumes"]
    assert compose["openbao"]["command"] == ["server"]
    web_compose = compose["web"]
    assert "profiles" not in web_compose
    assert web_compose["read_only"] is True
    assert web_compose["user"] == "1001:0"
    assert "ALL" in web_compose["cap_drop"]
    assert "no-new-privileges:true" in web_compose["security_opt"]
    assert "/mnt/persistent-state/operations:/srv/operations:ro" in web_compose["volumes"]
    assert "/etc/infra-manager/web/tls:/etc/infra-manager/web/tls:ro" in web_compose["volumes"]
    assert "/run/infra-web:/run/infra-web:ro" in web_compose["volumes"]
    assert not any("docker.sock" in x or "pve-only" in x for x in web_compose["volumes"])
    assert all("semaphore" not in x.lower() for x in compose["runtime"]["volumes"])

    image = (COMPOSE / "runtime-next/Dockerfile").read_text(encoding="utf-8")
    assert "FROM python:3.12" in image and "ansible-core==2.19.4" in image
    assert "standalone-v1" in image
    assert "COPY --from=packer_source" in image
    assert "FROM semaphoreui" not in image
    assert "tofu_" in image
    requirements = (COMPOSE / "runtime/requirements.txt").read_text(encoding="utf-8")
    for package in ("proxmoxer", "requests", "PyYAML", "jsonschema"):
        assert package in requirements
    assert "COPY runtime/requirements.txt" in image

    main = (ANSIBLE / "main.yml").read_text(encoding="utf-8")
    assert "import_tasks: runtime_prerequisites.yml" in main
    assert "import_tasks: runtime.yml" in main
    assert "import_tasks: semaphore.yml" not in main
    runtime_tasks = (ANSIBLE / "runtime.yml").read_text(encoding="utf-8")
    for item in (
        "RUNTIME_MODE=standalone", "INFRA_WEB_PUBLIC_HOST=",
        "infra-manager-operation-reconcile.service",
        "infra-manager-backup.timer", "infra-manager-portal-gateway.service",
        "infra-manager-openbao-startup-unseal.service",
        "subjectAltName=IP:", "/mnt/persistent-state",
        "docker-compose.yml", "infra-manager-status",
    ):
        assert item in runtime_tasks, item
    assert "provision.paths.semaphore_persistent_data" not in runtime_tasks
    assert "SEMAPHORE_VERSION" not in runtime_tasks
    assert "semaphore-project" not in runtime_tasks
    verify_tasks = (ANSIBLE / "verify.yml").read_text(encoding="utf-8")
    assert "docker, exec, infra-runtime, ansible, --version" in verify_tasks
    assert "semaphore-project" not in verify_tasks

    status = read_yaml(GUEST / "infra-manager-status-standalone.yaml")
    types = {check["type"] for check in status["checks"]}
    assert {"runtime", "infra_web", "openbao", "runtime_tools", "pve_access"} <= types
    assert "semaphore" not in types and "portal" not in types

    portal_unit = (
        GUEST / "rootfs/etc/systemd/system/infra-manager-portal-gateway.service"
    ).read_text(encoding="utf-8")
    assert "--bind 127.0.0.1" in portal_unit
    assert "RuntimeDirectory=infra-web" in portal_unit
    backup = read_yaml(ROOT / "infrastructure/hosts/pve/backup.yaml")
    assert [item["id"] for item in backup["destinations"]] == ["fast-ssd", "nas-backup"]
    assert not any(item.get("uuid") or item.get("source") for item in backup["destinations"])

    bootstrap = (
        ROOT / "scripts/bootstrap-runner/bootstrap_runner/persistence.py"
    ).read_text(encoding="utf-8")
    assert "host_state_semaphore_db" not in bootstrap
    assert "host_state_openbao_raft_dir" in bootstrap
    assert "host_recovery_github_key" in bootstrap
    assert "host_state_opentofu_file" in bootstrap

    access = read_yaml(ROOT / "infrastructure/security/access.yaml")
    assert not any(rule.get("service") == "semaphore" for rule in access["rules"])
    print("[ОК] Первоначальная установка 910 и аварийное восстановление не требуют Semaphore")


if __name__ == "__main__":
    try:
        verify()
    except (AssertionError, KeyError) as exc:
        raise SystemExit(f"Контракт самостоятельного 910 нарушен: {exc}") from exc
