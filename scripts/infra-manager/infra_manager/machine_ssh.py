"""Централизованный межмашинный SSH по правилам access.yaml."""

from __future__ import annotations

import argparse
import ipaddress
import json
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .access import AccessPolicy, GuestRecord, load_access_policy
from .common import InfraManagerError, console, require_command, run
from .pve import PveClient
from .pve_host import (
    sign_machine_ssh_key,
    sign_ssh_client_key,
    sync_machine_ssh_roles,
)
from .settings import PATHS

MACHINE_KEY = Path("/etc/proxmox-guest/ssh/machine_ed25519")
MACHINE_CERT = Path("/etc/proxmox-guest/ssh/machine_ed25519-cert.pub")
MACHINE_KNOWN_HOSTS = Path("/etc/ssh/project-machine-known-hosts")
MACHINE_CLIENT_CONFIG = Path("/etc/ssh/ssh_config.d/40-project-machine.conf")
AUTHORIZED_PRINCIPALS = Path("/etc/ssh/authorized_principals/root")
AUTHORIZED_PRINCIPALS_CONFIG = Path(
    "/etc/ssh/sshd_config.d/42-project-machine-principals.conf"
)


@dataclass(frozen=True)
class RunningGuest:
    record: GuestRecord
    address: str


PREPARE_TARGET_CODE = r"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

payload = json.loads(sys.stdin.read())
principals = payload.get("principals")
if not isinstance(principals, list) or any(
    not isinstance(item, str) or not item for item in principals
):
    raise SystemExit("invalid principals")

principal_dir = pathlib.Path("/etc/ssh/authorized_principals")
principal_file = principal_dir / "root"
config_dir = pathlib.Path("/etc/ssh/sshd_config.d")
config_file = config_dir / "42-project-machine-principals.conf"

principal_dir.mkdir(parents=True, exist_ok=True)
os.chmod(principal_dir, 0o755)
config_dir.mkdir(parents=True, exist_ok=True)
os.chmod(config_dir, 0o755)

content = "\n".join(["root", *sorted(set(principals))]) + "\n"
config = "AuthorizedPrincipalsFile /etc/ssh/authorized_principals/%u\n"


def atomic_write(path, text, mode):
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old == text:
        os.chmod(path, mode)
        return False
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = pathlib.Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chown(path, 0, 0)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)
    return True


changed = atomic_write(principal_file, content, 0o600)
changed = atomic_write(config_file, config, 0o644) or changed

subprocess.run(["/usr/sbin/sshd", "-t"], check=True)
effective = subprocess.run(
    ["/usr/sbin/sshd", "-T"],
    check=True,
    text=True,
    capture_output=True,
).stdout.lower()
if (
    "authorizedprincipalsfile /etc/ssh/authorized_principals/%u"
    not in effective
):
    raise SystemExit("sshd does not use AuthorizedPrincipalsFile")

if changed:
    subprocess.run(["systemctl", "restart", "ssh.service"], check=True)

print(json.dumps({"changed": changed}, separators=(",", ":")))
"""


PREPARE_SOURCE_CODE = r"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

payload = json.loads(sys.stdin.read())
vmid = payload.get("vmid")
host_ca = payload.get("host_ca")
host_pattern = payload.get("host_pattern")
if not isinstance(vmid, int) or vmid <= 0:
    raise SystemExit("invalid VMID")
if not isinstance(host_ca, str) or not host_ca.startswith(("ssh-", "ecdsa-", "sk-")):
    raise SystemExit("invalid host CA")
if not isinstance(host_pattern, str) or not host_pattern:
    raise SystemExit("invalid host pattern")

key_dir = pathlib.Path("/etc/proxmox-guest/ssh")
private_key = key_dir / "machine_ed25519"
public_key = key_dir / "machine_ed25519.pub"
known_hosts = pathlib.Path("/etc/ssh/project-machine-known-hosts")
client_config = pathlib.Path("/etc/ssh/ssh_config.d/40-project-machine.conf")

key_dir.mkdir(parents=True, exist_ok=True)
os.chown(key_dir, 0, 0)
os.chmod(key_dir, 0o700)

if not private_key.is_file():
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"project-machine-{vmid}",
            "-f",
            str(private_key),
        ],
        check=True,
    )
os.chown(private_key, 0, 0)
os.chmod(private_key, 0o600)
os.chown(public_key, 0, 0)
os.chmod(public_key, 0o644)

known_hosts.parent.mkdir(parents=True, exist_ok=True)
client_config.parent.mkdir(parents=True, exist_ok=True)
known_content = f"@cert-authority {host_pattern} {host_ca}\n"
config_content = f"""Host {host_pattern}
    User root
    IdentityFile /etc/proxmox-guest/ssh/machine_ed25519
    CertificateFile /etc/proxmox-guest/ssh/machine_ed25519-cert.pub
    IdentitiesOnly yes
    UserKnownHostsFile /etc/ssh/project-machine-known-hosts
    GlobalKnownHostsFile /dev/null
    StrictHostKeyChecking yes
"""


def atomic_write(path, text, mode):
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old == text:
        os.chmod(path, mode)
        return
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    temporary = pathlib.Path(name)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chown(path, 0, 0)
        os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


atomic_write(known_hosts, known_content, 0o644)
atomic_write(client_config, config_content, 0o644)
print(public_key.read_text(encoding="utf-8").strip())
"""


INSTALL_CERT_CODE = r"""
import os
import pathlib
import subprocess
import sys
import tempfile

certificate = sys.stdin.read().strip()
if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
    raise SystemExit("invalid machine certificate")

target = pathlib.Path("/etc/proxmox-guest/ssh/machine_ed25519-cert.pub")
target.parent.mkdir(parents=True, exist_ok=True)
fd, name = tempfile.mkstemp(prefix=".machine-cert.", dir=target.parent, text=True)
temporary = pathlib.Path(name)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(certificate)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    os.chown(target, 0, 0)
    os.chmod(target, 0o644)
finally:
    temporary.unlink(missing_ok=True)

subprocess.run(["ssh-keygen", "-L", "-f", str(target)], check=True)
"""


def _host_pattern(subnet: str) -> str:
    network = ipaddress.ip_network(subnet, strict=True)
    if not isinstance(network, ipaddress.IPv4Network) or network.prefixlen != 16:
        raise InfraManagerError(
            "Машинный SSH пока поддерживает административную IPv4-сеть /16"
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
            "infra-manager-machine-ssh-sync",
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
    path.write_text(f"@cert-authority {_host_pattern(subnet)} {ca}\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _ssh(
    guest: RunningGuest,
    *,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
    command: list[str],
    input_text: str | None = None,
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
        input_text=input_text,
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
        actual_kind = "vm" if resource.get("type") == "qemu" else "lxc"
        if actual_kind != record.kind:
            raise InfraManagerError(
                f"guest:{vmid} имеет неожиданный тип в PVE: {resource.get('type')}"
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


def _prepare_target(
    guest: RunningGuest,
    principals: tuple[str, ...],
    *,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
) -> None:
    result = _ssh(
        guest,
        private_key=private_key,
        certificate=certificate,
        known_hosts=known_hosts,
        command=["python3", "-c", PREPARE_TARGET_CODE],
        input_text=json.dumps(
            {"principals": list(principals)},
            separators=(",", ":"),
        ),
    )
    payload = json.loads(result.stdout)
    if not isinstance(payload, dict) or not isinstance(payload.get("changed"), bool):
        raise InfraManagerError(
            f"guest:{guest.record.vmid} вернул некорректный результат SSH-политики"
        )


def _prepare_source(
    guest: RunningGuest,
    *,
    host_ca: str,
    host_pattern: str,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
) -> str:
    result = _ssh(
        guest,
        private_key=private_key,
        certificate=certificate,
        known_hosts=known_hosts,
        command=["python3", "-c", PREPARE_SOURCE_CODE],
        input_text=json.dumps(
            {
                "vmid": guest.record.vmid,
                "host_ca": host_ca,
                "host_pattern": host_pattern,
            },
            separators=(",", ":"),
        ),
    )
    public_key = result.stdout.strip()
    if not public_key.startswith("ssh-ed25519 "):
        raise InfraManagerError(
            f"guest:{guest.record.vmid} не вернул машинный Ed25519 public key"
        )
    return public_key


def _install_machine_certificate(
    guest: RunningGuest,
    certificate_text: str,
    *,
    expected_principal: str,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
) -> None:
    inspected = run(
        ["ssh-keygen", "-L", "-f", "/dev/stdin"],
        input_text=certificate_text + "\n",
        capture_output=True,
        check=False,
    )
    if inspected.returncode == 0 and expected_principal not in inspected.stdout:
        raise InfraManagerError(
            f"Машинный сертификат guest:{guest.record.vmid} "
            "не содержит ожидаемый principal"
        )
    _ssh(
        guest,
        private_key=private_key,
        certificate=certificate,
        known_hosts=known_hosts,
        command=["python3", "-c", INSTALL_CERT_CODE],
        input_text=certificate_text + "\n",
    )


def _test_from_source(
    source: RunningGuest,
    target: RunningGuest,
    *,
    should_succeed: bool,
    private_key: Path,
    certificate: Path,
    known_hosts: Path,
) -> None:
    remote = (
        "ssh -o BatchMode=yes -o ConnectTimeout=8 "
        f"root@{shlex.quote(target.address)} true"
    )
    result = _ssh(
        source,
        private_key=private_key,
        certificate=certificate,
        known_hosts=known_hosts,
        command=["sh", "-c", remote],
        check=False,
    )
    if should_succeed and result.returncode != 0:
        raise InfraManagerError(
            f"Разрешённый SSH guest:{source.record.vmid} → "
            f"guest:{target.record.vmid} не работает: "
            f"{(result.stderr or '').strip()}"
        )
    if not should_succeed and result.returncode == 0:
        raise InfraManagerError(
            f"Запрещённый SSH guest:{source.record.vmid} → "
            f"guest:{target.record.vmid} неожиданно разрешён"
        )


def sync_machine_ssh(
    repo_root: Path,
    *,
    verify_connections: bool = False,
) -> int:
    """Синхронизировать роли OpenBao, principals и короткие сертификаты."""

    policy = load_access_policy(repo_root)
    if not policy.machine_identity_vmids:
        console.result("Машинные SSH-идентичности не требуются")
        return 0

    client = PveClient()
    running = _running_guests(policy, client)
    pve_nodes = {
        policy.guests[vmid].node
        for vmid in policy.machine_identity_vmids
        if vmid in policy.guests
    }
    if len(pve_nodes) != 1:
        raise InfraManagerError(
            "Машинный SSH первой версии поддерживает ровно один PVE-узел"
        )
    node = next(iter(pve_nodes))

    sync_machine_ssh_roles(node, sorted(policy.machine_identity_vmids))

    subnets = {
        guest.record.subnet
        for guest in running.values()
    }
    if len(subnets) > 1:
        raise InfraManagerError(
            "Машинный SSH первой версии поддерживает одну административную подсеть"
        )
    if not subnets:
        console.result("Нет запущенных Linux-гостей для синхронизации SSH")
        return 0
    subnet = next(iter(subnets))
    pattern = _host_pattern(subnet)
    host_ca = PATHS.ssh_host_ca_public_key.read_text(encoding="utf-8").strip()

    with tempfile.TemporaryDirectory(prefix="infra-machine-ssh-") as temp:
        temporary = Path(temp)
        admin_private, admin_certificate = _temporary_admin_identity(
            node,
            temporary,
        )
        admin_known_hosts = _temporary_host_ca_file(temporary, subnet)

        for vmid, guest in sorted(running.items()):
            _prepare_target(
                guest,
                policy.authorized_principals(vmid),
                private_key=admin_private,
                certificate=admin_certificate,
                known_hosts=admin_known_hosts,
            )

        for vmid in sorted(policy.machine_identity_vmids):
            guest = running.get(vmid)
            if guest is None:
                continue
            public_key = _prepare_source(
                guest,
                host_ca=host_ca,
                host_pattern=pattern,
                private_key=admin_private,
                certificate=admin_certificate,
                known_hosts=admin_known_hosts,
            )
            machine_certificate = sign_machine_ssh_key(
                node,
                vmid,
                public_key,
            )
            _install_machine_certificate(
                guest,
                machine_certificate,
                expected_principal=policy.machine_principal(vmid),
                private_key=admin_private,
                certificate=admin_certificate,
                known_hosts=admin_known_hosts,
            )

        if verify_connections:
            for source_vmid in sorted(policy.machine_identity_vmids):
                source = running.get(source_vmid)
                if source is None:
                    continue
                allowed = set(policy.targets_for(source_vmid))
                for target_vmid, target in sorted(running.items()):
                    if target_vmid == source_vmid:
                        continue
                    _test_from_source(
                        source,
                        target,
                        should_succeed=target_vmid in allowed,
                        private_key=admin_private,
                        certificate=admin_certificate,
                        known_hosts=admin_known_hosts,
                    )

    console.result(
        "Централизованный межмашинный SSH синхронизирован по access.yaml"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Синхронизировать межмашинный SSH по access.yaml"
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        required=True,
        help="Корень рабочей копии проекта",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Проверить разрешённые и запрещённые SSH-связи",
    )
    args = parser.parse_args(argv)
    return sync_machine_ssh(
        args.repo_root.resolve(),
        verify_connections=args.verify,
    )


if __name__ == "__main__":
    raise SystemExit(main())
