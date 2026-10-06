#!/usr/bin/env python3
"""Модульные и контрактные проверки состояния infra-manager."""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import semaphore as semaphore_module
from infra_manager import status as status_module
from infra_manager.common import InfraManagerError
from infra_manager.guest_operations import operation_guests
from infra_manager.semaphore import (
    PROJECT_REPO,
    SemaphoreClient,
    find_unique_by_name,
    find_unique_by_title,
    require_unique_by_name,
    semaphore_templates,
    semaphore_views,
    sync_project_from_task,
)
from infra_manager.settings import SETTINGS
from infra_manager.status import (
    SemaphoreSnapshot,
    _check_git_branch_contract,
    _dict_items,
    _project_branch,
    _show_full_status,
    check_status,
    load_semaphore_snapshot,
    load_status_definition,
    validate_semaphore_snapshot,
)

SEMAPHORE_TEMPLATES = semaphore_templates(ROOT)


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

    expected_templates = {
        "OpenTofu Plan": ("scripts/infra-manager/jobs/opentofu-plan.py", "[]"),
        "Build Template 9000": (
            "scripts/infra-manager/jobs/build-template.py",
            '["9000"]',
        ),
        "Set Log Level": (
            "scripts/infra-manager/jobs/set-log-level.py",
            "[]",
        ),
        "Deploy Guest": (
            "scripts/infra-manager/jobs/guest-operation.py",
            '["deploy"]',
        ),
        "Status Guest": (
            "scripts/infra-manager/jobs/guest-operation.py",
            '["status"]',
        ),
        "Repair Guest": (
            "scripts/infra-manager/jobs/guest-operation.py",
            '["repair"]',
        ),
        "Test Guest": (
            "scripts/infra-manager/jobs/guest-operation.py",
            '["test"]',
        ),
        "Sync Guest": (
            "scripts/infra-manager/jobs/guest-operation.py",
            '["sync"]',
        ),
        "Sync SSH Access": (
            "scripts/infra-manager/jobs/sync-ssh-access.py",
            "[]",
        ),
    }
    actual_templates = {
        spec.name: (spec.playbook, spec.arguments)
        for spec in SEMAPHORE_TEMPLATES
    }
    if actual_templates != expected_templates:
        fail(f"Неожиданный состав шаблонов Semaphore: {actual_templates!r}")

    expected_views = [
        ("Guests", 1),
        ("Infrastructure", 2),
        ("Security", 3),
    ]
    actual_views = [
        (spec.title, spec.position)
        for spec in semaphore_views()
    ]
    if actual_views != expected_views:
        fail(f"Неожиданный состав Views Semaphore: {actual_views!r}")

    expected_template_views = {
        "Deploy Guest": "Guests",
        "Status Guest": "Guests",
        "Repair Guest": "Guests",
        "Test Guest": "Guests",
        "Sync Guest": "Guests",
        "OpenTofu Plan": "Infrastructure",
        "Build Template 9000": "Infrastructure",
        "Set Log Level": "Infrastructure",
        "Sync SSH Access": "Security",
    }
    if {
        spec.name: spec.view for spec in SEMAPHORE_TEMPLATES
    } != expected_template_views:
        fail("Шаблоны Semaphore распределены по Views неверно")

    operation_templates = {
        "Deploy Guest": "deploy",
        "Status Guest": "status",
        "Repair Guest": "repair",
        "Test Guest": "test",
        "Sync Guest": "sync",
    }
    for template_name, operation in operation_templates.items():
        template = next(
            spec for spec in SEMAPHORE_TEMPLATES if spec.name == template_name
        )
        expected_values = [
            {
                "name": f"{guest.vmid} — {guest.name}",
                "value": str(guest.vmid),
            }
            for guest in operation_guests(ROOT, operation)
        ]
        if len(template.survey_vars) != 1:
            fail(f"{template_name} должен иметь одну survey-переменную")
        guest_survey = template.survey_vars[0]
        if (
            guest_survey.get("name") != "GUEST_VMID"
            or guest_survey.get("type") != "enum"
            or guest_survey.get("required") is not True
            or guest_survey.get("values") != expected_values
        ):
            fail(
                f"{template_name} получил неверный список гостей"
            )

    log_template = next(
        spec for spec in SEMAPHORE_TEMPLATES if spec.name == "Set Log Level"
    )
    if len(log_template.survey_vars) != 1:
        fail("Set Log Level должен иметь одну survey-переменную")
    log_survey = log_template.survey_vars[0]
    if (
        log_survey.get("name") != "INFRA_LOG_LEVEL"
        or log_survey.get("type") != "enum"
        or log_survey.get("required") is not True
        or log_survey.get("values") != [
            {"name": "Обычный", "value": "normal"},
            {"name": "Подробный", "value": "verbose"},
            {"name": "Тихий", "value": "quiet"},
        ]
    ):
        fail("Set Log Level получил неверный список режимов")

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        guest = fake / "infrastructure/guests/920-infra-manager/guest.yaml"
        guest.parent.mkdir(parents=True)
        guest.write_text(
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )
        (guest.parent / "provision.yaml").write_text(
            "schema_version: 1\nguest_vmid: 920\n",
            encoding="utf-8",
        )
        moved = {item.name: item for item in semaphore_templates(fake)}
    for template_name in (
        "Deploy Guest",
        "Status Guest",
        "Repair Guest",
        "Test Guest",
        "Sync Guest",
    ):
        moved_template = moved.get(template_name)
        if moved_template is None:
            fail(f"Шаблон {template_name} отсутствует")
        if moved_template.survey_vars[0].get("values") != [
            {"name": "920 — infra-manager", "value": "920"}
        ]:
            fail(
                f"{template_name} не строит выбор из guest.yaml + provision.yaml"
            )
    if any(name.startswith("Initialize OpenBao") for name in moved):
        fail("Initialize OpenBao не должен быть обычным шаблоном Semaphore")

    templates = [
        {
            "name": spec.name,
            "git_branch": branch,
            "environment_ids": [8, 10],
        }
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

    class InfraManagerEnvironmentClient(SemaphoreClient):
        def __init__(
            self,
            *,
            existing: dict | None,
            full: dict | None = None,
        ) -> None:
            self.existing = existing
            self.full = full or {}
            self.created: list[dict] = []
            self.updated: list[dict] = []

        def get(self, path: str, *, auth: str | None = None):
            del auth
            if path.endswith("/environment?sort=name&order=asc"):
                return [self.existing] if self.existing is not None else []
            if "/environment/" in path:
                return self.full
            raise AssertionError(f"Неожиданный GET: {path}")

        def post(self, path: str, payload, *, auth: str | None = None):
            del path, auth
            self.created.append(payload)
            return {"id": 10}

        def put(self, path: str, payload):
            del path
            self.updated.append(payload)
            return None

    created_env = InfraManagerEnvironmentClient(existing=None)
    if created_env.ensure_infra_manager_environment(1) != 10:
        fail("Новая Variable Group Infra Manager получила неверный id")
    created_payload = created_env.created[0]
    if created_payload.get("name") != SETTINGS.infra_manager_env_name:
        fail("Variable Group Infra Manager получила неверное имя")
    if created_payload.get("env") != '{"INFRA_LOG_LEVEL":"normal"}':
        fail("Новая Variable Group должна начинаться с INFRA_LOG_LEVEL=normal")

    existing_env = InfraManagerEnvironmentClient(
        existing={"id": 10, "name": SETTINGS.infra_manager_env_name},
        full={"env": '{"INFRA_LOG_LEVEL":"verbose"}'},
    )
    existing_env.ensure_infra_manager_environment(1)
    if existing_env.updated:
        fail("Ручной INFRA_LOG_LEVEL=verbose не должен перезаписываться")

    missing_level = InfraManagerEnvironmentClient(
        existing={"id": 10, "name": SETTINGS.infra_manager_env_name},
        full={"env": '{"OTHER":"value"}'},
    )
    missing_level.ensure_infra_manager_environment(1)
    if len(missing_level.updated) != 1:
        fail("Отсутствующий INFRA_LOG_LEVEL должен быть добавлен")
    updated_values = __import__("json").loads(missing_level.updated[0]["env"])
    if updated_values != {"OTHER": "value", "INFRA_LOG_LEVEL": "normal"}:
        fail("Добавление INFRA_LOG_LEVEL не должно удалять другие настройки")

    log_level_env = InfraManagerEnvironmentClient(
        existing={"id": 10, "name": SETTINGS.infra_manager_env_name},
        full={
            "json": "{}",
            "env": '{"OTHER":"value","INFRA_LOG_LEVEL":"normal"}',
            "secrets": [],
        },
    )
    log_level_env.set_infra_manager_log_level(1, "verbose")
    if len(log_level_env.updated) != 1:
        fail("Set Log Level должен обновлять Variable Group один раз")
    log_level_values = __import__("json").loads(
        log_level_env.updated[0]["env"]
    )
    if log_level_values != {
        "OTHER": "value",
        "INFRA_LOG_LEVEL": "verbose",
    }:
        fail("Set Log Level не должен удалять другие переменные")

    try:
        log_level_env.set_infra_manager_log_level(1, "debug")
    except InfraManagerError:
        pass
    else:
        fail("Set Log Level принял неподдерживаемый режим debug")

    if SETTINGS.opentofu_env_name != "OpenTofu PVE":
        fail("Имя Variable Group OpenTofu PVE изменилось")
    if SETTINGS.infra_manager_env_name != "Infra Manager":
        fail("Имя Variable Group Infra Manager изменилось")

    class ViewClient(SemaphoreClient):
        def __init__(self, existing):
            self.existing = existing
            self.posts = []
            self.puts = []

        def get(self, path: str, *, auth: str | None = None):
            del path, auth
            return [] if self.existing is None else [self.existing]

        def post(self, path: str, payload, *, auth: str | None = None):
            del path, auth
            self.posts.append(payload)
            return {"id": 31}

        def put(self, path: str, payload):
            del path
            self.puts.append(payload)
            return None

    create_view = ViewClient(existing=None)
    if create_view.ensure_view(
        1,
        title="Guests",
        position=1,
    ) != 31:
        fail("Создание Semaphore View вернуло неверный id")
    if create_view.posts[0] != {
        "project_id": 1,
        "title": "Guests",
        "position": 1,
    }:
        fail("Semaphore View создаётся с неверным контрактом")

    update_view = ViewClient(
        existing={
            "id": 32,
            "project_id": 1,
            "title": "Guests",
            "position": 7,
        }
    )
    if update_view.ensure_view(
        1,
        title="Guests",
        position=1,
    ) != 32:
        fail("Обновление Semaphore View вернуло неверный id")
    if update_view.puts[0] != {
        "id": 32,
        "project_id": 1,
        "title": "Guests",
        "position": 1,
    }:
        fail("Semaphore View не приводится к целевой позиции")

    class TemplateClient(SemaphoreClient):
        def __init__(self, existing):
            self.existing = existing
            self.posts = []
            self.puts = []

        def get(self, path: str, *, auth: str | None = None):
            del path, auth
            return [] if self.existing is None else [self.existing]

        def post(self, path: str, payload, *, auth: str | None = None):
            del path, auth
            self.posts.append(payload)
            return {"id": 22}

        def put(self, path: str, payload):
            del path
            self.puts.append(payload)
            return None

    create_template = TemplateClient(existing=None)
    if create_template.ensure_template(
        1,
        2,
        [8, 10],
        name="Test",
        playbook="job.py",
        branch="feature/test",
        arguments="[]",
        view_id=31,
    ) != 22:
        fail("Создание шаблона Semaphore вернуло неверный id")
    created = create_template.posts[0]
    if created.get("app") != "python":
        fail("Шаблон Semaphore должен выполняться как Python")
    if created.get("environment_ids") != [8, 10]:
        fail("Шаблон Semaphore должен получать обе Variable Group")
    if created.get("view_id") != 31:
        fail("Шаблон Semaphore должен быть привязан к View")
    if created.get("allow_override_args_in_task") is not False:
        fail("Шаблон Semaphore не должен разрешать замену аргументов")
    if created.get("allow_override_branch_in_task") is not False:
        fail("Шаблон Semaphore не должен разрешать замену Git-ветки")
    if created.get("survey_vars") != []:
        fail("Обычный шаблон Semaphore не должен получать survey-поля")

    survey = (
        {
            "name": "GUEST_VMID",
            "title": "Гость",
            "type": "enum",
            "required": True,
            "values": [{"name": "410 — ai-control", "value": "410"}],
        },
    )
    survey_template = TemplateClient(existing=None)
    survey_template.ensure_template(
        1,
        2,
        [8, 10],
        name="Deploy Guest",
        playbook="job.py",
        branch="feature/test",
        arguments="[]",
        view_id=31,
        survey_vars=survey,
    )
    if survey_template.posts[0].get("survey_vars") != list(survey):
        fail("Survey-переменная Deploy Guest не передаётся в Semaphore")

    update_template = TemplateClient(existing={"id": 23, "name": "Test"})
    if update_template.ensure_template(
        1,
        2,
        [8, 10],
        name="Test",
        playbook="job.py",
        branch="feature/test",
        arguments="[]",
        view_id=31,
    ) != 23:
        fail("Обновление шаблона Semaphore вернуло неверный id")
    if update_template.puts[0].get("id") != 23:
        fail("PUT шаблона Semaphore должен содержать id обновляемого объекта")

    class CleanupClient(SemaphoreClient):
        def __init__(self) -> None:
            self.deleted: list[str] = []

        def get(self, path: str, *, auth: str | None = None):
            del path, auth
            return [
                {"id": 1, "name": "Deploy Guest"},
                {"id": 2, "name": "Deploy Guest 410"},
                {"id": 3, "name": "Deploy Guest 910"},
                {"id": 4, "name": "Initialize OpenBao 910"},
                {"id": 5, "name": "My custom task"},
            ]

        def delete(self, path: str):
            self.deleted.append(path)
            return None

    cleanup_client = CleanupClient()
    cleanup_client.remove_obsolete_managed_templates(
        1,
        {"Deploy Guest"},
    )
    if cleanup_client.deleted != [
        "/project/1/templates/2",
        "/project/1/templates/3",
        "/project/1/templates/4",
    ]:
        fail("Синхронизация Semaphore неверно удаляет старые шаблоны")

    class TaskSyncClient:
        def __init__(self, token_valid: bool) -> None:
            self.auth_mode = "cookie"
            self._token_valid = token_valid

        def token_valid(self) -> bool:
            return self._token_valid

        def get(self, path: str):
            if path != "/projects":
                raise AssertionError(f"Неожиданный GET: {path}")
            return [{"id": 41, "name": "Proxmox Infrastructure"}]

    task_client = TaskSyncClient(token_valid=True)
    with (
        patch.object(
            semaphore_module,
            "SemaphoreClient",
            return_value=task_client,
        ),
        patch.object(semaphore_module, "nonempty", return_value=True),
        patch.object(semaphore_module, "persist_github_key"),
        patch.object(semaphore_module, "_sync_project_objects") as sync_objects,
    ):
        if sync_project_from_task(branch="main", repo_root=ROOT) != 0:
            fail("Безопасная синхронизация Semaphore вернула ошибку")
    if task_client.auth_mode != "token":
        fail("Sync Guest должен использовать существующий API token")
    sync_objects.assert_called_once_with(
        task_client,
        41,
        "main",
        ROOT,
        reuse_existing_git=True,
    )

    class ExistingGitClient:
        def __init__(self) -> None:
            self.puts: list[tuple[str, dict]] = []

        def get(self, path: str):
            if "/keys?" in path:
                return [
                    {
                        "id": 7,
                        "name": "GitHub project read-only",
                    }
                ]
            if "/repositories?" in path:
                return [
                    {
                        "id": 9,
                        "name": "proxmox",
                        "git_url": PROJECT_REPO,
                        "git_branch": "main",
                        "ssh_key_id": 7,
                    }
                ]
            raise AssertionError(f"Неожиданный GET: {path}")

        def put(self, path: str, payload: dict):
            self.puts.append((path, payload))
            return None

    existing_git_client = ExistingGitClient()
    if semaphore_module._require_existing_task_git(
        existing_git_client,
        41,
        "main",
    ) != (7, 9):
        fail("Task Sync не использует существующий Git-контур")
    if existing_git_client.puts:
        fail("Task Sync не должен переписывать Git-контур без изменений")

    order_events: list[str] = []

    class SyncOrderClient:
        def ensure_opentofu_environment(self, project_id: int) -> int:
            del project_id
            return 8

        def ensure_infra_manager_environment(self, project_id: int) -> int:
            del project_id
            return 10

        def ensure_view(
            self,
            project_id: int,
            *,
            title: str,
            position: int,
        ) -> int:
            del project_id, title, position
            return 31

        def remove_obsolete_managed_templates(
            self,
            project_id: int,
            expected_names: set[str],
        ) -> None:
            del project_id, expected_names

    def record_templates(repo_root: Path):
        if repo_root != ROOT:
            fail("Синхронизация получила неверный корень checkout")
        order_events.append("catalog")
        return ()

    def record_git(
        client,
        project_id: int,
        branch_name: str,
    ) -> tuple[int, int]:
        del client, project_id, branch_name
        order_events.append("git")
        return 7, 9

    with (
        patch.object(
            semaphore_module,
            "semaphore_templates",
            side_effect=record_templates,
        ),
        patch.object(
            semaphore_module,
            "_require_existing_task_git",
            side_effect=record_git,
        ),
    ):
        semaphore_module._sync_project_objects(
            SyncOrderClient(),
            41,
            "main",
            ROOT,
            reuse_existing_git=True,
        )
    if order_events != ["catalog", "git"]:
        fail(
            "Каталог гостей должен считываться до изменения Git-контура Semaphore"
        )

    invalid_task_client = TaskSyncClient(token_valid=False)
    with (
        patch.object(
            semaphore_module,
            "SemaphoreClient",
            return_value=invalid_task_client,
        ),
        patch.object(semaphore_module, "nonempty", return_value=True),
        patch.object(semaphore_module, "persist_github_key"),
        patch.object(semaphore_module, "_sync_project_objects") as sync_objects,
    ):
        try:
            sync_project_from_task(branch="main", repo_root=ROOT)
        except InfraManagerError:
            pass
        else:
            fail("Sync Guest не должен перевыпускать недействительный API token")
    sync_objects.assert_not_called()

    invalid_level = InfraManagerEnvironmentClient(
        existing={"id": 10, "name": SETTINGS.infra_manager_env_name},
        full={"env": '{"INFRA_LOG_LEVEL":"debug"}'},
    )
    try:
        invalid_level.ensure_infra_manager_environment(1)
    except InfraManagerError:
        pass
    else:
        fail("Некорректный INFRA_LOG_LEVEL должен отклоняться")

    view = find_unique_by_title(
        [{"id": 31, "title": "Guests"}],
        "Guests",
        "View",
    )
    if view is None or view.get("id") != 31:
        fail("find_unique_by_title не вернул ожидаемый View")

    try:
        find_unique_by_title(
            [
                {"id": 31, "title": "Guests"},
                {"id": 32, "title": "Guests"},
            ],
            "Guests",
            "View",
        )
    except InfraManagerError:
        pass
    else:
        fail("find_unique_by_title разрешил дубликаты")

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
            {"id": 10, "name": SETTINGS.infra_manager_env_name},
        ),
        views=(
            {"id": 31, "title": "Guests", "position": 1},
            {"id": 32, "title": "Infrastructure", "position": 2},
            {"id": 33, "title": "Security", "position": 3},
        ),
        templates=tuple(
            {
                "name": spec.name,
                "git_branch": branch,
                "app": spec.app,
                "playbook": spec.playbook,
                "arguments": spec.arguments,
                "view_id": {
                    "Guests": 31,
                    "Infrastructure": 32,
                    "Security": 33,
                }[spec.view],
                "survey_vars": [dict(item) for item in spec.survey_vars],
                "environment_ids": [8, 10],
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
            if path.endswith("/views"):
                return list(semaphore_snapshot.views)
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
        views=semaphore_snapshot.views,
        templates=semaphore_snapshot.templates,
    ):
        fail("load_semaphore_snapshot неверно собрал состояние проекта")
    if len(snapshot_client.paths) != 5:
        fail("Снимок Semaphore должен читать каждую коллекцию ровно один раз")

    status_file = (
        ROOT
        / "infrastructure"
        / "guests"
        / "910-infra-manager"
        / "infra-manager-status.yaml"
    )
    definition = load_status_definition(status_file)

    if definition.guest_vmid != 910:
        fail("Внутреннее описание состояния должно учитывать текущий VMID infra-manager")

    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp)
        guest_dir = fake / "infrastructure/guests/920-infra-manager"
        guest_dir.mkdir(parents=True)
        (guest_dir / "guest.yaml").write_text(
            "schema_version: 12\n"
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )
        moved_status = guest_dir / "infra-manager-status.yaml"
        moved_status.write_text(
            status_file.read_text(encoding="utf-8").replace(
                "guest_vmid: 910",
                "guest_vmid: 920",
                1,
            ),
            encoding="utf-8",
        )
        with patch.object(
            status_module,
            "PATHS",
            SimpleNamespace(repo_root=fake),
        ):
            moved_definition = load_status_definition(moved_status)
        if moved_definition.guest_vmid != 920:
            fail("status должен следовать VMID гостя с ролью infra-manager")
        operator_fields = [
            field
            for section in moved_definition.sections
            for field in section["fields"]
            if field["label"] == "Операторская команда"
        ]
        if len(operator_fields) != 1:
            fail("status должен содержать одну операторскую команду")
        operator_command = status_module._field_value(
            operator_fields[0],
            {"guest_vmid": "920"},
        )
        if operator_command != "infra-manager status":
            fail("status должен показывать единую PVE-команду")

    if [item["type"] for item in definition.checks] != [
        "runtime",
        "portal",
        "openbao",
        "semaphore",
        "runtime_tools",
        "pve_access",
    ]:
        fail("Порядок проверок должен задаваться status.yaml")

    with patch.object(
        status_module,
        "command_runner",
        SimpleNamespace(run=Mock(side_effect=[
            SimpleNamespace(returncode=0, stdout="true\n"),
            SimpleNamespace(
                returncode=0,
                stdout='{"initialized": true, "sealed": true}',
            ),
        ])),
    ):
        try:
            status_module._check_openbao(full=False)
        except InfraManagerError:
            pass
        else:
            fail("status принял инициализированный, но запечатанный OpenBao")

    with patch.object(
        status_module,
        "command_runner",
        SimpleNamespace(run=Mock(side_effect=[
            SimpleNamespace(returncode=0, stdout="true\n"),
            SimpleNamespace(
                returncode=0,
                stdout='{"initialized": false, "sealed": true}',
            ),
        ])),
    ):
        status_module._check_openbao(full=False)

    with patch.object(
        status_module,
        "command_runner",
        SimpleNamespace(run=Mock(side_effect=[
            SimpleNamespace(returncode=0, stdout="true\n"),
            SimpleNamespace(
                returncode=0,
                stdout='{"initialized": false, "sealed": true}',
            ),
        ])),
    ):
        try:
            status_module._check_openbao(full=True)
        except InfraManagerError:
            pass
        else:
            fail("Полный status принял неинициализированный OpenBao")

    with (
        patch.object(
            status_module,
            "command_runner",
            SimpleNamespace(run=Mock(side_effect=[
                SimpleNamespace(returncode=0, stdout="true\n"),
                SimpleNamespace(
                    returncode=0,
                    stdout='{"initialized": true, "sealed": false}',
                ),
            ])),
        ),
        patch.object(
            status_module,
            "read_env_file",
            return_value={"PVE_API_URL": "https://pve:8006"},
        ),
        patch.object(status_module, "check_openbao_kv") as check_kv,
        patch.object(
            status_module,
            "check_recovery_contour",
        ) as check_recovery,
    ):
        status_module._check_openbao(full=False)
    check_kv.assert_called_once_with("pve")
    check_recovery.assert_not_called()

    with (
        patch.object(
            status_module,
            "command_runner",
            SimpleNamespace(run=Mock(side_effect=[
                SimpleNamespace(returncode=0, stdout="true\n"),
                SimpleNamespace(
                    returncode=0,
                    stdout='{"initialized": true, "sealed": false}',
                ),
            ])),
        ),
        patch.object(
            status_module,
            "read_env_file",
            return_value={"PVE_API_URL": "https://pve:8006"},
        ),
        patch.object(status_module, "check_openbao_kv") as check_kv_full,
        patch.object(status_module, "_check_openbao_tls") as check_tls_full,
        patch.object(
            status_module,
            "check_openbao_operator_access",
        ) as check_operator_full,
        patch.object(
            status_module,
            "check_openbao_ssh_access",
        ) as check_ssh_access_full,
        patch.object(
            status_module,
            "check_recovery_contour",
        ) as check_recovery_full,
    ):
        status_module._check_openbao(full=True)
    check_kv_full.assert_called_once_with("pve")
    check_tls_full.assert_called_once_with()
    check_operator_full.assert_called_once_with("pve")
    check_ssh_access_full.assert_called_once_with("pve")
    check_recovery_full.assert_called_once_with("pve")

    portal_run = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout="true\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )
    with patch.object(
        status_module,
        "command_runner",
        SimpleNamespace(run=portal_run),
    ):
        status_module._check_portal()
    portal_calls = [call.args[0] for call in portal_run.call_args_list]
    if not any("homepage" in argv for argv in portal_calls):
        fail("status не проверяет контейнер Homepage")
    if not any(
        "http://127.0.0.1:3001/api/healthcheck" in argv
        for argv in portal_calls
    ):
        fail("status не проверяет healthcheck Homepage")
    if not any(
        "infra-manager-portal-gateway.service" in argv
        for argv in portal_calls
    ):
        fail("status не проверяет systemd-службу шлюза Homepage")
    if not any(
        f"http://127.0.0.1:{status_module.SETTINGS.portal_gateway_port}/health"
        in argv
        for argv in portal_calls
    ):
        fail("status не проверяет healthcheck шлюза Homepage")

    tls_run = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout='{"initialized": true, "sealed": false}',
            ),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )
    with (
        patch.object(status_module, "required_file") as require_tls_ca,
        patch.object(
            status_module,
            "_primary_ipv4",
            return_value="192.168.9.10",
        ),
        patch.object(
            status_module,
            "command_runner",
            SimpleNamespace(run=tls_run),
        ),
    ):
        status_module._check_openbao_tls()
    require_tls_ca.assert_called_once_with(status_module.OPENBAO_TLS_CA)
    tls_calls = [call.args[0] for call in tls_run.call_args_list]
    if not all(
        str(status_module.OPENBAO_TLS_CA) in argv
        for argv in tls_calls
    ):
        fail("TLS-проверки OpenBao не используют доверенный CA")
    if not any(
        "https://192.168.9.10:8202/v1/sys/health" in argv
        for argv in tls_calls
    ):
        fail("TLS-проверка OpenBao не обращается к машинному входу :8202")
    if not any(
        "https://192.168.9.10:8202/ui/" in argv
        for argv in tls_calls
    ):
        fail("Полный status не проверяет встроенный интерфейс OpenBao")

    values = {
        "address": "192.168.9.10",
        "project_branch": "main",
        "project_revision": "abc1234",
        "guest_vmid": "910",
    }
    output = io.StringIO()
    with (
        contextlib.redirect_stdout(output),
        patch.object(
            status_module,
            "console",
            SimpleNamespace(
                ok=lambda message: print(f"[ОК] {message}"),
            ),
        ),
    ):
        _show_full_status(definition, values)

    summary_text = output.getvalue()
    for expected in (
        "Внутренняя проверка infra-manager",
        "[ОК] Docker и infra-runtime работают",
        "[ОК] Homepage и его действия работают",
        "[ОК] OpenBao запущен",
        "[ОК] Semaphore работает",
        "[ОК] OpenTofu, Ansible и Packer готовы",
        "[ОК] Доступ к PVE подтверждён",
        "Адрес:   192.168.9.10",
        "Ветка:   main",
        "Версия:  abc1234",
        "infra-manager status",
        "infra-manager полностью готов",
    ):
        if expected not in summary_text:
            fail(f"Полный экран состояния не содержит: {expected}")

    with (
        patch.object(
            status_module,
            "load_status_definition",
            return_value=definition,
        ),
        patch.object(status_module, "_run_status_checks") as run_checks,
        patch.object(
            status_module,
            "_resolve_status_data",
            return_value=values,
        ) as resolve_data,
        patch.object(status_module, "_show_full_status") as show_summary,
    ):
        if check_status(full=True, quiet=True) != 0:
            fail("Скрытая полная проверка должна завершаться успешно")
        run_checks.assert_called_once()
        resolve_data.assert_called_once()
        show_summary.assert_not_called()

    print("Проверки состояния infra-manager пройдены.")


if __name__ == "__main__":
    main_test()
