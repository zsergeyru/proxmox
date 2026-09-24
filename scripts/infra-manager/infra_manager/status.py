"""Проверка готовности LXC 910 infra-manager."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console
from .pve import PveClient, check_access
from .semaphore import SemaphoreClient
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


def run_quiet(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def run_capture(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
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
    return os.environ.get("INFRA_PROJECT_BRANCH", SETTINGS.default_project_branch)


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
                "OpenTofu Plan",
                "Build Template 9000",
                "Deploy Guest 410",
            }
            and template.get("git_branch") != project_branch
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{template.get('name')}' "
                f"настроен не на ветку '{project_branch}'"
            )


def check_status(*, full: bool = False) -> int:
    project_branch = _project_branch()

    if shutil.which("docker") is None:
        raise InfraManagerError("Docker не установлен")
    if run_quiet(["docker", "compose", "version"]).returncode:
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

    running = run_capture(
        [
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            "infra-runtime",
        ]
    )
    if running.returncode or running.stdout.strip() != "true":
        raise InfraManagerError("Semaphore Server не запущен")

    semaphore = SemaphoreClient()
    semaphore.ping()
    semaphore.auth_mode = "token"

    raw_project_id = PROJECT_ID_FILE.read_text(
        encoding="utf-8"
    ).strip()
    if not raw_project_id.isdigit():
        raise InfraManagerError("Некорректный project-id Semaphore")
    project_id = int(raw_project_id)

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

    repositories = semaphore.get(
        f"/project/{project_id}/repositories?sort=name&order=asc"
    )
    repository = unique_named(
        repositories,
        "proxmox",
        "Git repository",
    )
    stored_key_id = repository.get("ssh_key_id")
    if (
        repository.get("git_url") != PROJECT_REPO
        or str(stored_key_id or "") != str(github_key_id)
        or repository.get("git_branch") != project_branch
    ):
        raise InfraManagerError(
            "Git repository 'proxmox' не соответствует "
            "ожидаемому URL, SSH key или ветке "
            f"'{project_branch}'"
        )
    repository_id = repository.get("id")
    if not isinstance(repository_id, int):
        raise InfraManagerError(
            "Git repository 'proxmox' имеет некорректный id"
        )

    # Semaphore v2.18.30 создаёт socket временного ssh-agent в каталоге
    # проекта раньше, чем branches API успевает создать этот каталог.
    prepare_tmp = run_quiet(
        [
            "docker",
            "exec",
            "--user",
            "1001:0",
            "infra-runtime",
            "mkdir",
            "-p",
            f"/tmp/semaphore/project_{project_id}",
        ]
    )
    if prepare_tmp.returncode:
        raise InfraManagerError(
            "Не удалось подготовить временный каталог Semaphore "
            "для проверки Git"
        )

    try:
        branches = semaphore.get(
            f"/project/{project_id}/repositories/"
            f"{repository_id}/branches"
        )
    except InfraManagerError as exc:
        raise InfraManagerError(
            "Semaphore не может прочитать Git repository 'proxmox' "
            "сохранённым SSH key"
        ) from exc
    if not isinstance(branches, list) or project_branch not in branches:
        raise InfraManagerError(
            "Semaphore не видит ветку "
            f"'{project_branch}' в Git repository 'proxmox'"
        )

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
    expected_templates = {
        "OpenTofu Plan": (
            "scripts/infra-manager/jobs/opentofu-plan.py",
            "[]",
        ),
        "Build Template 9000": (
            "scripts/infra-manager/jobs/build-template.py",
            '["9000"]',
        ),
        "Deploy Guest 410": (
            "scripts/infra-manager/jobs/deploy-guest.py",
            '["410"]',
        ),
    }
    for template_name, (playbook, arguments) in expected_templates.items():
        template = unique_named(
            templates,
            template_name,
            "шаблон Semaphore",
        )
        if (
            template.get("app") != "python"
            or template.get("playbook") != playbook
            or str(template.get("arguments") or "[]") != arguments
        ):
            raise InfraManagerError(
                f"Шаблон Semaphore '{template_name}' "
                "не соответствует Python-контракту"
            )

    if run_quiet(
        [
            "docker",
            "exec",
            "infra-runtime",
            "test",
            "-r",
            str(ANSIBLE_PRIVATE_KEY),
        ]
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
        if run_quiet(argv).returncode:
            raise InfraManagerError(message)

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
        console.ok(
            "infra-manager готов, полный контракт PVE API подтверждён"
        )
    else:
        console.ok("infra-manager готов")

    return 0
