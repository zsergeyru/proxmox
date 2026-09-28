"""Операции PVE-хоста, которые Proxmox не разрешает выполнять API token."""

from __future__ import annotations

import shlex
from pathlib import Path

from .common import InfraManagerError, console, require_command, run
from .settings import PATHS


def _required_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(f"Не найден {label}: {path}")


def _ssh(node: str, *command: str, capture: bool = False):
    """Выполнить команду на PVE от root по отдельному ключу infra-manager."""
    require_command("ssh")
    _required_file(PATHS.pve_host_private_key, "закрытый ключ root-доступа к PVE")
    _required_file(PATHS.pve_host_known_hosts, "known_hosts PVE")

    remote_command = shlex.join(command)

    return run(
        [
            "ssh",
            "-i",
            str(PATHS.pve_host_private_key),
            "-o",
            f"UserKnownHostsFile={PATHS.pve_host_known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            f"root@{node}",
            remote_command,
        ],
        capture_output=capture,
    )


def _features_from_config(config: str) -> dict[str, str]:
    """Разобрать строку features из pct config."""
    line = next(
        (item for item in config.splitlines() if item.startswith("features: ")),
        "",
    )
    if not line:
        return {}

    result: dict[str, str] = {}
    for item in line.partition(": ")[2].split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        result[name] = value if separator else "1"
    return result


def _render_features(features: dict[str, str]) -> str:
    return ",".join(
        f"{name}={value}"
        for name, value in sorted(features.items())
    )


def _container_running(node: str, vmid: int) -> bool:
    result = _ssh(node, "pct", "status", str(vmid), capture=True)
    return result.stdout.strip().endswith("running")


def ensure_container_host(node: str, vmid: int) -> None:
    """Обеспечить полный контракт container-host: nesting=1 и keyctl=1."""
    current_result = _ssh(node, "pct", "config", str(vmid), capture=True)
    current = _features_from_config(current_result.stdout)

    if current.get("nesting") == "1" and current.get("keyctl") == "1":
        console.ok(f"LXC {vmid}: container-host features уже настроены")
        return

    desired = dict(current)
    desired["nesting"] = "1"
    desired["keyctl"] = "1"

    was_running = _container_running(node, vmid)
    if was_running:
        console.info(
            f"Остановка LXC {vmid} для применения root-only feature keyctl"
        )
        _ssh(node, "pct", "stop", str(vmid))

    try:
        _ssh(
            node,
            "pct",
            "set",
            str(vmid),
            "--features",
            _render_features(desired),
        )
    finally:
        if was_running:
            _ssh(node, "pct", "start", str(vmid))

    verified = _ssh(node, "pct", "config", str(vmid), capture=True)
    actual = _features_from_config(verified.stdout)
    if actual.get("nesting") != "1" or actual.get("keyctl") != "1":
        raise InfraManagerError(
            f"LXC {vmid}: не удалось применить container-host features"
        )

    console.ok(f"LXC {vmid}: применены nesting=1,keyctl=1")


def apply_host_requirements(
    *,
    node: str,
    vmid: int,
    kind: str,
    features: tuple[str, ...],
) -> None:
    """Применить только те требования гостя, которым нужен root самого PVE."""
    if kind != "lxc":
        return
    if "container-host" in features:
        ensure_container_host(node, vmid)


def check_root_access(node: str) -> None:
    """Проверить рабочий root SSH-доступ к PVE без изменения состояния."""
    result = _ssh(node, "id", "-u", capture=True)
    if result.stdout.strip() != "0":
        raise InfraManagerError("SSH-доступ к PVE не выполняется от root")
