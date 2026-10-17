"""Формирование конфигурации локальных Homepage из provision.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from .common import InfraManagerError
from .guest_catalog import (
    GuestIdentity,
    find_guest_by_role,
    guest_identity,
    guest_management_address,
)
from .settings import PATHS, SETTINGS


PORTAL_ACTIONS = (
    (
        "status",
        "Проверить",
        "ST",
        "mdi-check-circle-outline-#34d399",
        "Проверка состояния",
    ),
    (
        "sync",
        "Синхронизировать",
        "SY",
        "mdi-sync-#38bdf8",
        "Обновление конфигурации",
    ),
    (
        "repair",
        "Исправить",
        "RP",
        "mdi-wrench-#fb923c",
        "Устранение проблем",
    ),
)
PORTAL_TARGETS = frozenset({"local", "home"})
PORTAL_DEFAULT_PORT = 3001


def _gateway_base_url() -> str:
    """Определить адрес портала по роли управляющего гостя, не по VMID."""
    manager = find_guest_by_role(
        PATHS.repo_root,
        SETTINGS.infra_manager_role,
    )
    address = guest_management_address(PATHS.repo_root, manager)
    if not address:
        raise InfraManagerError(
            "Не удалось определить административный адрес infra-manager"
        )
    return f"http://{address}:{SETTINGS.portal_gateway_port}"


def build_portal_bookmarks(vmid: int) -> list[dict]:
    """Сформировать блок Homepage со стандартными действиями гостя."""

    from .guest_operations import operation_guests

    guest_identity(PATHS.repo_root, vmid)
    gateway = _gateway_base_url()

    entries: list[dict] = []
    for operation, label, abbreviation, icon, description in PORTAL_ACTIONS:
        supported = {
            item.vmid for item in operation_guests(PATHS.repo_root, operation)
        }
        if vmid not in supported:
            continue

        entries.append(
            {
                label: [
                    {
                        "abbr": abbreviation,
                        "icon": icon,
                        "href": (
                            f"{gateway}/action/{operation}?vmid={vmid}"
                        ),
                        "description": description,
                    }
                ]
            }
        )

    if not entries:
        return []
    return [{"Управление": entries}]


def _load_provision(identity: GuestIdentity) -> dict[str, Any]:
    """Прочитать provision.yaml и подтвердить принадлежность к гостю."""
    path = identity.directory / "provision.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(
            f"Не удалось прочитать {path}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise InfraManagerError(f"{path}: ожидается YAML-объект")
    if data.get("guest_vmid") != identity.vmid:
        raise InfraManagerError(
            f"{path}: guest_vmid не соответствует guest.yaml"
        )
    return data


def local_portal_definition(
    repo_root: Path,
    identity: GuestIdentity,
) -> dict[str, Any] | None:
    """Вернуть описание локальной панели Homepage гостя."""

    provision = _load_provision(identity)
    portal = provision.get("portal")
    if not isinstance(portal, dict):
        return None

    local = portal.get("local")
    if not isinstance(local, dict) or local.get("enabled") is not True:
        return None

    title = local.get("title")
    if not isinstance(title, str) or not title.strip():
        raise InfraManagerError(
            f"{identity.directory / 'provision.yaml'}: "
            "portal.local.title должен быть непустой строкой"
        )

    port = local.get("port", PORTAL_DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise InfraManagerError(
            f"{identity.directory / 'provision.yaml'}: "
            "portal.local.port должен быть числом от 1 до 65535"
        )

    trusted_ca = local.get("trusted_ca")
    if trusted_ca is not None and (
        not isinstance(trusted_ca, str) or not trusted_ca.startswith("/")
    ):
        raise InfraManagerError(
            f"{identity.directory / 'provision.yaml'}: "
            "portal.local.trusted_ca должен быть абсолютным путём"
        )

    address = guest_management_address(repo_root, identity)
    if not address:
        raise InfraManagerError(
            f"Для гостя {identity.vmid} не определён административный IPv4"
        )

    return {
        "name": title.strip(),
        "scheme": "http",
        "port": port,
        "path": "/",
        "url": f"http://{address}:{port}/",
        "trusted_ca": trusted_ca,
    }


def _walk_portals(value: Any, source: str = "provision") -> list[tuple[str, dict]]:
    """Рекурсивно найти объявления портала с указанием исходного раздела."""
    found: list[tuple[str, dict]] = []
    if isinstance(value, dict):
        portal = value.get("portal")
        if isinstance(portal, dict) and "targets" in portal:
            found.append((source, portal))
        for key, child in value.items():
            if key == "portal":
                continue
            found.extend(_walk_portals(child, f"{source}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found.extend(_walk_portals(child, f"{source}[{index}]"))
    return found


def _validated_operator(source: str, operator: object) -> dict[str, Any] | None:
    """Проверить формат операторских учётных записей для портала.

    Неполные или противоречивые описания отвергаются до публикации."""
    if operator is None:
        return None
    if not isinstance(operator, dict):
        raise InfraManagerError(
            f"{source}.portal.operator должен быть объектом"
        )

    accounts = operator.get("accounts")
    if accounts is not None:
        if (
            not isinstance(accounts, list)
            or not accounts
            or any(not isinstance(item, dict) or "accounts" in item for item in accounts)
            or "username" in operator
            or "password" in operator
        ):
            raise InfraManagerError(
                f"{source}.portal.operator.accounts должен быть списком учётных записей"
            )
        validated = [
            _validated_operator(f"{source}.accounts[{index}]", account)
            for index, account in enumerate(accounts)
        ]
        if any(not item or not item.get("username") or not item.get("password") for item in validated):
            raise InfraManagerError(
                f"{source}.portal.operator.accounts требует логин и пароль"
            )
        return {"accounts": validated}

    username = operator.get("username")
    if username is not None and (
        not isinstance(username, str) or not username.strip()
    ):
        raise InfraManagerError(
            f"{source}.portal.operator.username должен быть непустой строкой"
        )

    password = operator.get("password")
    normalized_password: dict[str, str] | None = None
    if password is not None:
        if not isinstance(password, dict):
            raise InfraManagerError(
                f"{source}.portal.operator.password должен быть объектом"
            )
        source_type = password.get("source")
        if source_type not in {"guest_file", "guest_env", "guest_json", "pve_json", "guest_credential"}:
            raise InfraManagerError(
                f"{source}.portal.operator.password.source имеет "
                f"неподдерживаемое значение {source_type!r}"
            )
        path = password.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise InfraManagerError(
                f"{source}.portal.operator.password.path "
                "должен быть абсолютным путём"
            )

        normalized_password = {
            "source": source_type,
            "path": path,
        }
        if source_type == "guest_credential":
            users_file = password.get("users_file")
            if not isinstance(users_file, str) or not users_file.startswith("/"):
                raise InfraManagerError(
                    f"{source}.portal.operator.password.users_file должен быть абсолютным путём"
                )
            normalized_password["users_file"] = users_file
        if source_type in {"guest_env", "guest_json", "pve_json"}:
            key = password.get("key")
            if not isinstance(key, str) or not key.strip():
                raise InfraManagerError(
                    f"{source}.portal.operator.password.key "
                    "должен быть непустой строкой"
                )
            normalized_password["key"] = key.strip()

    if username is None and normalized_password is None:
        raise InfraManagerError(
            f"{source}.portal.operator должен задавать username или password"
        )

    result: dict[str, Any] = {}
    if isinstance(username, str):
        result["username"] = username.strip()
    if normalized_password is not None:
        result["password"] = normalized_password
    return result


def _validated_portal(source: str, portal: dict) -> dict[str, Any]:
    """Проверить адрес, параметры и цели локального портала.

    Недопустимые значения не должны попадать в формируемые настройки."""
    name = portal.get("name")
    scheme = portal.get("scheme")
    port = portal.get("port")
    path = portal.get("path", "/")
    targets = portal.get("targets")
    icon = portal.get("icon")
    description = portal.get("description")

    if not isinstance(name, str) or not name.strip():
        raise InfraManagerError(
            f"{source}.portal.name должен быть непустой строкой"
        )
    if scheme not in {"http", "https"}:
        raise InfraManagerError(
            f"{source}.portal.scheme должен быть http или https"
        )
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise InfraManagerError(
            f"{source}.portal.port должен быть числом от 1 до 65535"
        )
    if not isinstance(path, str) or not path.startswith("/"):
        raise InfraManagerError(
            f"{source}.portal.path должен начинаться с /"
        )
    if not isinstance(targets, list) or not targets:
        raise InfraManagerError(
            f"{source}.portal.targets должен быть непустым списком"
        )
    if any(not isinstance(item, str) for item in targets):
        raise InfraManagerError(
            f"{source}.portal.targets должен содержать строки"
        )
    unknown = set(targets) - PORTAL_TARGETS
    if unknown:
        raise InfraManagerError(
            f"{source}.portal.targets содержит неизвестные значения: "
            f"{', '.join(sorted(unknown))}"
        )
    if len(set(targets)) != len(targets):
        raise InfraManagerError(
            f"{source}.portal.targets содержит повторы"
        )
    if icon is not None and (
        not isinstance(icon, str) or not icon.strip()
    ):
        raise InfraManagerError(
            f"{source}.portal.icon должен быть непустой строкой"
        )
    if description is not None and (
        not isinstance(description, str) or not description.strip()
    ):
        raise InfraManagerError(
            f"{source}.portal.description должен быть непустой строкой"
        )

    return {
        "name": name.strip(),
        "scheme": scheme,
        "port": port,
        "path": path,
        "targets": targets,
        "icon": icon.strip() if isinstance(icon, str) else None,
        "description": (
            description.strip() if isinstance(description, str) else None
        ),
        "operator": _validated_operator(source, portal.get("operator")),
    }


def portal_definitions(
    repo_root: Path,
    identity: GuestIdentity,
) -> list[dict[str, Any]]:
    """Вернуть канонические описания опубликованных служб гостя."""

    address = guest_management_address(repo_root, identity)
    if not address:
        raise InfraManagerError(
            f"Для гостя {identity.vmid} не определён административный IPv4"
        )

    provision = _load_provision(identity)
    result: list[dict[str, Any]] = []
    names: set[str] = set()

    for source, raw in _walk_portals(provision):
        portal = _validated_portal(source, raw)
        name = portal["name"]
        if name in names:
            raise InfraManagerError(
                f"У гостя {identity.vmid} повторяется имя службы {name!r}"
            )
        names.add(name)

        result.append(
            {
                **portal,
                "source": source,
                "url": (
                    f"{portal['scheme']}://{address}:{portal['port']}"
                    f"{portal['path']}"
                ),
            }
        )
    return result



def operator_service_definitions(
    repo_root: Path,
    identity: GuestIdentity,
) -> list[dict[str, Any]]:
    """Собрать операторские адреса без привязки к Homepage или VMID.

    Поле operator_status задаётся рядом с public_url соответствующей службы.
    Это только описание для вывода; опубликованные порты проверяет status.yaml.
    """
    provision = _load_provision(identity)
    result: list[dict[str, Any]] = []
    names: set[str] = set()

    def walk(value: Any, source: str) -> None:
        if isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{source}[{index}]")
            return
        if not isinstance(value, dict):
            return

        definition = value.get("operator_status")
        if definition is not None:
            if not isinstance(definition, dict):
                raise InfraManagerError(f"{source}.operator_status должен быть объектом")
            name = definition.get("name")
            url = value.get("public_url")
            if not isinstance(name, str) or not name.strip():
                raise InfraManagerError(f"{source}.operator_status.name должен быть строкой")
            if not isinstance(url, str):
                raise InfraManagerError(f"{source}.public_url должен быть строкой")
            parsed = urlsplit(url)
            if (
                parsed.scheme not in {"https", "http"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query or parsed.fragment
            ):
                raise InfraManagerError(f"{source}.public_url не является безопасным веб-адресом")
            if name in names:
                raise InfraManagerError(f"Повторяется операторская служба {name}")
            names.add(name)
            via = definition.get("authentication")
            if via is not None and (not isinstance(via, str) or not via.strip()):
                raise InfraManagerError(f"{source}.operator_status.authentication должен быть строкой")
            setup_path = definition.get("setup_path")
            if setup_path is not None and (
                not isinstance(setup_path, str)
                or not setup_path.startswith("/")
                or setup_path.startswith("//")
                or not setup_path[1:]
                or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789/-" for char in setup_path[1:])
                or "//" in setup_path
            ):
                raise InfraManagerError(
                    f"{source}.operator_status.setup_path должен быть безопасным относительным путём"
                )
            result.append({
                "name": name.strip(),
                "url": url,
                "setup_url": url.rstrip("/") + setup_path if setup_path else None,
                "operator": _validated_operator(
                    f"{source}.operator_status", definition.get("operator")
                ),
                "authentication": via,
            })

        for key, child in value.items():
            if key != "operator_status":
                walk(child, f"{source}.{key}")

    walk(provision, "provision")
    return result


def portal_services(vmid: int, target: str) -> list[dict[str, Any]]:
    """Вернуть опубликованные службы гостя для логического назначения."""

    if target not in PORTAL_TARGETS:
        raise InfraManagerError(
            f"Неизвестное назначение портала: {target}"
        )

    identity = guest_identity(PATHS.repo_root, vmid)
    result: list[dict[str, Any]] = []

    for portal in portal_definitions(PATHS.repo_root, identity):
        if target not in portal["targets"]:
            continue

        name = portal["name"]
        url = portal["url"]
        service: dict[str, Any] = {"href": url}
        icon = portal.get("icon")
        if isinstance(icon, str):
            service["icon"] = icon
        description = portal.get("description")
        if isinstance(description, str):
            service["description"] = description
        service["siteMonitor"] = url
        result.append({name: service})

    return result


def build_portal_services(vmid: int) -> list[dict]:
    """Сформировать services.yaml локального Homepage."""

    services = portal_services(vmid, "local")
    if not services:
        return []
    return [{"Службы": services}]


def render_portal_services(vmid: int) -> str:
    """Вернуть services.yaml локального Homepage."""

    return yaml.safe_dump(
        build_portal_services(vmid),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def render_portal_bookmarks(vmid: int) -> str:
    """Вернуть bookmarks.yaml для одного гостя."""

    return yaml.safe_dump(
        build_portal_bookmarks(vmid),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def _write_yaml(output: Path, content: str) -> None:
    """Записать результат через временный файл и атомарную замену.

    Это снижает риск частично записанного YAML при ошибке записи."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(output)


def write_portal_services(vmid: int, output: Path) -> None:
    """Записать services.yaml атомарной заменой."""

    _write_yaml(output, render_portal_services(vmid))


def write_portal_bookmarks(vmid: int, output: Path) -> None:
    """Записать bookmarks.yaml атомарной заменой."""

    _write_yaml(output, render_portal_bookmarks(vmid))
