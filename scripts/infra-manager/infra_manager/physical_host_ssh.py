"""Доверенный административный SSH к физическому серверу.

Сравнивает ключ сервера с заранее подтверждённым отпечатком. Получение
ключа из сети само по себе никогда не считается подтверждением доверия.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .common import InfraManagerError, run
from .physical_hosts import PhysicalHost
from .pve_host import sign_ssh_client_key


_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def _scan_verified_key(host: PhysicalHost, directory: Path) -> Path:
    """Получить SSH-ключ только если он совпадает с закреплённым отпечатком."""

    if not host.address or not host.ssh_host_fingerprint:
        raise InfraManagerError("Для SSH требуются адрес и проверенный отпечаток")
    if not _FINGERPRINT.fullmatch(host.ssh_host_fingerprint):
        raise InfraManagerError("Некорректный отпечаток SSH-ключа физического сервера")

    response = run(
        ["ssh-keyscan", "-T", "10", "-p", str(host.port),
         "-t", "ed25519", host.address],
        check=False, capture_output=True,
    )
    if response.returncode != 0 or not response.stdout.strip():
        raise InfraManagerError("Физический сервер не выдал ключ SSH Ed25519")
    matched: list[str] = []
    for line in response.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "ssh-ed25519":
            continue
        candidate = directory / "candidate.pub"
        candidate.write_text(f"ssh-ed25519 {parts[2]}\n", encoding="utf-8")
        fingerprint = run(
            ["ssh-keygen", "-E", "sha256", "-lf", str(candidate)],
            capture_output=True,
        ).stdout
        if host.ssh_host_fingerprint in fingerprint.split():
            matched.append(parts[2])
    if len(set(matched)) != 1:
        raise InfraManagerError(
            "Ключ SSH NAS не совпадает с подтверждённым отпечатком "
            "или сервер выдал неоднозначную идентичность"
        )
    known_hosts = directory / "known_hosts"
    host_token = host.address if host.port == 22 else f"[{host.address}]:{host.port}"
    known_hosts.write_text(
        f"{host_token} ssh-ed25519 {matched[0]}\n", encoding="utf-8"
    )
    known_hosts.chmod(0o600)
    return known_hosts


class PhysicalHostSshSession:
    """Однократное SSH-подключение с временным ключом клиента OpenBao."""

    def __init__(self, host: PhysicalHost, pve_node: str) -> None:
        """Сохранить проверенную цель и доверенный PVE для подписи."""

        self.host = host
        self.pve_node = pve_node
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._private: Path | None = None
        self._known_hosts: Path | None = None

    def __enter__(self) -> PhysicalHostSshSession:
        """Получить подтверждение SSH-хоста и временную клиентскую подпись."""

        self._temporary = tempfile.TemporaryDirectory(prefix="physical-ssh-")
        try:
            directory = Path(self._temporary.name)
            self._known_hosts = _scan_verified_key(self.host, directory)
            self._private = directory / "client_ed25519"
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f",
                 str(self._private)])
            self._private.chmod(0o600)
            public = Path(str(self._private) + ".pub").read_text(
                encoding="utf-8"
            ).strip()
            signed = sign_ssh_client_key(self.pve_node, public)
            if not signed.startswith("ssh-ed25519-cert-v01@openssh.com "):
                raise InfraManagerError("OpenBao вернул некорректный SSH-сертификат")
            certificate = directory / "client_ed25519-cert.pub"
            certificate.write_text(signed + "\n", encoding="utf-8")
            certificate.chmod(0o600)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """Удалить временный закрытый ключ и сведения сеанса."""

        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def command(self, remote_command: str) -> list[str]:
        """Собрать строго проверяемую SSH-команду без запасных ключей."""

        if not self._private or not self._known_hosts or not self.host.address:
            raise InfraManagerError("SSH-сеанс физического сервера ещё не подготовлен")
        return [
            "ssh", "-F", "/dev/null", "-p", str(self.host.port),
            "-i", str(self._private),
            "-o", f"UserKnownHostsFile={self._known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            f"{self.host.user}@{self.host.address}",
            remote_command,
        ]

    def verify_connection(self) -> tuple[bool, str]:
        """Проверить вход по сертификату без изменения NAS."""

        result = run(
            self.command("true"),
            check=False,
            capture_output=True,
        )
        return result.returncode == 0, (
            "" if result.returncode == 0 else "NAS не принял временный SSH-сертификат"
        )
