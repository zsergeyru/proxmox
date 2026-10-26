"""Штатные операции над физическими серверами без создания объектов PVE."""

from __future__ import annotations

import os
import json
import re
import sys
from pathlib import Path

import yaml

from .common import InfraManagerError, console
from .guest_catalog import find_guest_by_role
from .settings import SETTINGS
from .physical_hosts import list_physical_hosts, load_physical_host
from .physical_host_bootstrap import (
    bootstrap_physical_host, resolve_saved_host, confirm_redeploy_host_key,
)
from .physical_host_ssh import discover_first_host_key
from dataclasses import replace


SUPPORTED_HOST_OPERATIONS = frozenset({"hosts", "status", "storage-plan", "storage-prepare", "deploy", "repair", "bootstrap", "ssh-trust-plan", "ssh-trust-confirm"})


def require_host_access(repo_root: Path, host_name: str) -> None:
    """Проверить явное право управляющей роли на физический сервер."""

    path = repo_root / "infrastructure" / "security" / "access.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не удалось прочитать политику доступа проекта") from exc
    rules = document.get("rules") if isinstance(document, dict) else None
    if not isinstance(rules, list):
        raise InfraManagerError("Политика доступа не содержит корректных rules")
    identity = find_guest_by_role(repo_root, SETTINGS.infra_manager_role)
    subject = f"guest:{identity.vmid}"
    matched = [
        rule for rule in rules if isinstance(rule, dict)
        and rule.get("subject") == subject
        and rule.get("service") == "ssh"
        and rule.get("resource") == "host"
        and "connect-root" in rule.get("access", [])
        and f"host:{host_name}" in rule.get("targets", [])
    ]
    if len(matched) != 1:
        raise InfraManagerError(
            f"Доступ {subject} к host:{host_name} не разрешён однозначно в access.yaml"
        )


def list_hosts(repo_root: Path) -> int:
    """Показать перечень физических машин и готовность настроек связи."""

    hosts = list_physical_hosts(repo_root)
    if not hosts:
        console.warning("Машинные описания физических серверов отсутствуют")
        return 0
    for host in hosts:
        address = host.address or "адрес не указан"
        console.info(f"{host.name}: физический сервер, {address}")
    return 0


def run_physical_host_operation(repo_root: Path, operation: str, name: str) -> int:
    """Проверить описание цели; не выполнять необъявленных разрушительных действий."""

    if operation not in SUPPORTED_HOST_OPERATIONS:
        raise InfraManagerError(f"Недопустимая операция физического сервера: {operation}")
    if operation == "hosts":
        return list_hosts(repo_root)
    host = resolve_saved_host(load_physical_host(repo_root, name))
    require_host_access(repo_root, name)
    if operation in {"ssh-trust-plan", "ssh-trust-confirm"}:
        if not host.address or not host.ssh_host_fingerprint:
            raise InfraManagerError("Сначала требуется первоначальное SSH-доверие")
        if operation == "ssh-trust-plan":
            current = discover_first_host_key(replace(host, ssh_host_fingerprint=None))
            print("PHYSICAL_SSH_TRUST_JSON=" + json.dumps({
                "name": host.name, "address": host.address,
                "previous": host.ssh_host_fingerprint,
                "current": current.ssh_host_fingerprint,
                "changed": host.ssh_host_fingerprint != current.ssh_host_fingerprint,
            }, ensure_ascii=False, separators=(",", ":")))
            return 0
        payload = sys.stdin.readline(2048)
        if not payload or len(payload) > 2047 or not payload.endswith("\n"):
            raise InfraManagerError("Не получено подтверждение SSH-ключа")
        try:
            approved = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InfraManagerError("Некорректное подтверждение SSH-ключа") from exc
        if not isinstance(approved, dict) or set(approved) != {"fingerprint"}:
            raise InfraManagerError("Некорректная форма подтверждения SSH-ключа")
        changed = confirm_redeploy_host_key(host, approved["fingerprint"])
        console.result("SSH-ключ обновлён после подтверждения" if changed else
                       "SSH-ключ физического сервера подтверждён")
        return 0
    if operation == "bootstrap":
        # One generic OpenBao/SSH trust bootstrap for ALL physical servers.
        # A host-specific deployment driver must never own initial access.
        if not host.address:
            raise InfraManagerError("Не указан проверенный адрес физического сервера")
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError("Не определён доверенный узел PVE")
        payload = sys.stdin.readline(16385)
        if not payload or len(payload) > 16384 or not payload.endswith("\n"):
            raise InfraManagerError("Некорректный закрытый ввод первого подключения")
        bootstrap_physical_host(host, pve_node, payload)
        console.result("SSH-доверие физического сервера установлено через OpenBao")
        return 0
    # No dynamic Python driver and no service-specific branch in 910.
    # Host-owned playbooks, phase order and status checks are Git data.
    from .physical_host_ssh import PhysicalHostSshSession
    from .physical_host_ansible import run_physical_ansible
    from .physical_host_credentials import operator_status_sections
    if host.engine != "ansible" or not host.phases:
        raise InfraManagerError("Физический сервер не объявил Ansible workflow")
    if not host.address or not host.ssh_host_fingerprint:
        raise InfraManagerError(
            "Для физического сервера нужен ранее подтверждённый SSH-ключ"
        )
    pve_node = os.environ.get("INFRA_PVE_NODE", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
        raise InfraManagerError("Не определён доверенный PVE-узел для OpenBao SSH")
    variables: dict[str, object] = {}
    if operation == "storage-prepare":
        payload = sys.stdin.readline(2048)
        if not payload or len(payload) > 2047 or not payload.endswith("\n"):
            raise InfraManagerError("Требуется подтверждение точного плана дисков")
        try:
            confirmation = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise InfraManagerError("Повреждено подтверждение дисков") from exc
        if (not isinstance(confirmation, dict)
                or set(confirmation) != {"fingerprint"}
                or not isinstance(confirmation["fingerprint"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", confirmation["fingerprint"])):
            raise InfraManagerError("Отпечаток плана дисков не подтверждён")
        variables["physical_approved_fingerprint"] = confirmation["fingerprint"]
        # PVE operator approval is a one-off transport flag, not a YAML choice.
        variables["physical_disk_recreate_authorized"] = True
    with PhysicalHostSshSession(host, pve_node) as session:
        if operation in {"deploy", "repair"}:
            for phase in host.phases:
                run_physical_ansible(
                    repo_root, session, variables=variables,
                    operation="deploy", phase=phase, callback=host.callback,
                )
            run_physical_ansible(
                repo_root, session, variables=variables,
                operation="status", callback=host.callback,
            )
        elif operation == "status":
            run_physical_ansible(
                repo_root, session, variables=variables,
                operation="status", callback=host.callback,
            )
        elif operation in {"storage-plan", "storage-prepare"}:
            run_physical_ansible(
                repo_root, session, variables=variables,
                operation=operation,
                callback="infra_storage_plan" if operation == "storage-plan"
                else host.callback,
            )
        else:
            raise InfraManagerError("Неизвестная декларативная операция сервера")
    if operation in {"deploy", "repair", "status"}:
        console.result(f"{host.name}: Ansible и status.yaml проверены")
        operator_status_sections(
            repo_root, host.name, host.address,
            show_secrets=os.environ.get("INFRA_MANAGER_OPERATOR_SHOW_SECRETS") == "1",
        )
    return 0
