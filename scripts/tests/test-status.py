#!/usr/bin/env python3
"""Модульные и контрактные проверки состояния infra-manager."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import status as status_module
from infra_manager.common import InfraManagerError
from infra_manager.semaphore import (
    PROJECT_REPO,
    SEMAPHORE_TEMPLATES,
    find_unique_by_name,
    require_unique_by_name,
)
from infra_manager.settings import SETTINGS
from infra_manager.status import (
    SemaphoreSnapshot,
    _check_git_branch_contract,
    _dict_items,
    _project_branch,
    load_semaphore_snapshot,
    validate_semaphore_snapshot,
)


def fail(message: str) -> None:
    raise SystemExit(message)


def main_test() -> None:
    previous_branch = os.environ.get("INFRA_PROJECT_BRANCH")
    try:
        os.environ["INFRA_PROJECT_BRANCH"] = "feature/test-branch"
        if _project_branch() != "feature/test-branch":
            fail("status не учитывает INFRA_PROJECT_BRANCH")
    finally:
        if previous_branch is None:
            os.environ.pop("INFRA_PROJECT_BRANCH", None)
        else:
            os.environ["INFRA_PROJECT_BRANCH"] = previous_branch

    branch = "feature/test-branch"
    repository = {
        "git_url": PROJECT_REPO,
        "ssh_key_id": 7,
        "git_branch": branch,
    }
    template_names = [spec.name for spec in SEMAPHORE_TEMPLATES]
    if len(template_names) != len(set(template_names)):
        fail("SEMAPHORE_TEMPLATES содержит повторяющиеся имена")
    for spec in SEMAPHORE_TEMPLATES:
        if spec.app != "python":
            fail(f"Шаблон Semaphore '{spec.name}' имеет неожиданный тип приложения")
        if not (ROOT / spec.playbook).is_file():
            fail(f"Playbook Semaphore не существует: {spec.playbook}")

    templates = [
        {"name": spec.name, "git_branch": branch}
        for spec in SEMAPHORE_TEMPLATES
    ]
    _check_git_branch_contract(
        repository,
        ["main", branch],
        templates,
        github_key_id=7,
        project_branch=branch,
    )

    wrong_repository = dict(repository)
    wrong_repository["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            wrong_repository,
            ["main", branch],
            templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял неверную ветку репозитория")

    wrong_templates = [dict(item) for item in templates]
    wrong_templates[1]["git_branch"] = "main"
    try:
        _check_git_branch_contract(
            repository,
            ["main", branch],
            wrong_templates,
            github_key_id=7,
            project_branch=branch,
        )
    except InfraManagerError:
        pass
    else:
        fail("status принял неверную ветку шаблона")

    one = find_unique_by_name(
        [{"id": 1, "name": "proxmox"}],
        "proxmox",
        "Git repository",
    )
    if one is None or one.get("id") != 1:
        fail("find_unique_by_name не вернул единственный подходящий объект")

    none = find_unique_by_name(
        [{"id": 1, "name": "other"}],
        "proxmox",
        "Git repository",
    )
    if none is not None:
        fail("find_unique_by_name не вернул None для отсутствующего объекта")

    try:
        find_unique_by_name(
            [
                {"id": 1, "name": "proxmox"},
                {"id": 2, "name": "proxmox"},
            ],
            "proxmox",
            "Git repository",
        )
    except InfraManagerError:
        pass
    else:
        fail("find_unique_by_name разрешил дубликаты")

    try:
        require_unique_by_name([], "proxmox", "Git repository")
    except InfraManagerError:
        pass
    else:
        fail("require_unique_by_name принял отсутствующий объект")

    try:
        _dict_items(
            [{"id": 1}, "некорректный элемент"],
            "тестовые объекты",
        )
    except InfraManagerError:
        pass
    else:
        fail("status молча отбросил некорректный элемент списка Semaphore")

    semaphore_snapshot = SemaphoreSnapshot(
        project_id=1,
        github_key={
            "id": 7,
            "name": "GitHub project read-only",
            "type": "ssh",
        },
        repository=repository,
        branches=("main", branch),
        environments=(
            {"id": 8, "name": SETTINGS.opentofu_env_name},
        ),
        templates=tuple(
            {
                "name": spec.name,
                "git_branch": branch,
                "app": spec.app,
                "playbook": spec.playbook,
                "arguments": spec.arguments,
            }
            for spec in SEMAPHORE_TEMPLATES
        ),
    )
    validate_semaphore_snapshot(
        semaphore_snapshot,
        project_branch=branch,
    )

    class SnapshotClient:
        def __init__(self) -> None:
            self.paths: list[str] = []

        def get(self, path: str):
            self.paths.append(path)
            if "/keys?" in path:
                return [semaphore_snapshot.github_key]
            if "/repositories?" in path:
                return [{"id": 9, "name": "proxmox", **repository}]
            if "/environment?" in path:
                return list(semaphore_snapshot.environments)
            if "/templates?" in path:
                return list(semaphore_snapshot.templates)
            raise AssertionError(f"Неожиданный конечный адрес Semaphore API: {path}")

    snapshot_client = SnapshotClient()
    with patch.object(
        status_module,
        "_load_repository_branches",
        return_value=["main", branch],
    ):
        loaded_snapshot = load_semaphore_snapshot(snapshot_client, 1)
    if loaded_snapshot != SemaphoreSnapshot(
        project_id=1,
        github_key=semaphore_snapshot.github_key,
        repository={"id": 9, "name": "proxmox", **repository},
        branches=("main", branch),
        environments=semaphore_snapshot.environments,
        templates=semaphore_snapshot.templates,
    ):
        fail("load_semaphore_snapshot неверно собрал состояние проекта")
    if len(snapshot_client.paths) != 4:
        fail("Снимок Semaphore должен читать каждую коллекцию ровно один раз")

    print("Проверки состояния infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
