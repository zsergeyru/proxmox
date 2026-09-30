"""Подготовка и проверка аварийного PVE-only контура 910."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .common import InfraManagerError, console
from .pve_host import (
    check_recovery_contour,
    install_recovery_host_support,
    prepare_recovery_git,
)


def pve_node_from_environment() -> str:
    endpoint = os.environ.get("TF_VAR_pve_endpoint", "").strip()
    if not endpoint:
        raise InfraManagerError("Не задан TF_VAR_pve_endpoint")

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise InfraManagerError(
            f"Некорректный адрес PVE в TF_VAR_pve_endpoint: {endpoint!r}"
        )
    return parsed.hostname


def prepare_recovery(repo_root: Path) -> int:
    """Установить PVE helper и подготовить аварийную Git-копию."""
    node = pve_node_from_environment()
    console.info("Подготовка аварийного контура 910")
    install_recovery_host_support(node, repo_root)
    prepare_recovery_git(node)
    console.result("PVE recovery helper и аварийный Git-доступ подготовлены")
    return 0


def check_recovery() -> int:
    """Проверить полный recovery-контракт после инициализации OpenBao."""
    node = pve_node_from_environment()
    check_recovery_contour(node)
    console.result("Аварийный контур 910 полностью готов")
    return 0
