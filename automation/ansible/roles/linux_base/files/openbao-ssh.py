#!/usr/bin/env python3
"""Подключиться по SSH с одноразовым паролем OpenBao и строгим host CA."""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import subprocess
import sys
from pathlib import Path

OTP_CLIENT = Path("/usr/local/sbin/infra-openbao-otp")
KNOWN_HOSTS = Path("/etc/ssh/openbao-otp-known-hosts")


class OtpSshError(RuntimeError):
    """Ошибка безопасного SSH-подключения через OpenBao OTP."""


def validate_target(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise OtpSshError("Цель должна быть точным IPv4-адресом") from exc
    if address.version != 4:
        raise OtpSshError("Цель должна быть IPv4-адресом")
    return str(address)


def target_is_trusted(target: str, path: Path = KNOWN_HOSTS) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    prefix = f"@cert-authority {target} "
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise OtpSshError("Не удалось прочитать список доверенных SSH-целей") from exc
    return any(line.startswith(prefix) for line in lines)


def request_otp(target: str) -> str:
    if not OTP_CLIENT.is_file():
        raise OtpSshError(f"Не найден клиент OpenBao OTP: {OTP_CLIENT}")
    result = subprocess.run(
        [str(OTP_CLIENT), target],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode:
        message = result.stderr.strip()
        raise OtpSshError(message or "OpenBao не выдал SSH OTP")
    otp = result.stdout.strip()
    if not otp or len(otp) > 512 or any(char.isspace() for char in otp):
        raise OtpSshError("Клиент OpenBao вернул некорректный SSH OTP")
    return otp


def ssh_arguments(
    target: str,
    password_fd: int,
    remote_command: list[str],
) -> list[str]:
    return [
        "sshpass",
        "-d",
        str(password_fd),
        "ssh",
        "-o",
        "PreferredAuthentications=keyboard-interactive",
        "-o",
        "PubkeyAuthentication=no",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=yes",
        "-o",
        "NumberOfPasswordPrompts=1",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=no",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
        f"root@{target}",
        *remote_command,
    ]


def run_ssh(target: str, remote_command: list[str]) -> int:
    target_ip = validate_target(target)
    if not target_is_trusted(target_ip):
        raise OtpSshError(
            f"Цель {target_ip} отсутствует в доверенном списке SSH host CA"
        )
    if shutil.which("ssh") is None or shutil.which("sshpass") is None:
        raise OtpSshError("Для OTP-подключения требуются ssh и sshpass")

    otp = request_otp(target_ip)
    read_fd, write_fd = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            ssh_arguments(target_ip, read_fd, remote_command),
            pass_fds=(read_fd,),
        )
        os.close(read_fd)
        read_fd = -1
        os.write(write_fd, (otp + "\n").encode("utf-8"))
        otp = ""
        os.close(write_fd)
        write_fd = -1
        return process.wait()
    finally:
        otp = ""
        if read_fd >= 0:
            os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SSH к разрешённой цели через OpenBao OTP"
    )
    parser.add_argument("target", help="точный IPv4-адрес SSH-цели")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="необязательная удалённая команда",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    try:
        return run_ssh(args.target, command)
    except OtpSshError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
