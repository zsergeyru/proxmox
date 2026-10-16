"""Единая проверка и операторский вывод состояния гостей."""

from __future__ import annotations

import json
import shlex
import socket
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

import yaml

from .common import CommandError, InfraManagerError, console, run
from .guest_catalog import GuestIdentity, guest_management_address
from .portal import local_portal_definition, operator_service_definitions, portal_definitions
from .guest_ssh import GuestSshSession
from .pve_host import read_pve_json_value
from .settings import PATHS


COMMON_STATUS_SCHEMA_VERSION = 2
COMMON_CHECK_TYPES = frozenset(
    {"docker", "docker_image", "file", "http", "local_status", "systemd", "tcp"}
)


def _run_guest_command(
    node: str,
    address: str,
    argv: list[str],
    *,
    capture_output: bool = True,
):
    """Выполнить команду в госте через единый временный SSH-сеанс OpenBao."""
    if not argv or any(not isinstance(item, str) or not item for item in argv):
        raise InfraManagerError("Некорректная команда проверки гостя")

    with GuestSshSession(node, address) as session:
        return run(
            session.ssh_command(shlex.join(argv)),
            capture_output=capture_output,
        )


def _read_guest_file(node: str, address: str, path: str) -> str:
    """Прочитать операторский файл гостя только по абсолютному пути.

    Содержимое может включать секреты; вызывающая сторона определяет,
    какие поля разрешено показать пользователю."""
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
    """Выбрать ровно одно непустое значение ключа из файла переменных."""
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
    """Разрешить объявленный источник операторского пароля гостя.

    Пароль извлекается из файла или файла переменных; эта функция
    не должна сама выводить его в журнал."""
    source = spec["source"]
    path = spec["path"]

    if source == "guest_file":
        value = _read_guest_file(node, address, path).strip()
        if not value:
            raise InfraManagerError(f"Операторский пароль в {path} пуст")
        return value

    if source == "guest_credential":
        # Не раскрывать устаревший пароль после смены его в самом приложении.
        # В записи хранится хеш, соответствовавший паролю на момент создания.
        try:
            raw = _read_guest_file(node, address, path)
        except CommandError:
            return "не сохранён; требуется явный сброс"
        try:
            credential = json.loads(raw)
        except json.JSONDecodeError:
            return "сохранённая запись повреждена"
        username = credential.get("username") if isinstance(credential, dict) else None
        password = credential.get("password") if isinstance(credential, dict) else None
        fingerprint = credential.get("password_hash") if isinstance(credential, dict) else None
        if not all(isinstance(x, str) and x for x in (username, password, fingerprint)):
            return "сохранённая запись повреждена"
        try:
            users = yaml.safe_load(_read_guest_file(node, address, spec["users_file"]))
            entry = (users or {}).get("users", {}).get(username)
        except (CommandError, yaml.YAMLError, AttributeError, TypeError):
            return "не удалось проверить действующий пароль"
        if not isinstance(entry, dict) or entry.get("password") != fingerprint:
            return "пароль изменён в Authelia; сохранённый больше не показывается"
        return password

    if source == "guest_env":
        return _read_env_value(
            _read_guest_file(node, address, path),
            spec["key"],
            path,
        )

    if source == "guest_json":
        try:
            payload = json.loads(_read_guest_file(node, address, path))
        except json.JSONDecodeError as exc:
            raise InfraManagerError(f"Некорректный JSON с паролями: {path}") from exc
        value = payload.get(spec["key"]) if isinstance(payload, dict) else None
        if not isinstance(value, str) or not value:
            raise InfraManagerError(
                f"Файл {path} не содержит пароль для {spec['key']}"
            )
        return value

    if source == "pve_json":
        return read_pve_json_value(node, path, spec["key"])

    raise InfraManagerError(
        f"Неизвестный источник операторского пароля: {source}"
    )


def _load_common_checks(identity: GuestIdentity) -> tuple[dict[str, Any], ...]:
    """Загрузить проверки гостя из status.yaml с проверкой версии схемы.

    Ошибки описания не заменяются пустым списком проверок."""
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
    """Проверить доступность указанного порта с ограничением времени ожидания."""
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
    *,
    project_branch: str | None = None,
) -> None:
    """Выполнить только разрешённую проверку из status.yaml.

    Неизвестный тип или некорректные параметры считаются ошибкой
    описания, а не успешной проверкой службы."""
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

    if check_type == "docker_image":
        image = check.get("image")
        if not isinstance(image, str) or not image.strip():
            raise InfraManagerError(
                "docker_image-проверка требует имя image"
            )
        _run_guest_command(
            node,
            address,
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
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
        argv = [command, "--full", "--quiet"]
        if project_branch:
            argv = [
                "env",
                f"INFRA_PROJECT_BRANCH={project_branch}",
                *argv,
            ]
        result = _run_guest_command(
            node,
            address,
            argv,
        )
        for line in (result.stdout or "").splitlines():
            if line.startswith("[ПРЕДУПРЕЖДЕНИЕ] "):
                console.warning(line.removeprefix("[ПРЕДУПРЕЖДЕНИЕ] "))
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
    *,
    project_branch: str | None = None,
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
    # Доступность порта не равна успешному SSH-входу: проверяем
    # сертификат клиента и сертификат сервера на каждом Status Guest.
    _run_guest_command(node, address, ["true"])
    messages = ["SSH по сертификату доступен"]

    dashboard = local_portal_definition(repo_root, identity)
    checked_ports: set[int] = set()
    if dashboard is not None:
        dashboard_port = dashboard["port"]
        _check_tcp(address, dashboard_port)
        checked_ports.add(dashboard_port)
        messages.append("Локальная панель доступна")

    # Обычные порталы и веб-службы с public_url используют единый статус,
    # но только порталы объявляют проверяемый внутренний порт.
    services = tuple(
        portal_definitions(repo_root, identity)
        + operator_service_definitions(repo_root, identity)
    )
    for service in services:
        port = service.get("port")
        if isinstance(port, int) and port not in checked_ports:
            _check_tcp(address, port)
            checked_ports.add(port)
        messages.append(f"{service['name']} доступен")

    for check in _load_common_checks(identity):
        _run_declared_check(
            node,
            address,
            check,
            project_branch=project_branch,
        )
        messages.append(str(check["message"]))

    return node, address, services, tuple(messages)


def _service_fields(
    node: str,
    address: str,
    service: dict[str, Any],
    *,
    show_secrets: bool,
) -> list[tuple[str, str]]:
    """Собрать отображаемые поля службы и операторских учётных записей.

    Скрытие паролей определяется show_secrets; без явного разрешения
    секретные значения не должны отображаться."""
    fields = [("Адрес", str(service["url"]))]
    operator = service.get("operator")
    if not isinstance(operator, dict):
        return fields
    auth = service.get("authentication")

    accounts = operator.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            fields.extend(_operator_fields(node, address, account, show_secrets=show_secrets))
    else:
        fields.extend(_operator_fields(node, address, operator, show_secrets=show_secrets))
    if isinstance(auth, str) and auth:
        fields.append(("Пароль", f"через {auth}"))
    return fields


def _operator_fields(
    node: str,
    address: str,
    operator: dict[str, Any],
    *,
    show_secrets: bool,
) -> list[tuple[str, str]]:
    """Показать одну учётную запись, читая пароль только в доверенном режиме."""
    fields: list[tuple[str, str]] = []
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
        project_branch=project_branch,
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
