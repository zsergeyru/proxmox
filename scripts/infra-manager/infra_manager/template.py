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


def _agent_enabled(config: dict[str, Any]) -> bool:
    agent = str(config.get("agent") or "")
    return (
        agent == "1"
        or agent.startswith("1,")
        or re.search(r"(^|,)enabled=1($|,)", agent) is not None
    )


def _template_failures(config: dict[str, Any]) -> list[str]:
    """Вернуть нарушения базового контракта шаблона."""
    description = str(config.get("description") or "")
    agent = str(config.get("agent") or "")
    checks = (
        (
            is_truthy(config.get("template", 0)),
            f"template={config.get('template', '<отсутствует>')}",
        ),
        (
            config.get("name") == "tpl-debian13",
            f"name={config.get('name', '<отсутствует>')}",
        ),
        (
            f"template-version={TEMPLATE_VERSION}" in description,
            f"description={config.get('description', '<отсутствует>')}",
        ),
        (
            is_truthy(config.get("protection", 0)),
            f"protection={config.get('protection', '<отсутствует>')}",
        ),
        (
            config.get("scsihw") == "virtio-scsi-single",
            f"scsihw={config.get('scsihw', '<отсутствует>')}",
        ),
        (
            _agent_enabled(config),
            f"agent={agent or '<отсутствует>'}",
        ),
        (
            "cloudinit" in str(config.get("ide0") or ""),
            "ide0=<cloudinit отсутствует>",
        ),
        (
            config.get("ciuser") == "root",
            f"ciuser={config.get('ciuser', '<отсутствует>')}",
        ),
        (
            "ip=dhcp" in str(config.get("ipconfig0") or ""),
            f"ipconfig0={config.get('ipconfig0', '<отсутствует>')}",
        ),
        (
            str(config.get("ciupgrade", 0)).lower() in {"0", "false"},
            f"ciupgrade={config.get('ciupgrade')}",
        ),
    )
    return [message for passed, message in checks if not passed]


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
