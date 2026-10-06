"""Единый операторский статус гостя и опубликованных служб."""

from __future__ import annotations

import shlex
import tempfile
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .guest_catalog import GuestIdentity, guest_management_address
from .portal import portal_definitions
from .pve_host import read_pve_json_value, sign_ssh_client_key
from .settings import PATHS


def _required_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(f"Не найден {label}: {path}")


def _read_guest_file(node: str, address: str, path: str) -> str:
    """Прочитать файл гостя по временному SSH-сертификату OpenBao."""

    target = Path(path)
    if not target.is_absolute():
        raise InfraManagerError(
            f"Путь операторского файла гостя должен быть абсолютным: {path}"
        )

    require_command("ssh")
    _required_file(
        PATHS.ansible_private_key,
        "закрытый ключ административного доступа к гостям",
    )
    _required_file(
        PATHS.ansible_public_key,
        "открытый ключ административного доступа к гостям",
    )
    _required_file(
        PATHS.ssh_host_ca_public_key,
        "публичный SSH host CA",
    )

    certificate = sign_ssh_client_key(
        node,
        PATHS.ansible_public_key.read_text(encoding="utf-8").strip(),
    )

    with tempfile.TemporaryDirectory(prefix="guest-status-") as temporary_dir:
        temporary = Path(temporary_dir)
        certificate_path = temporary / "guest_ed25519-cert.pub"
        known_hosts = temporary / "known_hosts"

        certificate_path.write_text(certificate + "\n", encoding="utf-8")
        certificate_path.chmod(0o600)

        host_ca = PATHS.ssh_host_ca_public_key.read_text(
            encoding="utf-8"
        ).strip()
        known_hosts.write_text(
            f"@cert-authority * {host_ca}\n",
            encoding="utf-8",
        )
        known_hosts.chmod(0o600)

        result = run(
            [
                "ssh",
                "-i",
                str(PATHS.ansible_private_key),
                "-o",
                f"CertificateFile={certificate_path}",
                "-o",
                f"UserKnownHostsFile={known_hosts}",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "ConnectTimeout=10",
                f"root@{address}",
                f"cat -- {shlex.quote(path)}",
            ],
            capture_output=True,
        )
    return result.stdout


def _read_env_value(content: str, key: str, path: str) -> str:
    prefix = f"{key}="
    matches = [
        line[len(prefix):]
        for line in content.splitlines()
        if line.startswith(prefix)
    ]
    if len(matches) != 1 or not matches[0]:
        raise InfraManagerError(
            f"Файл {path} должен содержать ровно одно непустое поле {key}"
        )
    return matches[0]


def _resolve_password(
    node: str,
    address: str,
    spec: dict[str, str],
) -> str:
    source = spec["source"]
    path = spec["path"]

    if source == "guest_file":
        value = _read_guest_file(node, address, path).strip()
        if not value:
            raise InfraManagerError(
                f"Операторский пароль в {path} пуст"
            )
        return value

    if source == "guest_env":
        key = spec["key"]
        return _read_env_value(
            _read_guest_file(node, address, path),
            key,
            path,
        )

    if source == "pve_json":
        return read_pve_json_value(node, path, spec["key"])

    raise InfraManagerError(
        f"Неизвестный источник операторского пароля: {source}"
    )


def _service_fields(
    node: str,
    address: str,
    service: dict[str, Any],
) -> list[tuple[str, str]]:
    fields = [("URL", str(service["url"]))]
    operator = service.get("operator")
    if not isinstance(operator, dict):
        return fields

    username = operator.get("username")
    if isinstance(username, str):
        fields.append(("Логин", username))

    password = operator.get("password")
    if isinstance(password, dict):
        fields.append(
            (
                "Пароль",
                _resolve_password(node, address, password),
            )
        )
    return fields


def show_guest_status(
    repo_root: Path,
    identity: GuestIdentity,
    resource: dict[str, object],
) -> None:
    """Показать единый операторский статус любого гостя проекта."""

    node = resource.get("node")
    if not isinstance(node, str) or not node:
        raise InfraManagerError(
            f"PVE не вернул узел для гостя {identity.vmid}"
        )

    address = guest_management_address(repo_root, identity)
    if not address:
        raise InfraManagerError(
            f"Для гостя {identity.vmid} не определён административный IPv4"
        )

    resource_type = resource.get("type")
    kind = "VM" if resource_type == "qemu" else "LXC" if resource_type == "lxc" else str(resource_type)

    services = portal_definitions(repo_root, identity)

    separator = "=" * 60
    print()
    print(separator)
    print(f"  Гость {identity.vmid} {identity.name}")
    print(separator)
    print()
    console.ok("Объект PVE запущен")

    print("\nСистема")
    print(f"  {'Тип:':<12}{kind}")
    print(f"  {'Адрес:':<12}{address}")
    print(f"  {'Узел:':<12}{node}")

    for service in services:
        fields = _service_fields(node, address, service)
        width = max(len(label) for label, _ in fields) + 3
        print(f"\n{service['name']}")
        for label, value in fields:
            print(f"  {label + ':':<{width}}{value}")

    print()
    print(separator)
    print("  Гость готов")
    print(separator)
