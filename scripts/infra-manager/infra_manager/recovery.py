"""Подготовка и проверка аварийного PVE-only контура infra-manager."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .common import InfraManagerError, console
from .pve_host import (
    check_recovery_contour,
    install_operator_host_support,
    install_recovery_host_support,
    prepare_recovery_git,
)

OPERATOR_COMMAND = Path("/usr/local/sbin/infra-manager")


def pve_node_from_environment() -> str:
    explicit = os.environ.get("INFRA_PVE_NODE", "").strip()
    if explicit:
        if any(character.isspace() for character in explicit) or "/" in explicit:
            raise InfraManagerError("INFRA_PVE_NODE имеет некорректный формат")
        return explicit

    endpoint = os.environ.get("TF_VAR_pve_endpoint", "").strip()
    if not endpoint:
        raise InfraManagerError(
            "Не задан INFRA_PVE_NODE или TF_VAR_pve_endpoint"
        )

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise InfraManagerError(
            f"Некорректный адрес PVE в TF_VAR_pve_endpoint: {endpoint!r}"
        )
    return parsed.hostname


def prepare_recovery(repo_root: Path) -> int:
    """Установить PVE helper и подготовить аварийную Git-копию."""
    node = pve_node_from_environment()
    console.info("Подготовка аварийного контура infra-manager")
    install_recovery_host_support(node, repo_root)
    prepare_recovery_git(node)
    console.result("PVE recovery helper и аварийный Git-доступ подготовлены")
    return 0


def install_operator_wrapper(repo_root: Path) -> int:
    """Установить тонкую PVE-оболочку после настройки управляющего гостя."""

    if not OPERATOR_COMMAND.is_file() or not os.access(OPERATOR_COMMAND, os.X_OK):
        raise InfraManagerError(
            f"Операторская команда управляющего гостя ещё не готова: "
            f"{OPERATOR_COMMAND}"
        )

    node = pve_node_from_environment()
    install_operator_host_support(node, repo_root)
    console.result("Тонкая операторская оболочка PVE установлена")
    return 0


def check_recovery() -> int:
    """Проверить полный recovery-контракт после инициализации OpenBao."""
    node = pve_node_from_environment()
    check_recovery_contour(node)
    console.result("Аварийный контур infra-manager полностью готов")
    return 0
