"""Первичная инициализация OpenBao через доверенный PVE-хост."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .common import InfraManagerError, console
from .pve_host import (
    initialize_openbao_on_host,
    install_openbao_host_support,
)


def _pve_node_from_environment() -> str:
    endpoint = os.environ.get("TF_VAR_pve_endpoint", "").strip()
    if not endpoint:
        raise InfraManagerError("Не задан TF_VAR_pve_endpoint")

    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise InfraManagerError(
            f"Некорректный адрес PVE в TF_VAR_pve_endpoint: {endpoint!r}"
        )
    return parsed.hostname


def run_initialize_openbao(repo_root: Path) -> int:
    """Инициализировать OpenBao и обеспечить два SSH-центра доверия."""
    node = _pve_node_from_environment()

    console.info("Установка сценария разблокировки OpenBao на PVE")
    install_openbao_host_support(node, repo_root)

    console.info("Инициализация OpenBao и проверка SSH-центров доверия")
    initialize_openbao_on_host(node)

    console.ok("OpenBao готов; два SSH-центра доверия и разблокировка настроены")
    return 0
