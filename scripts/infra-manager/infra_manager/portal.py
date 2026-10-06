"""Формирование ссылок локальных Homepage на операции Semaphore."""

from __future__ import annotations

from pathlib import Path

import yaml

from .common import InfraManagerError
from .guest_catalog import (
    find_guest_by_role,
    guest_identity,
    guest_management_address,
)
from .guest_operations import operation_guests
from .semaphore import (
    PROJECT_NAME,
    SemaphoreClient,
    find_unique_by_name,
    require_unique_by_name,
)
from .settings import PATHS, SETTINGS


PORTAL_ACTIONS = (
    ("status", "Проверить", "Status Guest", "ST"),
    ("sync", "Синхронизировать", "Sync Guest", "SY"),
    ("repair", "Исправить", "Repair Guest", "RP"),
)


def _semaphore_context() -> tuple[SemaphoreClient, int, str]:
    client = SemaphoreClient()
    if not client.token_valid():
        raise InfraManagerError(
            "Рабочий API token Semaphore отсутствует или недействителен"
        )
    client.auth_mode = "token"

    project = require_unique_by_name(
        client.get("/projects"),
        PROJECT_NAME,
        "project",
    )
    project_id = project.get("id")
    if not isinstance(project_id, int):
        raise InfraManagerError("Проект Semaphore имеет некорректный id")

    manager = find_guest_by_role(
        PATHS.repo_root,
        SETTINGS.infra_manager_role,
    )
    address = guest_management_address(PATHS.repo_root, manager)
    if not address:
        raise InfraManagerError(
            "Не удалось определить административный адрес infra-manager"
        )
    return client, project_id, address


def build_portal_bookmarks(vmid: int) -> list[dict]:
    """Сформировать блок Homepage со стандартными действиями гостя."""

    guest_identity(PATHS.repo_root, vmid)
    client, project_id, manager_address = _semaphore_context()
    templates = client.get(
        f"/project/{project_id}/templates?sort=name&order=asc"
    )

    entries: list[dict] = []
    for operation, label, template_name, abbreviation in PORTAL_ACTIONS:
        supported = {
            item.vmid for item in operation_guests(PATHS.repo_root, operation)
        }
        if vmid not in supported:
            continue

        template = find_unique_by_name(
            templates,
            template_name,
            "template",
        )
        if template is None:
            raise InfraManagerError(
                f"В Semaphore отсутствует шаблон '{template_name}'"
            )
        template_id = template.get("id")
        if not isinstance(template_id, int):
            raise InfraManagerError(
                f"Semaphore template '{template_name}' имеет некорректный id"
            )

        entries.append(
            {
                label: [
                    {
                        "abbr": abbreviation,
                        "href": (
                            f"http://{manager_address}:3000/project/"
                            f"{project_id}/templates/{template_id}"
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

    data = build_portal_bookmarks(vmid)
    return yaml.safe_dump(
        data,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def write_portal_bookmarks(vmid: int, output: Path) -> None:
    """Записать bookmarks.yaml атомарной заменой."""

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(render_portal_bookmarks(vmid), encoding="utf-8")
    temporary.replace(output)
