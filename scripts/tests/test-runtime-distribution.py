#!/usr/bin/env python3
"""Проверка необязательной поставки готового образа infra-runtime."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure/guests/910-infra-manager"
PROVISION = yaml.safe_load((GUEST / "provision.yaml").read_text(encoding="utf-8"))
COMPOSE = yaml.safe_load(
    (GUEST / "rootfs/opt/infra-manager/compose/docker-compose.yml")
    .read_text(encoding="utf-8")
)
RUNTIME_TASKS = yaml.safe_load(
    (ROOT / "automation/ansible/roles/infra_manager/tasks/runtime.yml")
    .read_text(encoding="utf-8")
)
WORKFLOW = yaml.safe_load(
    (ROOT / ".github/workflows/repository-checks.yml").read_text(encoding="utf-8")
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def task(name: str) -> dict:
    matched = [item for item in RUNTIME_TASKS if item.get("name") == name]
    require(len(matched) == 1, f"Неожиданное количество задач: {name}")
    return matched[0]


runtime = PROVISION["docker"]["services"]["runtime"]
registry = runtime["registry_image"]
require(
    registry == "ghcr.io/zsergeyru/proxmox-infra-runtime",
    "Реестр должен быть единственным источником адреса образа",
)
require(
    COMPOSE["services"]["runtime"]["image"] == "infra-runtime:${RUNTIME_VERSION}",
    "Рабочий Compose должен сохранять локальный тег",
)
require(
    "build" in COMPOSE["services"]["runtime"],
    "Должна сохраняться возможность локальной сборки",
)

pull = task("Скачать проверенный образ управляющей среды")
pull_args = pull["ansible.builtin.command"]["argv"]
require(pull_args[:4] == ["timeout", "120", "docker", "pull"], "Скачивание должно иметь предел ожидания")
require(
    pull_args[4] == "{{ provision.docker.services.runtime.registry_image }}:sha-{{ infra_runtime_revision.stdout }}",
    "Скачивание должно выбирать образ по точному коммиту",
)
require(pull["failed_when"] is False, "Сбой скачивания должен разрешать локальную сборку")
require(pull["register"] == "infra_runtime_pull", "Результат скачивания не сохранён")

revision = task("Определить версию проекта для готового образа")
require(
    revision["ansible.builtin.command"]["argv"][-2:] == ["rev-parse", "HEAD"],
    "Версию нужно брать из реально полученной Git-копии",
)
require(
    "org.opencontainers.image.revision" in str(
        task("Подтвердить соответствие образа версии проекта")
    ),
    "Перед применением готового образа нужно проверить его источник",
)
require(
    "infra_runtime_pull.rc == 0"
    in task("Назначить рабочую метку скачанному образу")["when"],
    "Готовый образ нельзя использовать после неудачного скачивания",
)
build = task("Собрать управляющую среду")
require(
    build["when"] == "infra_runtime_pull.rc != 0",
    "Сборка должна выполняться только при недоступности готового образа",
)
require(
    build["ansible.builtin.command"]["argv"][-3:] == ["build", "--pull", "runtime"],
    "Локальная сборка должна оставаться прежней",
)

validate = WORKFLOW["jobs"]["validate"]
publish = WORKFLOW["jobs"]["publish-infra-runtime"]
require(publish["needs"] == "validate", "Публиковать можно только после проверок")
require(
    "github.event_name == 'push'" in publish["if"],
    "Публикация из pull_request запрещена",
)
require(
    publish["permissions"]["packages"] == "write"
    and publish["permissions"]["contents"] == "read",
    "Право записи пакетов должно быть только у задания публикации",
)
require(
    WORKFLOW["permissions"]["contents"] == "read"
    and "packages" not in WORKFLOW["permissions"],
    "Проверки не должны получать доступ на запись в реестр",
)
steps = publish["steps"]
build_script = next(
    step["run"] for step in steps if step["name"] == "Build image from verified source"
)
require(
    "ghcr.io/zsergeyru/proxmox-infra-runtime:sha-${{ github.sha }}" in build_script,
    "Публикация должна использовать адрес из provision.yaml и точный коммит",
)
require(
    "org.opencontainers.image.revision=${{ github.sha }}" in build_script,
    "Опубликованный образ должен содержать проверяемый коммит",
)
for version, arg in [
    (runtime["base_image"].split(":")[-1], "SEMAPHORE_VERSION"),
    (runtime["tools"]["opentofu"], "OPENTOFU_VERSION"),
    (runtime["tools"]["packer"], "PACKER_VERSION"),
]:
    require(
        f"--build-arg {arg}={version}" in build_script,
        f"Публикуемый образ использует неверную версию {arg}",
    )
require(
    any(step["name"] == "Build infra-runtime image" for step in validate["steps"]),
    "Публикация не должна заменять проверку сборки и запуска образа",
)
print("[ОК] Поставка infra-runtime: GitHub после проверок и локальная сборка при сбое")
