"""Единый административный SSH-сеанс для управляемых гостей.

В штатном режиме создаёт одноразовый ключ, получает клиентский сертификат
OpenBao и доверяет серверу только через опубликованный SSH Host CA. В
первоначальном режиме использует переданный извне одноразовый ключ и заранее
проверенный ключ сервера. Не обслуживает доступ к PVE или межмашинный SSH OTP.

Временные закрытые ключи, сертификаты и known_hosts удаляются при выходе
из сеанса даже после ошибки. Постоянный known_hosts гостевых машин не меняется.
"""

from __future__ import annotations

import re
import shlex
import tempfile
from pathlib import Path
from typing import Callable

from .common import InfraManagerError, run
from .pve_host import sign_ssh_client_key
from .settings import PATHS


class GuestSshSession:
    """Подготовить и использовать один административный SSH-сеанс гостя."""

    def __init__(
        self,
        node: str,
        address: str,
        *,
        host_ca: Path | None = None,
        sign_client: Callable[[str, str], str] = sign_ssh_client_key,
        execute: Callable = run,
        bootstrap_key: Path | None = None,
        bootstrap_known_hosts: Path | None = None,
        pinned_host_key: str | None = None,
    ) -> None:
        """Выбрать сертификатный либо явно разрешённый начальный режим.

        Начальный режим допустим лишь с переданным одноразовым ключом и
        проверенным заранее known_hosts. Переключения на него при неудачной
        штатной аутентификации внутри класса нет.
        """
        self.node = node
        self.address = address
        self.host_ca = host_ca if host_ca is not None else PATHS.ssh_host_ca_public_key
        self.sign_client = sign_client
        self.execute = execute
        self.bootstrap_key = bootstrap_key
        self.bootstrap_known_hosts = bootstrap_known_hosts
        self.pinned_host_key = pinned_host_key
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.private_key: Path | None = None
        self.certificate: Path | None = None
        self.known_hosts: Path | None = None

    def __enter__(self) -> GuestSshSession:
        """Подготовить доверие и удостоверение, не меняя файлов гостя."""
        if self.bootstrap_key is not None:
            if self.pinned_host_key is not None:
                raise InfraManagerError("Нельзя совместить начальный и восстановительный SSH-доступ")
            if not self.bootstrap_key.is_file():
                raise InfraManagerError("Отсутствует одноразовый начальный SSH-ключ")
            if (
                self.bootstrap_known_hosts is None
                or not self.bootstrap_known_hosts.is_file()
                or self.bootstrap_known_hosts.stat().st_size == 0
            ):
                raise InfraManagerError(
                    "Начальный SSH-доступ не имеет подтверждённого ключа сервера"
                )
            self.private_key = self.bootstrap_key
            self.known_hosts = self.bootstrap_known_hosts
            return self

        if not self.host_ca.is_file() or not self.host_ca.stat().st_size:
            raise InfraManagerError(f"Отсутствует SSH Host CA OpenBao: {self.host_ca}")
        ca = self.host_ca.read_text(encoding="utf-8").strip()
        if not ca.startswith(("ssh-ed25519 ", "ssh-rsa ", "ecdsa-sha2-")):
            raise InfraManagerError("SSH Host CA OpenBao имеет некорректный формат")

        # Доверие к конкретному адресу берётся только из CA, не из ssh-keyscan
        # и не из сохраняемых ключей предыдущих версий гостя.
        self._temporary = tempfile.TemporaryDirectory(prefix="guest-ssh-")
        try:
            temporary = Path(self._temporary.name)
            self.private_key = temporary / "admin_ed25519"
            self.execute([
                "ssh-keygen", "-q", "-t", "ed25519", "-N", "",
                "-C", "infra-manager-guest-admin", "-f", str(self.private_key),
            ])
            self.private_key.chmod(0o600)
            public = Path(str(self.private_key) + ".pub").read_text(
                encoding="utf-8"
            ).strip()
            certificate = self.sign_client(self.node, public)
            if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
                raise InfraManagerError("OpenBao вернул некорректный SSH-сертификат")
            self.certificate = temporary / "admin_ed25519-cert.pub"
            self.certificate.write_text(certificate + "\n", encoding="utf-8")
            self.certificate.chmod(0o600)
            self.execute(["ssh-keygen", "-L", "-f", str(self.certificate)], capture_output=True)

            self.known_hosts = temporary / "known_hosts"
            if self.pinned_host_key is None:
                host_line = f"@cert-authority {self.address} {ca}\n"
            else:
                parts = self.pinned_host_key.strip().split()
                if (
                    len(parts) != 2 or parts[0] != "ssh-ed25519"
                    or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])
                ):
                    raise InfraManagerError("Некорректный ранее закреплённый ключ SSH-сервера")
                host_line = f"{self.address} {parts[0]} {parts[1]}\n"
            self.known_hosts.write_text(host_line, encoding="utf-8")
            self.known_hosts.chmod(0o600)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        """Удалить собственную временную идентичность при любом исходе."""
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _options(self) -> list[str]:
        """Вернуть строгие параметры SSH для выбранного режима доверия."""
        if self.private_key is None or self.known_hosts is None:
            raise InfraManagerError("Административный SSH-сеанс ещё не подготовлен")
        options = [
            "-o", f"UserKnownHostsFile={self.known_hosts}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "UpdateHostKeys=no",
            "-o", "BatchMode=yes",
            "-o", "IdentitiesOnly=yes",
            "-o", "ConnectTimeout=10",
        ]
        if self.certificate is not None:
            # После перехода на CA не допускаем доверие к обычному host key.
            options += [
                "-o", "HostKeyAlgorithms=" + (
                    "ssh-ed25519" if self.pinned_host_key is not None
                    else "ssh-ed25519-cert-v01@openssh.com"
                ),
            ]
            options += ["-o", f"CertificateFile={self.certificate}"]
        return options

    def ssh_command(self, remote_command: str) -> list[str]:
        """Собрать SSH-команду с тем же доверием, что использует Ansible."""
        if self.private_key is None:
            raise InfraManagerError("Административный SSH-сеанс ещё не подготовлен")
        return [
            "ssh", "-i", str(self.private_key),
            *self._options(),
            f"root@{self.address}",
            remote_command,
        ]

    def ansible_ssh_args(self) -> str:
        """Передать Ansible те же ограничения проверки сервера и клиента."""
        return shlex.join(self._options())

    def verify_connection(self) -> tuple[bool, str]:
        """Проверить реальный вход; вернуть безопасную причину отказа SSH."""
        result = self.execute(
            self.ssh_command("true"), check=False, capture_output=True
        )
        if result.returncode == 0:
            return True, ""
        reason = (result.stderr or "").strip().replace("\n", " ")
        return False, reason[:500] or f"SSH завершился с кодом {result.returncode}"
