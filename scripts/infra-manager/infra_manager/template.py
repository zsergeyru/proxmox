"""Общий контракт Packer-шаблонов Proxmox."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console
from .pve import PveClient

TEMPLATE_VERSION = 8


def is_truthy(value: Any) -> bool:
    return value in (1, True, "1", "true")


def _template_failures(config: dict[str, Any]) -> list[str]:
    """Вернуть нарушения базового контракта шаблона."""

    failures: list[str] = []

    if not is_truthy(config.get("template", 0)):
        failures.append(f"template={config.get('template', '<отсутствует>')}")
    if config.get("name") != "tpl-debian13":
        failures.append(f"name={config.get('name', '<отсутствует>')}")
    if f"template-version={TEMPLATE_VERSION}" not in str(
        config.get("description") or ""
    ):
        failures.append(
            f"description={config.get('description', '<отсутствует>')}"
        )
    if not is_truthy(config.get("protection", 0)):
        failures.append(
            f"protection={config.get('protection', '<отсутствует>')}"
        )
    if config.get("scsihw") != "virtio-scsi-single":
        failures.append(
            f"scsihw={config.get('scsihw', '<отсутствует>')}"
        )

    agent = str(config.get("agent") or "")
    if not (
        agent == "1"
        or agent.startswith("1,")
        or re.search(r"(^|,)enabled=1($|,)", agent)
    ):
        failures.append(f"agent={agent or '<отсутствует>'}")

    if "cloudinit" not in str(config.get("ide0") or ""):
        failures.append("ide0=<cloudinit отсутствует>")
    if config.get("ciuser") != "root":
        failures.append(
            f"ciuser={config.get('ciuser', '<отсутствует>')}"
        )
    if "ip=dhcp" not in str(config.get("ipconfig0") or ""):
        failures.append(
            f"ipconfig0={config.get('ipconfig0', '<отсутствует>')}"
        )
    if str(config.get("ciupgrade", 0)).lower() not in {"0", "false"}:
        failures.append(f"ciupgrade={config.get('ciupgrade')}")

    return failures


def verify_template_contract(
    client: PveClient,
    *,
    node: str,
    vmid: int,
) -> None:
    """Проверить конфигурационный контракт готового шаблона."""

    config = client.vm_config(node=node, vmid=vmid)
    failures = _template_failures(config)
    if failures:
        details = "\n".join(f"  - {item}" for item in failures)
        raise InfraManagerError(
            f"Шаблон {vmid} не соответствует базовому контракту:\n"
            f"{details}\n"
            f"Фактическая конфигурация: "
            f"{json.dumps(config, ensure_ascii=False, sort_keys=True)}"
        )
    console.ok(f"Контракт шаблона {vmid} подтверждён")


def run_build_template(repo_root: Path, vmid: int) -> int:
    """Совместимый вход в отдельный сценарий сборки шаблона."""

    from .template_build import run_build_template as run_build

    return run_build(repo_root, vmid)


def run_verify_template(vmid: int) -> int:
    """Совместимый вход в отдельный сценарий проверки шаблона."""

    from .template_verify import run_verify_template as run_verify

    return run_verify(vmid)
