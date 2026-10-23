"""Доверенный административный SSH к физическому серверу.

При первом развёртывании автоматически запоминает полученный SSH-ключ.
При последующих подключениях сверяет его с сохранённым отпечатком.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from dataclasses import replace
from pathlib import Path

from .common import InfraManagerError, run
from .physical_hosts import PhysicalHost
from .pve_host import sign_ssh_client_key


_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")


def discover_first_host_key(host: PhysicalHost) -> PhysicalHost:
    """При первом подключении выбрать единственный SSH-ключ Ed25519 NAS.

    Запись доверия производится только после успешной проверки OpenBao.
    """

    if not host.address or host.ssh_host_fingerprint is not None:
        raise InfraManagerError("Первый поиск SSH-ключа допустим только без закреплённого ключа")
    response = run(
        ["ssh-keyscan", "-T", "10", "-p", str(host.port),
         "-t", "ed25519", host.address],
        check=False, capture_output=True,
    )
    if response.returncode != 0:
        raise InfraManagerError("NAS не ответил на первоначальный запрос SSH-ключа")
    keys = set()
    for line in response.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            keys.add(parts[2])
    if len(keys) != 1:
        raise InfraManagerError("Не удалось получить единственный SSH Ed25519 ключ NAS")
    with tempfile.TemporaryDirectory(prefix="nas-initial-key-") as directory:
        candidate = Path(directory) / "host.pub"
        candidate.write_text(f"ssh-ed25519 {next(iter(keys))}\n", encoding="utf-8")
        result = run(
            ["ssh-keygen", "-E", "sha256", "-lf", str(candidate)],
            capture_output=True, check=False,
        )
    fingerprints = [
        value for value in result.stdout.split()
        if _FINGERPRINT.fullmatch(value)
    ]
    if result.returncode != 0 or len(fingerprints) != 1:
        raise InfraManagerError("Не удалось вычислить отпечаток SSH-ключа NAS")
    return replace(host, ssh_host_fingerprint=fingerprints[0])


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

    def ansible_parameters(self) -> tuple[str, str]:
        """Вернуть временный закрытый ключ и параметры проверки SSH."""

        if not self._private or not self._known_hosts or not self.host.address:
            raise InfraManagerError("Сначала откройте доверенный SSH-сеанс")
        options = [
            "-F", "/dev/null",
            "-o", f"UserKnownHostsFile={self._known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-p", str(self.host.port),
        ]
        return str(self._private), shlex.join(options)

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
