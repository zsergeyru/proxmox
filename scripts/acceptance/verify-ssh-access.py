#!/usr/bin/env python3
"""Приёмочная проверка реального межмашинного SSH через OpenBao OTP."""

from __future__ import annotations

import ipaddress
import os
import shlex
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

from infra_manager.access import AccessPolicy, GuestRecord, load_access_policy
from infra_manager.common import InfraManagerError, console, require_command, run
from infra_manager.guest_catalog import find_guest_by_role
from infra_manager.pve import PveClient
from infra_manager.pve_host import sign_ssh_client_key
from infra_manager.settings import PATHS, SETTINGS


@dataclass(frozen=True)
class RunningGuest:
    record: GuestRecord
    address: str


def _pve_node_from_environment() -> str:
    endpoint = os.environ.get("TF_VAR_pve_endpoint", "").strip()
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise InfraManagerError(
            "Для приёмочной проверки нужен TF_VAR_pve_endpoint с HTTPS-адресом PVE"
        )
    return parsed.hostname


def _host_pattern(subnet: str) -> str:
    network = ipaddress.ip_network(subnet, strict=True)
    if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen != 16:
        raise InfraManagerError(
            "Приёмочная проверка SSH OTP пока поддерживает административную IPv4-сеть /16"
        )
    first, second, _, _ = str(network.network_address).split(".")
    return f"{first}.{second}.*"


def _temporary_admin_identity(node: str, directory: Path) -> tuple[Path, Path]:
    require_command("ssh-keygen")
    private_key = directory / "admin_ed25519"
    run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "infra-manager-ssh-otp-acceptance",
            "-f",
            str(private_key),
        ]
    )
    public_key = private_key.with_suffix(".pub").read_text(
        encoding="utf-8"
    ).strip()
    certificate = sign_ssh_client_key(node, public_key)
    certificate_path = Path(str(private_key) + "-cert.pub")
    certificate_path.write_text(certificate + "\n", encoding="utf-8")
    certificate_path.chmod(0o600)
    return private_key, certificate_path


def _temporary_host_ca_file(directory: Path, subnet: str) -> Path:
    if not PATHS.ssh_host_ca_public_key.is_file():
        raise InfraManagerError(
            f"Не опубликован SSH host CA: {PATHS.ssh_host_ca_public_key}"
        )
    ca = PATHS.ssh_host_ca_public_key.read_text(encoding="utf-8").strip()
    if not ca.startswith(("ssh-", "ecdsa-", "sk-")):
        raise InfraManagerError("Опубликован некорректный SSH host CA")
    path = directory / "host-ca-known-hosts"
    path.write_text(
        f"@cert-authority {_host_pattern(subnet)} {ca}\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _ssh(
    guest: RunningGuest,
    *,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
    command: list[str],
    check: bool = True,
):
    require_command("ssh")
    return run(
        [
            "ssh",
            "-i",
            str(private_key),
            "-o",
            f"CertificateFile={certificate}",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            f"root@{guest.address}",
            shlex.join(command),
        ],
        capture_output=True,
        check=check,
    )


def _running_guests(
    policy: AccessPolicy,
    client: PveClient,
) -> dict[int, RunningGuest]:
    running: dict[int, RunningGuest] = {}
    for vmid, record in sorted(policy.guests.items()):
        if not record.provisioned_linux:
            continue
        resource = client.find_vm(vmid)
        if resource is None or str(resource.get("status") or "") != "running":
            continue
        resource_type = resource.get("type")
        actual_kind = "vm" if resource_type == "qemu" else "lxc" if resource_type == "lxc" else None
        if actual_kind != record.kind:
            raise InfraManagerError(
                f"guest:{vmid} имеет неожиданный тип в PVE: {resource_type!r}"
            )
        address = record.address
        if address is None:
            found = client.guest_management_ipv4(
                node=record.node,
                vmid=vmid,
                kind=record.kind,
                subnet=record.subnet,
            )
            if found is None:
                continue
            address = str(found)
        running[vmid] = RunningGuest(record=record, address=address)
    return running


def verify_ssh_access(repo_root: Path, node: str) -> None:
    policy = load_access_policy(repo_root)
    if not policy.ssh_otp_edges:
        console.result("В access.yaml нет межмашинных SSH OTP-связей")
        return

    running = _running_guests(policy, PveClient())
    required = {
        vmid
        for edge in policy.ssh_otp_edges
        for vmid in (edge.source_vmid, edge.target_vmid)
    }
    missing = sorted(required - running.keys())
    if missing:
        raise InfraManagerError(
            "Нельзя выполнить приёмочную проверку SSH OTP: "
            f"гости не запущены или их адрес не определён: {missing}"
        )

    infra_manager = find_guest_by_role(
        repo_root,
        SETTINGS.infra_manager_role,
    )
    manager = policy.guests.get(infra_manager.vmid)
    if manager is None or manager.address is None:
        raise InfraManagerError(
            "Для SSH OTP не определён адрес OpenBao на infra-manager"
        )

    subnets = {running[vmid].record.subnet for vmid in required}
    if len(subnets) != 1:
        raise InfraManagerError(
            "Приёмочная проверка SSH OTP пока поддерживает одну административную подсеть"
        )

    with tempfile.TemporaryDirectory(prefix="infra-ssh-otp-acceptance-") as temporary:
        directory = Path(temporary)
        private_key, certificate = _temporary_admin_identity(node, directory)
        known_hosts = _temporary_host_ca_file(directory, next(iter(subnets)))

        sources = sorted({edge.source_vmid for edge in policy.ssh_otp_edges})
        for source_vmid in sources:
            source = running[source_vmid]
            preflight = _ssh(
                source,
                private_key=private_key,
                certificate=certificate,
                known_hosts=known_hosts,
                command=[
                    "sh",
                    "-c",
                    "test -s /etc/infra-manager/openbao/machine.env "
                    "&& test -x /usr/local/sbin/infra-openbao-ssh "
                    "&& test -x /usr/local/sbin/infra-openbao-otp "
                    "&& curl -fsS --cacert /etc/infra-manager/openbao/ca.crt "
                    f"https://{manager.address}:8202/v1/sys/health >/dev/null",
                ],
                check=False,
            )
            if preflight.returncode:
                raise InfraManagerError(
                    f"guest:{source_vmid}: OTP-клиент или TLS-вход OpenBao не готовы"
                )

            allowed = set(policy.targets_for(source_vmid))
            for target_vmid in sorted(allowed):
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
                        f"Разрешённый SSH OTP guest:{source_vmid} → "
                        f"guest:{target_vmid} не работает"
                    )
                console.ok(
                    f"SSH OTP guest:{source_vmid} → guest:{target_vmid} работает"
                )

            forbidden = sorted(set(running) - allowed - {source_vmid})
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

    console.result("Приёмочная проверка SSH OTP успешно завершена")


def main() -> int:
    try:
        verify_ssh_access(ROOT, _pve_node_from_environment())
    except (InfraManagerError, OSError, ValueError) as exc:
        console.error(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
