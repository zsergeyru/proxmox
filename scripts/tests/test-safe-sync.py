#!/usr/bin/env python3
"""Проверить, что Sync гостя не превращается в обновление программ или PVE."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "automation/ansible"
PLAYBOOK = ANSIBLE / "playbooks/configure-guest.yml"
SYNC_ROLES = ("web_gateway", "ai_services", "portal")
UNSAFE_MODULES = {
    "ansible.builtin.apt",
    "ansible.builtin.package",
    "ansible.builtin.get_url",
    "ansible.builtin.systemd_service",
}


def tasks(path: Path) -> list[dict]:
    """Прочитать список задач, запрещая пустые и некорректные файлы."""
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, list) and loaded, path
    assert all(isinstance(task, dict) for task in loaded), path
    return loaded


def verify_sync_tasks(role: str, filename: str = "sync.yml") -> None:
    """Запретить установку пакетов в Sync и во вложенных задачах роли."""
    if "/" in filename or not filename.endswith(".yml"):
        raise AssertionError((role, filename))
    path = ANSIBLE / "roles" / role / "tasks" / filename
    for task in tasks(path):
        included = task.get("ansible.builtin.include_tasks")
        if isinstance(included, str):
            verify_sync_tasks(role, included)
        assert not (UNSAFE_MODULES & task.keys()), (role, task.get("name"))
        command = task.get("ansible.builtin.command")
        if not isinstance(command, dict):
            continue
        argv = command.get("argv", [])
        assert isinstance(argv, list), task
        command_line = " ".join(str(arg) for arg in argv)
        for forbidden in (
            "docker compose", "docker pull", "docker run", "docker build",
            "apt ", "pip install", "infra-manager deploy",
        ):
            assert forbidden not in command_line, (role, task.get("name"))
        if "docker " in command_line:
            assert any(
                command_line.startswith(f"docker {operation}")
                for operation in ("exec ", "inspect ", "restart ")
            ), (role, task.get("name"))


def verify_playbook() -> None:
    """Проверить отдельную фазу Ansible без запуска полных ролей."""
    plays = tasks(PLAYBOOK)
    assert len(plays) == 1
    actions = plays[0]["tasks"]
    safe_roles = {}
    for action in actions:
        definition = action.get("ansible.builtin.include_role")
        if not isinstance(definition, dict):
            continue
        role = definition.get("name")
        if definition.get("tasks_from") == "sync":
            safe_roles[role] = str(action.get("when"))
        elif role in {"linux_base", "guest_layout"}:
            assert "sync" in str(action.get("when")), role
        elif role in {"docker", "infra_manager", "ai_control",
                      "photo_archive", "web_gateway", "network_gateway",
                      "portal", "ai_services"}:
            assert "full" in str(action.get("when")), role
    assert set(safe_roles) == set(SYNC_ROLES), safe_roles
    assert all("sync" in condition for condition in safe_roles.values())


def main() -> None:
    """Выполнить проверки контракта отдельных синхронизирующих задач."""
    for role in SYNC_ROLES:
        verify_sync_tasks(role)
    verify_playbook()
    print("[ОК] Sync не устанавливает ПО и не запускает Docker Compose")


if __name__ == "__main__":
    main()
