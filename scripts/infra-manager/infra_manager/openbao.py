"""Первичная инициализация OpenBao через доверенный PVE-хост."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from .access import load_access_policy
from .common import InfraManagerError, console
from .pve_host import (
    check_recovery_contour,
    repair_openbao_on_host,
    install_openbao_host_support,
    install_recovery_host_support,
    prepare_recovery_git,
    sync_openbao_otp_contract,
    update_openbao_pve_api_credential,
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


BOOTSTRAP_PVE_API_ENV = Path(
    "/run/infra-manager/bootstrap-secrets/pve-api.env"
)


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        values[key] = value
    return values


def _staged_pve_api_credential() -> dict[str, str] | None:
    """Прочитать одноразовый PVE credential recovery, если он передан."""

    if not BOOTSTRAP_PVE_API_ENV.is_file():
        return None

    values = _read_env_file(BOOTSTRAP_PVE_API_ENV)
    endpoint = values.get("PVE_API_URL", "")
    token_id = values.get("PVE_API_TOKEN_ID", "")
    token_secret = values.get("PVE_API_TOKEN_SECRET", "")
    if not endpoint.startswith("https://"):
        raise InfraManagerError(
            f"Некорректный PVE endpoint в {BOOTSTRAP_PVE_API_ENV}"
        )
    if token_id != "root@pam!infra-manager":
        raise InfraManagerError(
            f"Некорректный PVE token id в {BOOTSTRAP_PVE_API_ENV}"
        )
    if not token_secret or any(char.isspace() for char in token_secret):
        raise InfraManagerError(
            f"Некорректный PVE token secret в {BOOTSTRAP_PVE_API_ENV}"
        )
    return {
        "endpoint": endpoint,
        "token_id": token_id,
        "token_secret": token_secret,
    }


def _otp_sources(repo_root: Path) -> list[dict[str, object]]:
    policy = load_access_policy(repo_root)
    sources: list[dict[str, object]] = []

    source_vmids = sorted(
        {
            edge.source_vmid
            for edge in policy.ssh_otp_edges
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
    """Идемпотентно подготовить или восстановить OpenBao и его контур доступа."""
    node = _pve_node_from_environment()

    console.info("Проверка OpenBao infra-manager")
    console.detail("Установка служебных PVE-only сценариев")
    install_openbao_host_support(node, repo_root)
    install_recovery_host_support(node, repo_root)
    prepare_recovery_git(node)

    console.detail("Приведение OpenBao к рабочему состоянию")
    repair_openbao_on_host(node)

    staged_pve = _staged_pve_api_credential()
    if staged_pve is not None:
        console.detail("Обновление PVE API credential после recovery")
        update_openbao_pve_api_credential(
            node,
            endpoint=staged_pve["endpoint"],
            token_id=staged_pve["token_id"],
            token_secret=staged_pve["token_secret"],
        )

    console.detail("Синхронизация SSH OTP и машинных AppRole")
    sync_openbao_otp_contract(node, _otp_sources(repo_root))

    console.detail("Проверка аварийного контура")
    check_recovery_contour(node)

    console.result(
        "OpenBao, аварийный контур и рабочие секреты готовы"
    )
    return 0


def sync_ssh_access(repo_root: Path) -> int:
    """Привести SSH OTP/AppRole OpenBao к правилам access.yaml."""
    node = _pve_node_from_environment()
    sync_openbao_otp_contract(node, _otp_sources(repo_root))
    console.result("SSH OTP и машинные AppRole синхронизированы по access.yaml")
    return 0
