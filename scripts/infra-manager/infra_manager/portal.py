"""Формирование ссылок локальных Homepage на стандартные операции гостя."""

from __future__ import annotations

from .common import InfraManagerError
from .guest_catalog import (
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


def render_portal_bookmarks(vmid: int) -> str:
    """Вернуть bookmarks.yaml для одного гостя."""

    import yaml

    return yaml.safe_dump(
        build_portal_bookmarks(vmid),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def write_portal_bookmarks(vmid: int, output) -> None:
    """Записать bookmarks.yaml атомарной заменой."""

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(render_portal_bookmarks(vmid), encoding="utf-8")
    temporary.replace(output)
