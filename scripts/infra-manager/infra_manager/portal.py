"""Формирование конфигурации локальных Homepage из provision.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .common import InfraManagerError
from .guest_catalog import (
    GuestIdentity,
    find_guest_by_role,
    guest_identity,
    guest_management_address,
)
from .guest_operations import operation_guests
from .settings import PATHS, SETTINGS


PORTAL_ACTIONS = (
    ("status", "Проверить", "ST"),
    ("sync", "Синхронизировать", "SY"),
    ("repair", "Исправить", "RP"),
)
PORTAL_TARGETS = frozenset({"local", "home"})


def _gateway_base_url() -> str:
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

    guest_identity(PATHS.repo_root, vmid)
    gateway = _gateway_base_url()

    entries: list[dict] = []
    for operation, label, abbreviation in PORTAL_ACTIONS:
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
                        "href": (
                            f"{gateway}/action/{operation}?vmid={vmid}"
                        ),
                        "description": f"Гость {vmid}",
                    }
                ]
            }
        )

    if not entries:
        return []
    return [{"Управление": entries}]


def _load_provision(identity: GuestIdentity) -> dict[str, Any]:
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


def _walk_portals(value: Any, source: str = "provision") -> list[tuple[str, dict]]:
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


def _validated_portal(source: str, portal: dict) -> dict[str, Any]:
    name = portal.get("name")
    scheme = portal.get("scheme")
    port = portal.get("port")
    path = portal.get("path", "/")
    targets = portal.get("targets")

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

    return {
        "name": name.strip(),
        "scheme": scheme,
        "port": port,
        "path": path,
        "targets": targets,
    }


def portal_services(vmid: int, target: str) -> list[dict[str, Any]]:
    """Вернуть опубликованные службы гостя для логического назначения."""

    if target not in PORTAL_TARGETS:
        raise InfraManagerError(
            f"Неизвестное назначение портала: {target}"
        )

    identity = guest_identity(PATHS.repo_root, vmid)
    address = guest_management_address(PATHS.repo_root, identity)
    if not address:
        raise InfraManagerError(
            f"Для гостя {vmid} не определён административный IPv4"
        )

    provision = _load_provision(identity)
    result: list[dict[str, Any]] = []
    names: set[str] = set()

    for source, raw in _walk_portals(provision):
        portal = _validated_portal(source, raw)
        if target not in portal["targets"]:
            continue

        name = portal["name"]
        if name in names:
            raise InfraManagerError(
                f"Для назначения {target} повторяется имя службы {name!r}"
            )
        names.add(name)

        url = (
            f"{portal['scheme']}://{address}:{portal['port']}"
            f"{portal['path']}"
        )
        service: dict[str, Any] = {"href": url}
        if portal["scheme"] == "http":
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
