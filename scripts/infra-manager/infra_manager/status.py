"""Проверка готовности LXC infra-manager."""

from __future__ import annotations

import json
import shutil
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .common import InfraManagerError, command_runner, console
from .guest_catalog import find_guest_by_role
from .pve import PveClient, check_access, read_env_file
from .semaphore import (
    SemaphoreClient,
    find_unique_by_title,
    require_unique_by_name,
    semaphore_templates,
    semaphore_views,
)
from .pve_host import (
    check_openbao_kv,
    check_openbao_operator_access,
    check_openbao_ssh_access,
    check_recovery_contour,
)
from .settings import PATHS, SETTINGS

PVE_ENV = PATHS.pve_api_env
CA_BUNDLE = PATHS.ca_bundle
OPENBAO_TLS_CA = PATHS.openbao_tls_ca
ANSIBLE_PRIVATE_KEY = PATHS.ansible_private_key
ANSIBLE_PUBLIC_KEY = PATHS.ansible_public_key
SERVER_ENV = PATHS.server_env
GITHUB_KEY_COPY = PATHS.github_key_copy
PROJECT_ID_FILE = PATHS.semaphore_project_id_file
OPENTOFU_INPUT = PATHS.opentofu_input
OPENTOFU_STATE_DIR = PATHS.opentofu_state_dir
SEMAPHORE_API_TOKEN_FILE = PATHS.semaphore_api_token_file
STATUS_FILE = PATHS.status_file
PROJECT_REPO = SETTINGS.project_repo

STATUS_CHECK_TYPES = frozenset(
    {"runtime", "portal", "openbao", "semaphore", "runtime_tools", "pve_access"}
)
STATUS_DATA_SOURCE_TYPES = frozenset(
    {"primary_ipv4", "project_branch", "git_revision", "file"}
)


@dataclass(frozen=True)
class SemaphoreSnapshot:
    """Фактическое состояние проекта Semaphore за один цикл чтения."""

    project_id: int
    github_key: dict[str, Any]
    repository: dict[str, Any]
    branches: tuple[str, ...]
    environments: tuple[dict[str, Any], ...]
    views: tuple[dict[str, Any], ...]
    templates: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class StatusDefinition:
    """Проверки, данные и вид полного состояния гостя."""

    guest_vmid: int
    title: str
    short_ready_message: str
    ready_message: str
    separator: str
    separator_width: int
    checks: tuple[dict[str, Any], ...]
    data: dict[str, dict[str, Any]]
    sections: tuple[dict[str, Any], ...]


def required_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(
            f"Отсутствует обязательный файл: {path}"
        )


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InfraManagerError(
            f"status.yaml: {label} должен быть непустой строкой"
        )
    return value


def load_status_definition(path: Path | None = None) -> StatusDefinition:
    """Прочитать и проверить описание состояния infra-manager."""
    target = path or STATUS_FILE
    required_file(target)
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(
            f"Не удалось прочитать описание состояния: {target}"
        ) from exc

    if not isinstance(raw, dict):
        raise InfraManagerError("status.yaml должен содержать mapping/object")
    if raw.get("schema_version") != 1:
        raise InfraManagerError("status.yaml имеет неподдерживаемую версию")
    identity = find_guest_by_role(
        PATHS.repo_root,
        SETTINGS.infra_manager_role,
    )
    if raw.get("guest_vmid") != identity.vmid:
        raise InfraManagerError(
            "status.yaml должен описывать текущий VMID infra-manager "
            f"{identity.vmid}"
        )

    layout = raw.get("layout")
    if not isinstance(layout, dict):
        raise InfraManagerError("status.yaml: отсутствует layout")
    separator = _required_text(layout.get("separator"), "layout.separator")
    width = layout.get("width")
    if not isinstance(width, int) or not 20 <= width <= 120:
        raise InfraManagerError(
            "status.yaml: layout.width должен быть числом от 20 до 120"
        )

    checks = raw.get("checks")
    if not isinstance(checks, list) or not checks:
        raise InfraManagerError("status.yaml: checks должен быть непустым списком")
    normalized_checks: list[dict[str, Any]] = []
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise InfraManagerError(
                f"status.yaml: checks[{index}] должен быть объектом"
            )
        check_type = check.get("type")
        if check_type not in STATUS_CHECK_TYPES:
            raise InfraManagerError(
                f"status.yaml: неизвестный тип проверки {check_type!r}"
            )
        message = _required_text(
            check.get("message"),
            f"checks[{index}].message",
        )
        normalized_checks.append(
            {"type": str(check_type), "message": message}
        )

    data = raw.get("data")
    if not isinstance(data, dict) or not data:
        raise InfraManagerError("status.yaml: data должен быть непустым объектом")
    normalized_data: dict[str, dict[str, Any]] = {}
    for name, source in data.items():
        if not isinstance(name, str) or not name:
            raise InfraManagerError(
                "status.yaml: имя источника данных должно быть строкой"
            )
        if not isinstance(source, dict):
            raise InfraManagerError(
                f"status.yaml: источник {name!r} должен быть объектом"
            )
        source_type = source.get("type")
        if source_type not in STATUS_DATA_SOURCE_TYPES:
            raise InfraManagerError(
                f"status.yaml: неизвестный источник данных {source_type!r}"
            )
        if source_type == "git_revision":
            repository = source.get("repository")
            if not isinstance(repository, str) or not repository.startswith("/"):
                raise InfraManagerError(
                    f"status.yaml: {name}.repository должен быть абсолютным путём"
                )
        if source_type == "file":
            file_path = source.get("path")
            if not isinstance(file_path, str) or not file_path.startswith("/"):
                raise InfraManagerError(
                    f"status.yaml: {name}.path должен быть абсолютным путём"
                )
        normalized_data[name] = dict(source)

    sections = raw.get("sections")
    if not isinstance(sections, list) or not sections:
        raise InfraManagerError(
            "status.yaml: sections должен быть непустым списком"
        )
    normalized_sections: list[dict[str, Any]] = []
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise InfraManagerError(
                f"status.yaml: sections[{section_index}] должен быть объектом"
            )
        section_title = _required_text(
            section.get("title"),
            f"sections[{section_index}].title",
        )
        fields = section.get("fields")
        if not isinstance(fields, list) or not fields:
            raise InfraManagerError(
                f"status.yaml: sections[{section_index}].fields пуст"
            )
        normalized_fields: list[dict[str, str]] = []
        for field_index, field in enumerate(fields):
            if not isinstance(field, dict):
                raise InfraManagerError(
                    "status.yaml: поле секции должно быть объектом"
                )
            label = _required_text(
                field.get("label"),
                (
                    f"sections[{section_index}]."
                    f"fields[{field_index}].label"
                ),
            )
            value_keys = [
                key
                for key in ("source", "value", "template")
                if key in field
            ]
            if len(value_keys) != 1:
                raise InfraManagerError(
                    "status.yaml: поле должно иметь ровно один "
                    "source, value или template"
                )
            key = value_keys[0]
            value = _required_text(
                field.get(key),
                (
                    f"sections[{section_index}]."
                    f"fields[{field_index}].{key}"
                ),
            )
            if key == "source" and value not in normalized_data:
                raise InfraManagerError(
                    f"status.yaml: неизвестный источник {value!r}"
                )
            normalized_fields.append({"label": label, key: value})
        normalized_sections.append(
            {"title": section_title, "fields": normalized_fields}
        )

    return StatusDefinition(
        guest_vmid=int(raw["guest_vmid"]),
        title=_required_text(raw.get("title"), "title"),
        short_ready_message=_required_text(
            raw.get("short_ready_message"),
            "short_ready_message",
        ),
        ready_message=_required_text(
            raw.get("ready_message"),
            "ready_message",
        ),
        separator=separator,
        separator_width=width,
        checks=tuple(normalized_checks),
        data=normalized_data,
        sections=tuple(normalized_sections),
    )


def _project_branch() -> str:
    return SETTINGS.project_branch()


def _check_git_branch_contract(
    repository: dict[str, Any],
    branches: Any,
    templates: Any,
    *,
    github_key_id: int,
    project_branch: str,
) -> None:
    stored_key_id = repository.get("ssh_key_id")
    repository_matches = (
        repository.get("git_url") == PROJECT_REPO
        and str(stored_key_id or "") == str(github_key_id)
        and repository.get("git_branch") == project_branch
    )
    if not repository_matches:
        raise InfraManagerError(
            "Git repository 'proxmox' не соответствует "
            "ожидаемому URL, SSH key или ветке "
            f"'{project_branch}'"
        )

    if not isinstance(branches, (list, tuple)) or project_branch not in branches:
        raise InfraManagerError(
            "Semaphore не видит ветку "
            f"'{project_branch}' в Git repository 'proxmox'"
        )

    if not isinstance(templates, (list, tuple)):
        raise InfraManagerError(
            "Semaphore вернул некорректный список шаблонов"
        )
    for template in templates:
        if (
            isinstance(template, dict)
            and template.get("name") in {
                spec.name for spec in semaphore_templates()
            }
            and template.get("git_branch") != project_branch
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{template.get('name')}' "
                f"настроен не на ветку '{project_branch}'"
            )


def _check_local_prerequisites() -> None:
    """Проверить Docker, обязательные файлы и каталог состояния OpenTofu."""
    if shutil.which("docker") is None:
        raise InfraManagerError("Docker не установлен")
    if command_runner.run(
        ["docker", "compose", "version"],
        quiet=True,
        check=False,
    ).returncode:
        raise InfraManagerError("Docker Compose недоступен")

    for path in (
        PVE_ENV,
        SERVER_ENV,
        SEMAPHORE_API_TOKEN_FILE,
        GITHUB_KEY_COPY,
        PROJECT_ID_FILE,
        OPENTOFU_INPUT,
        CA_BUNDLE,
        ANSIBLE_PRIVATE_KEY,
        ANSIBLE_PUBLIC_KEY,
    ):
        required_file(path)

    if not OPENTOFU_STATE_DIR.is_dir():
        raise InfraManagerError("Отсутствует каталог OpenTofu state")


def _check_runtime() -> None:
    """Проверить, что контейнер infra-runtime запущен."""
    running = command_runner.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            "infra-runtime",
        ],
        capture=True,
        check=False,
    )
    if running.returncode or running.stdout.strip() != "true":
        raise InfraManagerError("Semaphore Server не запущен")


def _check_portal() -> None:
    """Проверить Homepage и шлюз стандартных действий."""

    running = command_runner.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            "homepage",
        ],
        capture=True,
        check=False,
    )
    if running.returncode or running.stdout.strip() != "true":
        raise InfraManagerError("Homepage не запущен")

    homepage = command_runner.run(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            "http://127.0.0.1:3001/api/healthcheck",
        ],
        quiet=True,
        check=False,
    )
    if homepage.returncode:
        raise InfraManagerError("Homepage :3001 не отвечает")

    gateway_service = command_runner.run(
        [
            "systemctl",
            "is-active",
            "--quiet",
            "infra-manager-portal-gateway.service",
        ],
        quiet=True,
        check=False,
    )
    if gateway_service.returncode:
        raise InfraManagerError("Шлюз действий Homepage не запущен")

    gateway = command_runner.run(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            f"http://127.0.0.1:{SETTINGS.portal_gateway_port}/health",
        ],
        quiet=True,
        check=False,
    )
    if gateway.returncode:
        raise InfraManagerError("Шлюз действий Homepage не отвечает")


def _check_openbao_tls() -> None:
    """Проверить внешний TLS-вход OpenBao по доверенному CA."""
    required_file(OPENBAO_TLS_CA)
    address = _primary_ipv4()
    response = command_runner.run(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            "--cacert",
            str(OPENBAO_TLS_CA),
            f"https://{address}:8202/v1/sys/health",
        ],
        capture=True,
        check=False,
    )
    if response.returncode:
        raise InfraManagerError(
            "TLS-вход OpenBao :8202 недоступен или сертификат не подтверждён"
        )

    try:
        payload = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            "TLS-вход OpenBao вернул некорректное состояние"
        ) from exc

    if (
        not isinstance(payload, dict)
        or payload.get("initialized") is not True
        or payload.get("sealed") is not False
    ):
        raise InfraManagerError(
            "TLS-вход OpenBao не подтверждает готовое разблокированное состояние"
        )

    ui = command_runner.run(
        [
            "curl",
            "-fsS",
            "--connect-timeout",
            "5",
            "--max-time",
            "10",
            "--cacert",
            str(OPENBAO_TLS_CA),
            "--output",
            "/dev/null",
            f"https://{address}:8202/ui/",
        ],
        quiet=True,
        check=False,
    )
    if ui.returncode:
        raise InfraManagerError(
            "Встроенный интерфейс OpenBao :8202/ui/ недоступен"
        )


def _check_openbao(*, full: bool) -> None:
    """Проверить OpenBao и при full также TLS/OTP и аварийный PVE-only контур."""
    running = command_runner.run(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            "openbao",
        ],
        capture=True,
        check=False,
    )
    if running.returncode or running.stdout.strip() != "true":
        raise InfraManagerError("OpenBao не запущен")

    response = command_runner.run(
        [
            "curl",
            "-fsS",
            "http://127.0.0.1:8200/v1/sys/seal-status",
        ],
        capture=True,
        check=False,
    )
    if response.returncode:
        raise InfraManagerError("OpenBao не возвращает состояние")

    try:
        payload = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            "OpenBao вернул некорректное состояние"
        ) from exc

    if not isinstance(payload, dict) or not isinstance(
        payload.get("initialized"), bool
    ) or not isinstance(payload.get("sealed"), bool):
        raise InfraManagerError(
            "OpenBao вернул неполное состояние"
        )

    if full and not payload["initialized"]:
        raise InfraManagerError(
            "OpenBao не инициализирован; полный статус infra-manager не может быть готов"
        )

    if payload["initialized"] and payload["sealed"]:
        raise InfraManagerError(
            "OpenBao инициализирован, но остаётся запечатан"
        )

    if payload["initialized"]:
        endpoint = read_env_file(PVE_ENV).get("PVE_API_URL", "")
        node = urlparse(endpoint).hostname if endpoint else None
        if not node:
            raise InfraManagerError(
                "Не удалось определить узел PVE для проверки OpenBao KV"
            )
        check_openbao_kv(node)
        if full:
            _check_openbao_tls()
            check_openbao_operator_access(node)
            check_openbao_ssh_access(node)
            check_recovery_contour(node)


def _load_repository_branches(
    semaphore: SemaphoreClient,
    project_id: int,
    repository_id: int,
) -> Any:
    """Получить доступные Git-ветки через сохранённый SSH key."""
    # Semaphore v2.18.30 создаёт socket временного ssh-agent в каталоге
    # проекта раньше, чем branches API успевает создать этот каталог.
    prepare_tmp = command_runner.run(
        [
            "docker",
            "exec",
            "--user",
            "1001:0",
            "infra-runtime",
            "mkdir",
            "-p",
            f"/tmp/semaphore/project_{project_id}",
        ],
        quiet=True,
        check=False,
    )
    if prepare_tmp.returncode:
        raise InfraManagerError(
            "Не удалось подготовить временный каталог Semaphore "
            "для проверки Git"
        )

    try:
        return semaphore.get(
            f"/project/{project_id}/repositories/"
            f"{repository_id}/branches"
        )
    except InfraManagerError as exc:
        raise InfraManagerError(
            "Semaphore не может прочитать Git repository 'proxmox' "
            "сохранённым SSH key"
        ) from exc


def _dict_items(value: Any, kind: str) -> tuple[dict[str, Any], ...]:
    """Нормализовать список объектов Semaphore для снимка."""

    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise InfraManagerError(
            f"Semaphore вернул некорректный список: {kind}"
        )
    return tuple(value)


def _survey_vars_match(
    actual: Any,
    expected: tuple[dict[str, Any], ...],
) -> bool:
    """Проверить управляемые поля survey-переменных, разрешая служебные поля API."""

    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for actual_item, expected_item in zip(actual, expected, strict=True):
        if not isinstance(actual_item, dict):
            return False
        for key, value in expected_item.items():
            if actual_item.get(key) != value:
                return False
    return True


def load_semaphore_snapshot(
    semaphore: SemaphoreClient,
    project_id: int,
) -> SemaphoreSnapshot:
    """Однократно получить данные проекта Semaphore для проверки."""

    keys = semaphore.get(
        f"/project/{project_id}/keys?sort=name&order=asc"
    )
    github_key = require_unique_by_name(
        keys,
        "GitHub project read-only",
        "SSH key",
    )

    repositories = semaphore.get(
        f"/project/{project_id}/repositories?sort=name&order=asc"
    )
    repository = require_unique_by_name(
        repositories,
        "proxmox",
        "Git repository",
    )
    repository_id = repository.get("id")
    if not isinstance(repository_id, int):
        raise InfraManagerError(
            "Git repository 'proxmox' имеет некорректный id"
        )

    branches = _load_repository_branches(
        semaphore,
        project_id,
        repository_id,
    )
    if not isinstance(branches, list) or not all(
        isinstance(branch, str) for branch in branches
    ):
        raise InfraManagerError(
            "Semaphore вернул некорректный список Git-веток"
        )

    environments = semaphore.get(
        f"/project/{project_id}/environment?sort=name&order=asc"
    )
    views = semaphore.get(f"/project/{project_id}/views")
    templates = semaphore.get(
        f"/project/{project_id}/templates?sort=name&order=asc"
    )

    return SemaphoreSnapshot(
        project_id=project_id,
        github_key=github_key,
        repository=repository,
        branches=tuple(branches),
        environments=_dict_items(environments, "Variable Group"),
        views=_dict_items(views, "Views"),
        templates=_dict_items(templates, "шаблоны"),
    )


def validate_semaphore_snapshot(
    snapshot: SemaphoreSnapshot,
    *,
    project_branch: str,
) -> None:
    """Проверить снимок Semaphore без дополнительных API-вызовов."""

    if snapshot.github_key.get("type") != "ssh":
        raise InfraManagerError(
            "Semaphore key 'GitHub project read-only' имеет неверный тип"
        )
    github_key_id = snapshot.github_key.get("id")
    if not isinstance(github_key_id, int):
        raise InfraManagerError(
            "Semaphore key 'GitHub project read-only' "
            "имеет некорректный id"
        )

    opentofu_environment = require_unique_by_name(
        list(snapshot.environments),
        SETTINGS.opentofu_env_name,
        "Variable Group",
    )
    infra_manager_environment = require_unique_by_name(
        list(snapshot.environments),
        SETTINGS.infra_manager_env_name,
        "Variable Group",
    )
    environment_ids = {
        item.get("id")
        for item in (opentofu_environment, infra_manager_environment)
    }
    if (
        len(environment_ids) != 2
        or any(not isinstance(item, int) for item in environment_ids)
    ):
        raise InfraManagerError(
            "Variable Group Semaphore имеют некорректные id"
        )

    _check_git_branch_contract(
        snapshot.repository,
        snapshot.branches,
        snapshot.templates,
        github_key_id=github_key_id,
        project_branch=project_branch,
    )

    view_ids: dict[str, int] = {}
    for spec in semaphore_views():
        view = find_unique_by_title(
            list(snapshot.views),
            spec.title,
            "View Semaphore",
        )
        if view is None:
            raise InfraManagerError(
                f"В Semaphore отсутствует View '{spec.title}'"
            )
        view_id = view.get("id")
        if not isinstance(view_id, int):
            raise InfraManagerError(
                f"View Semaphore '{spec.title}' имеет некорректный id"
            )
        if view.get("position") != spec.position:
            raise InfraManagerError(
                f"View Semaphore '{spec.title}' имеет неверную позицию"
            )
        view_ids[spec.title] = view_id

    for spec in semaphore_templates():
        template = require_unique_by_name(
            list(snapshot.templates),
            spec.name,
            "шаблон Semaphore",
        )
        if (
            template.get("app") != spec.app
            or template.get("playbook") != spec.playbook
            or str(template.get("arguments") or "[]") != spec.arguments
            or template.get("view_id") != view_ids[spec.view]
            or not _survey_vars_match(
                template.get("survey_vars") or [],
                spec.survey_vars,
            )
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{spec.name}' "
                "не соответствует Python-контракту"
            )
        template_environment_ids = template.get("environment_ids")
        if (
            not isinstance(template_environment_ids, list)
            or set(template_environment_ids) != environment_ids
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{spec.name}' "
                "не подключён к обеим Variable Group"
            )


def _check_semaphore(*, project_branch: str) -> None:
    """Подключиться к Semaphore и проверить единый снимок проекта."""

    semaphore = SemaphoreClient()
    semaphore.ping()
    semaphore.auth_mode = "token"

    raw_project_id = PROJECT_ID_FILE.read_text(encoding="utf-8").strip()
    if not raw_project_id.isdigit():
        raise InfraManagerError("Некорректный project-id Semaphore")

    snapshot = load_semaphore_snapshot(semaphore, int(raw_project_id))
    validate_semaphore_snapshot(
        snapshot,
        project_branch=project_branch,
    )


def _check_runtime_tools() -> None:
    """Проверить ключ Ansible и обязательные инструменты infra-runtime."""
    if command_runner.run(
        [
            "docker",
            "exec",
            "infra-runtime",
            "test",
            "-r",
            str(ANSIBLE_PRIVATE_KEY),
        ],
        quiet=True,
        check=False,
    ).returncode:
        raise InfraManagerError(
            "Закрытый ключ Ansible недоступен внутри infra-runtime"
        )

    checks = (
        (
            ["docker", "exec", "infra-runtime", "tofu", "version"],
            "OpenTofu недоступен",
        ),
        (
            ["docker", "exec", "infra-runtime", "packer", "version"],
            "Packer недоступен",
        ),
        (
            [
                "docker",
                "exec",
                "infra-runtime",
                "ansible",
                "--version",
            ],
            "Ansible недоступен",
        ),
        (
            [
                "docker",
                "exec",
                "infra-runtime",
                "python3",
                "-c",
                "import proxmoxer",
            ],
            "proxmoxer недоступен",
        ),
    )
    for argv, message in checks:
        if command_runner.run(
            argv,
            quiet=True,
            check=False,
        ).returncode:
            raise InfraManagerError(message)


def _check_runtime_bundle() -> None:
    """Проверить Docker, обязательные файлы и infra-runtime."""

    _check_local_prerequisites()
    _check_runtime()


def _check_pve(*, full: bool) -> None:
    """Проверить базовую авторизацию PVE и при необходимости полный контракт."""
    pve = PveClient(PVE_ENV, CA_BUNDLE)
    version = pve.data("/version")
    if not isinstance(version, dict) or not (
        version.get("version") or version.get("release")
    ):
        raise InfraManagerError(
            "PVE API credential не проходит базовую авторизацию"
        )

    if full:
        check_access(quiet=True)


def _run_status_checks(
    definition: StatusDefinition,
    *,
    project_branch: str,
    full: bool,
) -> None:
    """Выполнить разрешённые проверки в порядке из status.yaml."""

    for check in definition.checks:
        check_type = check["type"]
        if check_type == "runtime":
            _check_runtime_bundle()
        elif check_type == "portal":
            _check_portal()
        elif check_type == "openbao":
            _check_openbao(full=full)
        elif check_type == "semaphore":
            _check_semaphore(project_branch=project_branch)
        elif check_type == "runtime_tools":
            _check_runtime_tools()
        elif check_type == "pve_access":
            _check_pve(full=full)
        else:
            raise InfraManagerError(
                f"Неизвестный тип проверки состояния: {check_type}"
            )


def _primary_ipv4() -> str:
    """Вернуть основной IPv4 текущего infra-manager."""
    result = command_runner.run(
        ["hostname", "-I"],
        capture=True,
        check=False,
    )
    if result.returncode:
        raise InfraManagerError("Не удалось определить адрес infra-manager")

    addresses = [
        item
        for item in result.stdout.split()
        if "." in item and item != "127.0.0.1"
    ]
    if not addresses:
        raise InfraManagerError("Не удалось определить адрес infra-manager")
    return addresses[0]


def _git_revision(repository: Path) -> str:
    """Вернуть короткий хэш указанной рабочей копии Git."""
    if not repository.is_dir():
        raise InfraManagerError(
            f"Отсутствует рабочая копия проекта: {repository}"
        )
    result = command_runner.run(
        [
            "git",
            "-C",
            str(repository),
            "rev-parse",
            "--short",
            "HEAD",
        ],
        capture=True,
        check=False,
    )
    revision = result.stdout.strip() if not result.returncode else ""
    if not revision:
        raise InfraManagerError(
            f"Не удалось определить версию проекта: {repository}"
        )
    return revision


def _read_required_text(path: Path) -> str:
    """Прочитать обязательный непустой текстовый файл."""
    required_file(path)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise InfraManagerError(
            f"Не удалось прочитать обязательный файл: {path}"
        ) from exc
    if not value:
        raise InfraManagerError(f"Обязательный файл пуст: {path}")
    return value


def _resolve_data_source(
    source: dict[str, Any],
    *,
    project_branch: str,
) -> str:
    """Получить одно фактическое значение разрешённого типа."""
    source_type = source["type"]

    if source_type == "primary_ipv4":
        return _primary_ipv4()
    if source_type == "project_branch":
        return project_branch
    if source_type == "git_revision":
        return _git_revision(Path(str(source["repository"])))
    if source_type == "file":
        return _read_required_text(Path(str(source["path"])))

    raise InfraManagerError(
        f"Неизвестный источник данных состояния: {source_type}"
    )


def _resolve_status_data(
    definition: StatusDefinition,
    *,
    project_branch: str,
) -> dict[str, str]:
    """Собрать и проверить все данные, объявленные в status.yaml."""
    values = {
        name: _resolve_data_source(
            source,
            project_branch=project_branch,
        )
        for name, source in definition.data.items()
    }
    values["guest_vmid"] = str(definition.guest_vmid)

    for section in definition.sections:
        for field in section["fields"]:
            _field_value(field, values)

    return values


def _render_template(template: str, values: dict[str, str]) -> str:
    """Подставить только уже собранные значения состояния."""
    try:
        return template.format_map(values)
    except KeyError as exc:
        raise InfraManagerError(
            f"status.yaml: неизвестное значение в шаблоне: {exc.args[0]}"
        ) from exc


def _field_value(field: dict[str, str], values: dict[str, str]) -> str:
    if "source" in field:
        return values[field["source"]]
    if "value" in field:
        return field["value"]
    return _render_template(field["template"], values)


def _show_full_status(
    definition: StatusDefinition,
    values: dict[str, str],
) -> None:
    """Показать полный экран строго по status.yaml."""
    separator = definition.separator * definition.separator_width

    print()
    print(separator)
    print(f"  {definition.title}")
    print(separator)
    print()

    for check in definition.checks:
        console.ok(check["message"])

    for section in definition.sections:
        print(f"\n{section['title']}")
        fields = section["fields"]
        label_width = max(len(field["label"]) for field in fields) + 3
        for field in fields:
            label = f"{field['label']}:"
            value = _field_value(field, values)
            print(f"  {label:<{label_width}}{value}")

    print()
    print(separator)
    print(f"  {definition.ready_message}")
    print(separator)


def check_status(*, full: bool = False, quiet: bool = False) -> int:
    """Проверить готовность infra-manager по установленному status.yaml."""
    definition = load_status_definition()
    project_branch = _project_branch()

    if not quiet:
        console.info("Проверка управляющего контура")

    _run_status_checks(
        definition,
        project_branch=project_branch,
        full=full,
    )

    values = (
        _resolve_status_data(
            definition,
            project_branch=project_branch,
        )
        if full
        else {}
    )

    if quiet:
        return 0

    if full:
        _show_full_status(definition, values)
    else:
        console.ok(definition.short_ready_message)

    return 0
