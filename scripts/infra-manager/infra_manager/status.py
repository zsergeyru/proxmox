"""Проверка готовности LXC 910 infra-manager."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .common import InfraManagerError, command_runner, console
from .pve import PveClient, check_access
from .semaphore import SEMAPHORE_TEMPLATES, SemaphoreClient
from .settings import PATHS, SETTINGS

PVE_ENV = PATHS.pve_api_env
CA_BUNDLE = PATHS.ca_bundle
ANSIBLE_PRIVATE_KEY = PATHS.ansible_private_key
ANSIBLE_PUBLIC_KEY = PATHS.ansible_public_key
SERVER_ENV = PATHS.server_env
GITHUB_KEY_COPY = PATHS.github_key_copy
PROJECT_ID_FILE = PATHS.semaphore_project_id_file
OPENTOFU_INPUT = PATHS.opentofu_input
OPENTOFU_STATE_DIR = PATHS.opentofu_state_dir
SEMAPHORE_API_TOKEN_FILE = PATHS.semaphore_api_token_file
PROJECT_REPO = SETTINGS.project_repo


def required_file(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(
            f"Отсутствует обязательный файл: {path}"
        )


def unique_named(
    items: Any,
    name: str,
    kind: str,
) -> dict[str, Any]:
    if not isinstance(items, list):
        raise InfraManagerError(
            f"Semaphore вернул некорректный список: {kind}"
        )
    matches = [
        item
        for item in items
        if isinstance(item, dict) and item.get("name") == name
    ]
    if len(matches) != 1:
        raise InfraManagerError(
            f"В Semaphore должен существовать ровно один "
            f"{kind} '{name}'"
        )
    return matches[0]


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

    if not isinstance(branches, list) or project_branch not in branches:
        raise InfraManagerError(
            "Semaphore не видит ветку "
            f"'{project_branch}' в Git repository 'proxmox'"
        )

    if not isinstance(templates, list):
        raise InfraManagerError(
            "Semaphore вернул некорректный список шаблонов"
        )
    for template in templates:
        if (
            isinstance(template, dict)
            and template.get("name") in {
                spec.name for spec in SEMAPHORE_TEMPLATES
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


def _connect_semaphore() -> tuple[SemaphoreClient, int]:
    """Подключиться к Semaphore и вернуть client и project id."""
    semaphore = SemaphoreClient()
    semaphore.ping()
    semaphore.auth_mode = "token"

    raw_project_id = PROJECT_ID_FILE.read_text(
        encoding="utf-8"
    ).strip()
    if not raw_project_id.isdigit():
        raise InfraManagerError("Некорректный project-id Semaphore")
    return semaphore, int(raw_project_id)


def _check_github_key(
    semaphore: SemaphoreClient,
    project_id: int,
) -> int:
    """Проверить GitHub SSH key Semaphore и вернуть его id."""
    keys = semaphore.get(
        f"/project/{project_id}/keys?sort=name&order=asc"
    )
    github_key = unique_named(
        keys,
        "GitHub project read-only",
        "SSH key",
    )
    if github_key.get("type") != "ssh":
        raise InfraManagerError(
            "Semaphore key 'GitHub project read-only' имеет неверный тип"
        )
    github_key_id = github_key.get("id")
    if not isinstance(github_key_id, int):
        raise InfraManagerError(
            "Semaphore key 'GitHub project read-only' "
            "имеет некорректный id"
        )
    return github_key_id


def _get_repository(
    semaphore: SemaphoreClient,
    project_id: int,
) -> tuple[dict[str, Any], int]:
    """Получить единственный Git repository proxmox и его id."""
    repositories = semaphore.get(
        f"/project/{project_id}/repositories?sort=name&order=asc"
    )
    repository = unique_named(
        repositories,
        "proxmox",
        "Git repository",
    )
    repository_id = repository.get("id")
    if not isinstance(repository_id, int):
        raise InfraManagerError(
            "Git repository 'proxmox' имеет некорректный id"
        )
    return repository, repository_id


def _get_repository_branches(
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


def _check_opentofu_environment(
    semaphore: SemaphoreClient,
    project_id: int,
) -> None:
    """Проверить Variable Group OpenTofu PVE."""
    environments = semaphore.get(
        f"/project/{project_id}/environment?sort=name&order=asc"
    )
    if not any(
        isinstance(item, dict)
        and item.get("name") == "OpenTofu PVE"
        for item in environments
    ):
        raise InfraManagerError(
            "В Semaphore отсутствует Variable Group OpenTofu PVE"
        )


def _check_templates(
    semaphore: SemaphoreClient,
    project_id: int,
    repository: dict[str, Any],
    branches: Any,
    *,
    github_key_id: int,
    project_branch: str,
) -> None:
    """Проверить ветку и контракт заданий Semaphore."""
    templates = semaphore.get(
        f"/project/{project_id}/templates?sort=name&order=asc"
    )
    _check_git_branch_contract(
        repository,
        branches,
        templates,
        github_key_id=github_key_id,
        project_branch=project_branch,
    )
    for spec in SEMAPHORE_TEMPLATES:
        template = unique_named(
            templates,
            spec.name,
            "шаблон Semaphore",
        )
        if (
            template.get("app") != spec.app
            or template.get("playbook") != spec.playbook
            or str(template.get("arguments") or "[]") != spec.arguments
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{spec.name}' "
                "не соответствует Python-контракту"
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


def check_status(*, full: bool = False) -> int:
    """Проверить готовность infra-manager по последовательным этапам."""
    project_branch = _project_branch()

    _check_local_prerequisites()
    _check_runtime()

    semaphore, project_id = _connect_semaphore()
    github_key_id = _check_github_key(semaphore, project_id)
    repository, repository_id = _get_repository(semaphore, project_id)
    branches = _get_repository_branches(
        semaphore,
        project_id,
        repository_id,
    )
    _check_opentofu_environment(semaphore, project_id)
    _check_templates(
        semaphore,
        project_id,
        repository,
        branches,
        github_key_id=github_key_id,
        project_branch=project_branch,
    )
    _check_runtime_tools()
    _check_pve(full=full)

    if full:
        console.ok(
            "infra-manager готов, полный контракт PVE API подтверждён"
        )
    else:
        console.ok("infra-manager готов")

    return 0
