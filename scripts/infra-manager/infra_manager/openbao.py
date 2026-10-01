"""Первичная инициализация OpenBao через доверенный PVE-хост."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .access import load_access_policy
from .common import InfraManagerError, console
from .pve_host import (
    check_recovery_contour,
    cleanup_transition_state,
    initialize_openbao_on_host,
    install_openbao_host_support,
    install_recovery_host_support,
    prepare_recovery_git,
    sync_openbao_otp_contract,
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


def _otp_sources(repo_root: Path) -> list[dict[str, object]]:
    policy = load_access_policy(repo_root)
    sources: list[dict[str, object]] = []

    source_vmids = sorted(
        {
            edge.source_vmid
            for edge in policy.machine_ssh_edges
        }
    )
    for source_vmid in source_vmids:
        source = policy.guests[source_vmid]
        if source.address is None:
            raise InfraManagerError(
                f"guest:{source_vmid}: для OpenBao OTP нужен доверенный статический адрес"
            )

        target_cidrs: list[str] = []
        for target_vmid in policy.targets_for(source_vmid):
            target = policy.guests[target_vmid]
            if target.address is None:
                raise InfraManagerError(
                    f"guest:{target_vmid}: OTP-цель не имеет доверенного статического адреса"
                )
            target_cidrs.append(f"{target.address}/32")

        if not target_cidrs:
            continue

        sources.append(
            {
                "vmid": source_vmid,
                "source_cidr": f"{source.address}/32",
                "target_cidrs": sorted(set(target_cidrs)),
            }
        )

    return sources


def run_initialize_openbao(repo_root: Path) -> int:
    """Инициализировать OpenBao, KV v2 и два SSH-центра доверия."""
    node = _pve_node_from_environment()

    console.info("Проверка OpenBao 910")
    console.detail("Установка служебных PVE-only сценариев")
    install_openbao_host_support(node, repo_root)
    install_recovery_host_support(node, repo_root)
    prepare_recovery_git(node)

    console.detail("Инициализация OpenBao, KV v2 и проверка SSH-центров доверия")
    initialize_openbao_on_host(node)

    console.detail("Синхронизация SSH OTP и машинных AppRole")
    sync_openbao_otp_contract(node, _otp_sources(repo_root))

    console.detail("Проверка аварийного контура")
    check_recovery_contour(node)

    console.detail("Удаление переходных файловых secret-источников")
    cleanup_transition_state(node)
    check_recovery_contour(node)

    console.result(
        "OpenBao, аварийный контур и миграция старой схемы готовы"
    )
    return 0
