"""Проверка реального SSH OTP перед отказом от машинных сертификатов."""

from __future__ import annotations

import tempfile
from pathlib import Path

from .access import load_access_policy
from .common import InfraManagerError, console
from .machine_ssh import (
    _running_guests,
    _ssh,
    _temporary_admin_identity,
    _temporary_host_ca_file,
)
from .pve import PveClient


CLEANUP_LEGACY_CODE = r"""
import pathlib
import subprocess as process

paths = (
    "/etc/proxmox-guest/ssh/machine_ed25519",
    "/etc/proxmox-guest/ssh/machine_ed25519.pub",
    "/etc/proxmox-guest/ssh/machine_ed25519-cert.pub",
    "/etc/ssh/project-machine-known-hosts",
    "/etc/ssh/ssh_config.d/40-project-machine.conf",
    "/etc/ssh/authorized_principals/root",
    "/etc/ssh/sshd_config.d/42-project-machine-principals.conf",
)
for name in paths:
    pathlib.Path(name).unlink(missing_ok=True)
process.run(["/usr/sbin/sshd", "-t"], check=True)
process.run(["systemctl", "reload", "ssh.service"], check=True)
"""


def verify_ssh_access(repo_root: Path, node: str) -> None:
    """Проверить OTP и затем удалить прежние гостевые SSH-сертификаты."""
    policy = load_access_policy(repo_root)
    if not policy.machine_ssh_edges:
        return

    running = _running_guests(policy, PveClient())
    required = {
        vmid
        for edge in policy.machine_ssh_edges
        for vmid in (edge.source_vmid, edge.target_vmid)
    }
    missing = sorted(required - running.keys())
    if missing:
        raise InfraManagerError(
            "Нельзя подтвердить SSH OTP: гости не запущены или их адрес "
            f"не определён: {missing}"
        )
    manager = policy.guests.get(910)
    if manager is None or manager.address is None:
        raise InfraManagerError("Для SSH OTP не определён адрес OpenBao на 910")

    subnets = {running[vmid].record.subnet for vmid in required}
    if len(subnets) != 1:
        raise InfraManagerError(
            "Проверка SSH OTP пока поддерживает одну административную подсеть"
        )

    with tempfile.TemporaryDirectory(prefix="infra-ssh-otp-") as temporary:
        directory = Path(temporary)
        private_key, certificate = _temporary_admin_identity(node, directory)
        known_hosts = _temporary_host_ca_file(directory, next(iter(subnets)))

        for source_vmid in sorted({edge.source_vmid for edge in policy.machine_ssh_edges}):
            source = running[source_vmid]
            preflight = _ssh(
                source,
                private_key=private_key,
                certificate=certificate,
                known_hosts=known_hosts,
                command=[
                    "sh", "-c",
                    "test -s /etc/infra-manager/openbao/machine.env "
                    "&& test -x /usr/local/sbin/infra-openbao-ssh "
                    "&& curl -fsS --cacert /etc/infra-manager/openbao/ca.crt "
                    f"https://{manager.address}:8202/v1/sys/health >/dev/null",
                ],
                check=False,
            )
            if preflight.returncode:
                raise InfraManagerError(
                    f"guest:{source_vmid}: OTP-клиент или TLS-вход OpenBao "
                    "не готовы; сначала обновите гостя через Deploy Guest"
                )

            for target_vmid in policy.targets_for(source_vmid):
                target = running[target_vmid]
                result = _ssh(
                    source,
                    private_key=private_key,
                    certificate=certificate,
                    known_hosts=known_hosts,
                    command=[
                        "/usr/local/sbin/infra-openbao-ssh",
                        target.address,
                        "true",
                    ],
                    check=False,
                )
                if result.returncode:
                    raise InfraManagerError(
                        f"SSH OTP guest:{source_vmid} → guest:{target_vmid} "
                        "не прошёл; старую схему отключать нельзя"
                    )
                console.ok(
                    f"SSH OTP guest:{source_vmid} → guest:{target_vmid} работает"
                )

            forbidden = sorted(set(running) - set(policy.targets_for(source_vmid)) - {source_vmid})
            for target_vmid in forbidden:
                result = _ssh(
                    source,
                    private_key=private_key,
                    certificate=certificate,
                    known_hosts=known_hosts,
                    command=[
                        "/usr/local/sbin/infra-openbao-otp",
                        running[target_vmid].address,
                    ],
                    check=False,
                )
                if result.returncode == 0:
                    raise InfraManagerError(
                        f"Запрещённая OTP-цель guest:{source_vmid} → "
                        f"guest:{target_vmid} неожиданно разрешена"
                    )

        inactive_sources = (
            policy.machine_identity_vmids
            - {edge.source_vmid for edge in policy.machine_ssh_edges}
        )
        for source_vmid in sorted(inactive_sources & running.keys()):
            source = running[source_vmid]
            result = _ssh(
                source,
                private_key=private_key,
                certificate=certificate,
                known_hosts=known_hosts,
                command=[
                    "/usr/local/sbin/infra-openbao-ssh",
                    manager.address,
                    "true",
                ],
                check=False,
            )
            if result.returncode == 0:
                raise InfraManagerError(
                    f"guest:{source_vmid} получил SSH OTP без разрешённых целей"
                )

        manager_guest = running.get(910)
        if manager_guest is None:
            raise InfraManagerError("Гость 910 недоступен для отключения старого SSH-таймера")
        _ssh(
            manager_guest,
            private_key=private_key,
            certificate=certificate,
            known_hosts=known_hosts,
            command=[
                "sh", "-c",
                "set -e; "
                "if test -e /etc/systemd/system/infra-manager-machine-ssh-refresh.timer; "
                "then systemctl disable --now infra-manager-machine-ssh-refresh.timer; fi; "
                "if systemctl is-active --quiet infra-manager-machine-ssh-refresh.service; "
                "then systemctl stop infra-manager-machine-ssh-refresh.service; fi; "
                "rm -f /etc/systemd/system/infra-manager-machine-ssh-refresh.service "
                "/etc/systemd/system/infra-manager-machine-ssh-refresh.timer "
                "/usr/local/sbin/infra-manager-machine-ssh-refresh; "
                "systemctl daemon-reload",
            ],
        )

        for vmid in sorted(required):
            _ssh(
                running[vmid],
                private_key=private_key,
                certificate=certificate,
                known_hosts=known_hosts,
                command=["python3", "-c", CLEANUP_LEGACY_CODE],
            )

        for edge in sorted(
            policy.machine_ssh_edges,
            key=lambda item: (item.source_vmid, item.target_vmid),
        ):
            source = running[edge.source_vmid]
            target = running[edge.target_vmid]
            result = _ssh(
                source,
                private_key=private_key,
                certificate=certificate,
                known_hosts=known_hosts,
                command=[
                    "/usr/local/sbin/infra-openbao-ssh",
                    target.address,
                    "true",
                ],
                check=False,
            )
            if result.returncode:
                raise InfraManagerError(
                    f"После удаления старого SSH-контракта OTP "
                    f"guest:{edge.source_vmid} → guest:{edge.target_vmid} "
                    "не работает"
                )
