"""Штатные операции над физическими серверами без создания объектов PVE."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import yaml

from .common import InfraManagerError, console
from .physical_hosts import list_physical_hosts, load_physical_host
from .physical_host_ssh import PhysicalHostSshSession
from .physical_host_bootstrap import bootstrap_physical_host
from .physical_storage import read_disk_inspection, verify_expected_disks
from .physical_nas_system import read_nas_system, require_omv8
from .physical_nas_ansible import configure_nas
from .common import run


SUPPORTED_HOST_OPERATIONS = frozenset({"hosts", "status", "inspect", "deploy", "repair", "bootstrap"})


def require_host_access(repo_root: Path, host_name: str) -> None:
    """Проверить явное машинное право 910 на конкретный физический сервер."""

    path = repo_root / "infrastructure" / "security" / "access.yaml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InfraManagerError("Не удалось прочитать политику доступа проекта") from exc
    rules = document.get("rules") if isinstance(document, dict) else None
    if not isinstance(rules, list):
        raise InfraManagerError("Политика доступа не содержит корректных rules")
    matched = [
        rule for rule in rules if isinstance(rule, dict)
        and rule.get("subject") == "guest:910"
        and rule.get("service") == "ssh"
        and rule.get("resource") == "host"
        and "connect-root" in rule.get("access", [])
        and f"host:{host_name}" in rule.get("targets", [])
    ]
    if len(matched) != 1:
        raise InfraManagerError(
            f"Доступ guest:910 к host:{host_name} не разрешён однозначно в access.yaml"
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
    host = load_physical_host(repo_root, name)
    require_host_access(repo_root, name)
    console.info(f"Физический сервер: {host.name}")
    if host.address is None:
        raise InfraManagerError(
            f"{host.directory / 'host.yaml'}: укажите подтверждённый сетевой адрес NAS; "
            "автоматический поиск соседних машин запрещён"
        )
    if host.ssh_host_fingerprint is None:
        raise InfraManagerError(
            f"{host.directory / 'host.yaml'}: не указан подтверждённый SSH-отпечаток "
            "физического сервера; нельзя доверять найденному ключу автоматически"
        )

    if operation == "bootstrap":
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError("Не определён доверенный узел PVE")
        payload = sys.stdin.readline(16385)
        if not payload or len(payload) > 16384 or not payload.endswith("\n"):
            raise InfraManagerError("Некорректный закрытый ввод для первого подключения")
        bootstrap_physical_host(host, pve_node, payload)
        console.result("Начальное доверие NAS установлено, вход OpenBao подтверждён")
        return 0

    if operation in {"inspect", "status", "deploy", "repair"}:
        pve_node = os.environ.get("INFRA_PVE_NODE", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.:-]{0,252}", pve_node):
            raise InfraManagerError(
                "Не определён доверенный PVE-узел для подписи временного SSH-ключа"
            )
        console.info("Проверка SSH-ключа NAS и временного сертификата OpenBao")
        with PhysicalHostSshSession(host, pve_node) as session:
            system = read_nas_system(session)
            console.info(
                f"ОС: Debian {system.debian}, OpenMediaVault "
                f"{system.omv_version or 'не установлена'}"
            )
            result = run(
                session.command(
                    "lsblk --json --bytes --output "
                    "NAME,PATH,MODEL,SERIAL,SIZE,TYPE,FSTYPE,UUID,PTTYPE,MOUNTPOINTS"
                ),
                capture_output=True,
            )
            disks = read_disk_inspection(result.stdout)
            if operation in {"deploy", "repair"}:
                require_omv8(system)
                verify_expected_disks(host.devices, disks)
                configure_nas(repo_root, session)
                # Не объявлять весь NAS развёрнутым после одной базовой роли.
                raise InfraManagerError(
                    "Базовая настройка ОС завершена. Настройка хранилищ, "
                    "SMB и резервного копирования ещё не реализована; "
                    "существующие диски не изменялись"
                )
        expected_serials = {
            item["serial"]: item["role"]
            for item in host.devices if item["serial"] is not None
        }
        for disk in disks:
            identity = disk.serial or "серийный номер отсутствует"
            role = expected_serials.get(disk.serial, "роль не подтверждена")
            condition = "системный" if disk.is_system else (
                "с существующей разметкой" if disk.contains_existing_layout
                else "без обнаруженной разметки"
            )
            console.info(
                f"{disk.path}: {disk.model} | {disk.capacity_bytes // 10**9} ГБ "
                f"| {identity} | {condition} | {role}"
            )
        if operation == "status":
            verify_expected_disks(host.devices, disks)
            console.result("NAS доступен по доверенному SSH, назначенные диски найдены")
        else:
            console.result("Чтение сведений о накопителях NAS завершено без изменений")
        return 0

    raise InfraManagerError(f"Неизвестная операция NAS: {operation}")
