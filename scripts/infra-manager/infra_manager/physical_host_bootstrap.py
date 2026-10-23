"""Первоначальное доверие физического NAS по паролю существующего sudo-пользователя.

Пароль используется только в памяти для одного SSH-подключения.
Временных пользователей, постоянных ключей и файлов с паролем нет.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
from dataclasses import replace
from pathlib import Path

from .common import InfraManagerError, console
from .physical_host_ssh import (
    PhysicalHostSshSession, _scan_verified_key, discover_first_host_key, _FINGERPRINT,
)
from .physical_hosts import PhysicalHost
from .physical_host_state import checked_record_path
from .settings import PATHS


_USER = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$")
_REQUIRED = 20


def marker_path(name: str) -> Path:
    """Построить локальный сохраняемый путь для уже подтверждённого входа."""

    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise InfraManagerError("Некорректное имя физического сервера")
    return checked_record_path(name, data_root=PATHS.data_dir)


def resolve_saved_host(host: PhysicalHost) -> PhysicalHost:
    """Использовать закреплённый SSH-ключ, не повторять первое доверие."""

    path = marker_path(host.name)
    if not path.exists():
        if checked_record_path(host.name, storage=True, data_root=PATHS.data_dir).exists():
            raise InfraManagerError(
                "Назначения дисков сохранились, но отметка SSH-доверия исчезла. "
                "Автоматический повтор начального входа запрещён"
            )
        return host
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfraManagerError("Отметка SSH-доверия NAS повреждена; повторный пароль запрещён") from exc
    if not isinstance(marker, dict) or marker.get("name") != host.name:
        raise InfraManagerError("Отметка SSH-доверия NAS не соответствует узлу")
    fingerprint = marker.get("fingerprint")
    if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(fingerprint):
        raise InfraManagerError("В отметке SSH-доверия NAS отсутствует корректный отпечаток")
    if marker.get("address") != host.address:
        raise InfraManagerError("Адрес NAS изменился; повторный вход по паролю запрещён")
    if host.ssh_host_fingerprint not in (None, fingerprint):
        raise InfraManagerError(
            "Отпечаток NAS в host.yaml отличается от сохранённого SSH-ключа"
        )
    return replace(host, ssh_host_fingerprint=fingerprint)


def is_bootstrapped(host: PhysicalHost) -> bool:
    """Не запрашивать пароль после успешного первого SSH-подключения."""

    resolve_saved_host(host)
    return marker_path(host.name).exists()


def _save_bootstrapped(host: PhysicalHost) -> None:
    """Атомарно запомнить успешный сертификатный вход без секретов."""

    path = marker_path(host.name)
    fd, name = tempfile.mkstemp(prefix=f".{host.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"name": host.name, "fingerprint": host.ssh_host_fingerprint,
                 "address": host.address},
                stream,
                ensure_ascii=False,
            )
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _install_client_ca(
    host: PhysicalHost, username: str, password: str, public_ca: str
) -> None:
    """Один раз включить доверие клиентскому CA с существующими sudo-правами."""

    try:
        import paramiko
    except ImportError as exc:
        raise InfraManagerError(
            "Для начального доступа управляющего гостя необходим пакет paramiko в infra-runtime"
        ) from exc

    # Публичные данные CA передаются в аргументе удалённой команды;
    # пароль не попадает ни в оболочку, ни в аргументы, ни в окружение.
    ca = shlex.quote(public_ca)
    script = "\n".join([
        "set -eu",
        "install -d -m 0755 /etc/ssh/sshd_config.d",
        "umask 077",
        'ca_tmp=$(mktemp /etc/ssh/.infra-client-ca.XXXXXX)',
        'config_tmp=$(mktemp /etc/ssh/sshd_config.d/.infra-client-ca.XXXXXX)',
        'trap \'rm -f "$ca_tmp" "$config_tmp"\' EXIT',
        f"printf '%s\\n' {ca} > \"$ca_tmp\"",
        'chmod 0644 "$ca_tmp"',
        "printf '%s\n' 'TrustedUserCAKeys /etc/ssh/trusted-user-ca-keys.pem' 'PermitRootLogin prohibit-password' > \"$config_tmp\"",
        'chmod 0644 "$config_tmp"',
        'mv -f "$ca_tmp" /etc/ssh/trusted-user-ca-keys.pem',
        'mv -f "$config_tmp" /etc/ssh/sshd_config.d/40-infra-manager-user-ca.conf',
        "/usr/sbin/sshd -t",
        "sshd_effective=$(/usr/sbin/sshd -T -C user=root,host=localhost,addr=127.0.0.1)",
        'printf "%s\\n" "$sshd_effective" | grep -Fxq "trustedusercakeys /etc/ssh/trusted-user-ca-keys.pem"',
        'printf "%s\\n" "$sshd_effective" | grep -Eq "^permitrootlogin (prohibit-password|without-password)$"',
        "systemctl reload ssh.service",
    ])
    with tempfile.TemporaryDirectory(prefix="nas-password-ssh-") as temp:
        known_hosts = _scan_verified_key(host, Path(temp))
        ssh = paramiko.SSHClient()
        ssh.load_host_keys(str(known_hosts))
        ssh.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            ssh.connect(
                hostname=host.address, port=host.port, username=username,
                password=password, look_for_keys=False, allow_agent=False,
                timeout=10, auth_timeout=15, banner_timeout=15,
            )
            command = "sudo -S -p '' sh -c " + shlex.quote(script)
            stdin, stdout, stderr = ssh.exec_command(command, timeout=30)
            stdin.write(password + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
            code = stdout.channel.recv_exit_status()
            # Вывод sudo не публикуем: ошибочный вывод может содержать
            # служебные сведения системы или пароль.
            if code != 0:
                raise InfraManagerError(
                    "NAS отказал в настройке доверия SSH. Проверьте sudo, "
                    "службу ssh.service и права существующего пользователя."
                )
        except (paramiko.SSHException, OSError) as exc:
            raise InfraManagerError(
                "Не удалось войти по паролю в NAS или подтвердить SSH-ключ"
            ) from exc
        finally:
            ssh.close()


def bootstrap_physical_host(
    host: PhysicalHost, pve_node: str, payload: str
) -> int:
    """Применить доверие и запомнить успех только после проверки OpenBao."""

    if is_bootstrapped(host):
        raise InfraManagerError(
            "Первоначальное доверие уже установлено; повторный ввод пароля запрещён"
        )
    try:
        credentials = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise InfraManagerError("Параметры первоначального подключения повреждены") from exc
    username = credentials.get("username") if isinstance(credentials, dict) else None
    password = credentials.get("password") if isinstance(credentials, dict) else None
    if not isinstance(username, str) or not _USER.fullmatch(username):
        raise InfraManagerError("Некорректное имя администратора NAS")
    if not isinstance(password, str) or not password or "\n" in password or "\r" in password:
        raise InfraManagerError("Пустой или некорректный пароль для первого подключения")
    try:
        public_ca = PATHS.ssh_client_ca_public_key.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise InfraManagerError("Не найден открытый ключ клиентского CA OpenBao") from exc
    parts = public_ca.split()
    if (
        "\n" in public_ca or len(parts) < 2
        or parts[0] not in {"ssh-ed25519", "ssh-rsa"}
        or not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", parts[1])
    ):
        raise InfraManagerError("Некорректный открытый ключ клиентского CA OpenBao")
    if host.ssh_host_fingerprint is None:
        host = discover_first_host_key(host)
        console.info(
            f"Первый SSH-ключ NAS принят автоматически: {host.ssh_host_fingerprint}. "
            "После успешного входа он будет закреплён для следующих подключений"
        )
    _install_client_ca(host, username, password, public_ca)
    # Второе независимое SSH-подключение обязательно: пароль уже не
    # используется, сервер и сертификат проверяются в штатном режиме.
    with PhysicalHostSshSession(host, pve_node) as session:
        verified, _reason = session.verify_connection()
        if not verified:
            raise InfraManagerError(
                "Доверие к OpenBao настроено, но NAS не подтвердил "
                "вход по сертификату root. Проверьте PermitRootLogin "
                "и настройки SSH; повторная установка ОС не требуется."
            )
    _save_bootstrapped(host)
    return 0
