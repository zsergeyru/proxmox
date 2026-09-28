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
        console.ok(f"LXC {vmid}: параметры контейнера уже настроены")
        return

    desired = dict(current)
    desired["nesting"] = "1"
    desired["keyctl"] = "1"

    was_running = _container_running(node, vmid)
    if was_running:
        console.info(
            f"Остановка LXC {vmid} для изменения параметров контейнера"
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

    console.ok(f"LXC {vmid}: параметры контейнера настроены")


def ensure_infra_self_access(
    node: str,
    vmid: int,
    *,
    hostname: str,
    public_key: str,
) -> None:
    """Разрешить Ansible-ключу 910 вход в сам 910 через доверенный PVE."""
    if not public_key.strip():
        raise InfraManagerError("Открытый Ansible-ключ 910 пуст")

    config = _ssh(node, "pct", "config", str(vmid), capture=True).stdout
    expected_hostname = f"hostname: {hostname}"
    ownership_markers = (
        "owner=proxmox-project",
        "role=infra-manager",
    )
    if expected_hostname not in config or not all(
        marker in config for marker in ownership_markers
    ):
        raise InfraManagerError(
            f"VMID {vmid} не подтверждён как принадлежащий infra-manager"
        )

    script = r"""
from pathlib import Path
import os
import sys

key = sys.argv[1].strip()
begin = "# BEGIN infra-manager ansible self"
end = "# END infra-manager ansible self"
ssh_dir = Path("/root/.ssh")
target = ssh_dir / "authorized_keys"

ssh_dir.mkdir(parents=True, exist_ok=True)
os.chmod(ssh_dir, 0o700)
lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

result = []
inside = False
for line in lines:
    if line == begin:
        inside = True
        continue
    if line == end:
        inside = False
        continue
    if not inside:
        result.append(line)

while result and not result[-1].strip():
    result.pop()

if result:
    result.append("")
result.extend([begin, f"{key} infra-manager ansible guest", end])
target.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
"""
    _ssh(
        node,
        "pct",
        "exec",
        str(vmid),
        "--",
        "python3",
        "-c",
        script,
        public_key.strip(),
    )
    console.ok(f"LXC {vmid}: постоянный Ansible-доступ к самому себе готов")


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
