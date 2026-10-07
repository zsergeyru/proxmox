"""Единая проверка и операторский вывод состояния гостей."""

from __future__ import annotations

import shlex
import socket
import tempfile
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

from .common import InfraManagerError, console, require_command, run
from .guest_catalog import GuestIdentity, guest_management_address
from .portal import local_portal_definition, portal_definitions
from .pve_host import read_pve_json_value, sign_ssh_client_key
from .settings import PATHS


COMMON_STATUS_SCHEMA_VERSION = 2
COMMON_CHECK_TYPES = frozenset(
    {"docker", "file", "http", "local_status", "systemd", "tcp"}
)


def _required_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(f"Не найден {label}: {path}")


def _ssh_material(
    node: str,
    directory: Path,
) -> tuple[Path, Path, Path]:
    """Создать краткоживущую SSH-идентичность для одной проверки."""

    require_command("ssh")
    require_command("ssh-keygen")
    _required_file(
        PATHS.ssh_host_ca_public_key,
        "публичный SSH host CA",
    )

    private_key = directory / "status_ed25519"
    public_key = directory / "status_ed25519.pub"
    certificate_path = directory / "status_ed25519-cert.pub"
    known_hosts = directory / "known_hosts"

    run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            "infra-manager-status",
            "-f",
            str(private_key),
        ]
    )
    private_key.chmod(0o600)

    certificate = sign_ssh_client_key(
        node,
        public_key.read_text(encoding="utf-8").strip(),
    )
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
    return private_key, certificate_path, known_hosts


def _run_guest_command(
    node: str,
    address: str,
    argv: list[str],
    *,
    capture_output: bool = True,
):
    """Выполнить заранее сформированную команду в госте по временному сертификату."""

    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise InfraManagerError("Некорректная команда проверки гостя")

    with tempfile.TemporaryDirectory(prefix="guest-status-") as temporary_dir:
        temporary = Path(temporary_dir)
        private_key, certificate_path, known_hosts = _ssh_material(
            node,
            temporary,
        )

        remote_command = shlex.join(argv)
        return run(
            [
                "ssh",
                "-i",
                str(private_key),
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
                remote_command,
            ],
            capture_output=capture_output,
        )


def _read_guest_file(node: str, address: str, path: str) -> str:
    target = Path(path)
    if not target.is_absolute():
        raise InfraManagerError(
            f"Путь операторского файла гостя должен быть абсолютным: {path}"
        )
    return _run_guest_command(
        node,
        address,
        ["cat", "--", path],
    ).stdout


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
            raise InfraManagerError(f"Операторский пароль в {path} пуст")
        return value

    if source == "guest_env":
        return _read_env_value(
            _read_guest_file(node, address, path),
            spec["key"],
            path,
        )

    if source == "pve_json":
        return read_pve_json_value(node, path, spec["key"])

    raise InfraManagerError(
        f"Неизвестный источник операторского пароля: {source}"
    )


def _load_common_checks(identity: GuestIdentity) -> tuple[dict[str, Any], ...]:
    path = identity.directory / "status.yaml"
    if not path.is_file():
        return ()

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(f"Не удалось прочитать {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise InfraManagerError(f"{path}: ожидается YAML-объект")

    if raw.get("schema_version") != COMMON_STATUS_SCHEMA_VERSION:
        raise InfraManagerError(
            f"{path}: неподдерживаемая версия описания состояния"
        )
    if raw.get("guest_vmid") != identity.vmid:
        raise InfraManagerError(
            f"{path}: guest_vmid не соответствует guest.yaml"
        )

    checks = raw.get("checks", [])
    if not isinstance(checks, list):
        raise InfraManagerError(f"{path}: checks должен быть списком")

    normalized: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise InfraManagerError(
                f"{path}: checks[{index}] должен быть объектом"
            )
        check_type = check.get("type")
        if check_type not in COMMON_CHECK_TYPES:
            raise InfraManagerError(
                f"{path}: неизвестный тип проверки {check_type!r}"
            )
        message = check.get("message")
        if not isinstance(message, str) or not message.strip():
            raise InfraManagerError(
                f"{path}: checks[{index}].message должен быть строкой"
            )
        normalized.append(dict(check))
    return tuple(normalized)


def _check_tcp(address: str, port: int) -> None:
    try:
        with socket.create_connection((address, port), timeout=5):
            pass
    except OSError as exc:
        raise InfraManagerError(
            f"TCP {address}:{port} недоступен: {exc}"
        ) from exc


def _run_declared_check(
    node: str,
    address: str,
    check: dict[str, Any],
) -> None:
    check_type = check["type"]

    if check_type == "tcp":
        port = check.get("port")
        if isinstance(port, bool) or not isinstance(port, int):
            raise InfraManagerError("tcp-проверка требует числовой port")
        _check_tcp(address, port)
        return

    if check_type == "docker":
        container = check.get("container")
        if not isinstance(container, str) or not container:
            raise InfraManagerError(
                "docker-проверка требует имя container"
            )
        result = _run_guest_command(
            node,
            address,
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Running}}",
                container,
            ],
        )
        if result.stdout.strip() != "true":
            raise InfraManagerError(
                f"Контейнер {container} не находится в состоянии running"
            )
        return

    if check_type == "systemd":
        unit = check.get("unit")
        if not isinstance(unit, str) or not unit:
            raise InfraManagerError(
                "systemd-проверка требует имя unit"
            )
        _run_guest_command(
            node,
            address,
            ["systemctl", "is-active", "--quiet", unit],
        )
        return

    if check_type == "file":
        path = check.get("path")
        if not isinstance(path, str) or not Path(path).is_absolute():
            raise InfraManagerError(
                "file-проверка требует абсолютный path"
            )
        _run_guest_command(
            node,
            address,
            ["test", "-s", path],
        )
        return

    if check_type == "local_status":
        command = check.get("command")
        if not isinstance(command, str) or not Path(command).is_absolute():
            raise InfraManagerError(
                "local_status требует абсолютный command"
            )
        _run_guest_command(
            node,
            address,
            [command, "--full", "--quiet"],
        )
        return

    if check_type == "http":
        url = check.get("url")
        if not isinstance(url, str) or not url:
            raise InfraManagerError("http-проверка требует url")
        resolved = url.format(address=address)
        request = Request(resolved, method="GET")
        try:
            with urlopen(request, timeout=5) as response:
                status = int(response.status)
        except Exception as exc:
            raise InfraManagerError(
                f"HTTP {resolved} недоступен: {exc}"
            ) from exc
        if not 200 <= status < 400:
            raise InfraManagerError(
                f"HTTP {resolved} вернул код {status}"
            )
        return

    raise InfraManagerError(f"Неизвестный тип проверки: {check_type}")


def verify_guest_status(
    repo_root: Path,
    identity: GuestIdentity,
    resource: dict[str, object],
) -> tuple[str, str, tuple[dict[str, Any], ...], tuple[str, ...]]:
    """Проверить базовое состояние и вернуть данные для единого вывода."""

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

    _check_tcp(address, 22)
    messages = ["SSH доступен"]

    dashboard = local_portal_definition(repo_root, identity)
    checked_ports: set[int] = set()
    if dashboard is not None:
        dashboard_port = dashboard["port"]
        _check_tcp(address, dashboard_port)
        checked_ports.add(dashboard_port)
        messages.append("Локальная панель доступна")

    services = tuple(portal_definitions(repo_root, identity))
    for service in services:
        port = service.get("port")
        if isinstance(port, int) and port not in checked_ports:
            _check_tcp(address, port)
            checked_ports.add(port)
        messages.append(f"{service['name']} доступен")

    for check in _load_common_checks(identity):
        _run_declared_check(node, address, check)
        messages.append(str(check["message"]))

    return node, address, services, tuple(messages)


def _service_fields(
    node: str,
    address: str,
    service: dict[str, Any],
    *,
    show_secrets: bool,
) -> list[tuple[str, str]]:
    fields = [("Адрес", str(service["url"]))]
    operator = service.get("operator")
    if not isinstance(operator, dict):
        return fields

    username = operator.get("username")
    if isinstance(username, str):
        fields.append(("Логин", username))

    password = operator.get("password")
    if isinstance(password, dict):
        value = (
            _resolve_password(node, address, password)
            if show_secrets
            else "скрыт"
        )
        fields.append(("Пароль", value))
    return fields


def show_guest_status(
    repo_root: Path,
    identity: GuestIdentity,
    resource: dict[str, object],
    *,
    full: bool = True,
    show_secrets: bool = False,
    project_branch: str | None = None,
    project_revision: str | None = None,
) -> None:
    """Проверить и показать единый статус любого поддерживаемого гостя."""

    node, address, services, messages = verify_guest_status(
        repo_root,
        identity,
        resource,
    )

    resource_type = resource.get("type")
    kind = (
        "VM"
        if resource_type == "qemu"
        else "LXC"
        if resource_type == "lxc"
        else str(resource_type)
    )

    if not full:
        console.ok(
            f"{identity.vmid} {identity.name}: {kind}, {address}, "
            "гость готов"
        )
        return

    separator = "=" * 60
    print()
    print(separator)
    print(f"  Гость {identity.vmid} — {identity.name}")
    print(separator)
    print()

    console.ok("Объект PVE запущен")
    for message in messages:
        console.ok(message)

    print("\nСистема")
    print(f"  {'Тип:':<12}{kind}")
    print(f"  {'Роль:':<12}{identity.role or '-'}")
    print(f"  {'Адрес:':<12}{address}")
    print(f"  {'Узел:':<12}{node}")
    dashboard = local_portal_definition(repo_root, identity)
    if dashboard is not None:
        print(f"  {'Панель:':<12}{dashboard['url']}")

    if project_branch is not None or project_revision is not None:
        print("\nПроект")
        print(f"  {'Ветка:':<12}{project_branch or 'не определена'}")
        print(f"  {'Версия:':<12}{project_revision or 'не определена'}")

    for service in services:
        fields = _service_fields(
            node,
            address,
            service,
            show_secrets=show_secrets,
        )
        width = max(len(label) for label, _ in fields) + 3
        print(f"\n{service['name']}")
        for label, value in fields:
            print(f"  {label + ':':<{width}}{value}")

    print()
    print(separator)
    print("  Гость полностью готов")
    print(separator)
