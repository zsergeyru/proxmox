"""Задания OpenTofu для infra-manager."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .pve import PveClient
from .settings import PATHS

CA_BUNDLE = PATHS.ca_bundle
STATE_DIR = PATHS.opentofu_dir
STATE_FILE = PATHS.opentofu_state_dir / "proxmox.tfstate"
GUEST_STATE_FILE = PATHS.opentofu_input
CONTROL_VMIDS = {910}


@dataclass(frozen=True)
class OpenTofuWorkspace:
    """Подготовленная рабочая область OpenTofu и требуемое состояние."""

    directory: Path
    state_dir: Path
    env: dict[str, str]
    payload: dict[str, Any]

    def initialize(self) -> None:
        """Инициализировать провайдеры без изменения инфраструктуры."""

        _init(self.directory, self.env)

    def get_resource_state(self, target: str) -> tuple[bool, str]:
        """Вернуть наличие ресурса в state и его статус."""

        return _state_status(self.directory, target, self.env)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise InfraManagerError(f"Не задан {name}")
    return value


def _opentofu_env() -> dict[str, str]:
    if not CA_BUNDLE.is_file() or CA_BUNDLE.stat().st_size == 0:
        raise InfraManagerError(f"Не найден CA bundle: {CA_BUNDLE}")
    env = os.environ.copy()
    env["SSL_CERT_FILE"] = str(CA_BUNDLE)
    return env


def _paths(repo_root: Path) -> tuple[Path, Path]:
    opentofu_dir = repo_root / "automation" / "opentofu"
    renderer = repo_root / "scripts" / "guests" / "render-opentofu-input.py"
    if not opentofu_dir.is_dir():
        raise InfraManagerError(
            f"Не найден каталог OpenTofu: {opentofu_dir}"
        )
    if not renderer.is_file():
        raise InfraManagerError(
            f"Не найден генератор состояния OpenTofu: {renderer}"
        )
    return opentofu_dir, renderer


def _resolve_lxc_templates(payload: dict[str, Any]) -> None:
    """Дополнить LXC точным локальным template_file_id из PVE."""
    guests = payload.get("guests")
    if not isinstance(guests, dict):
        raise InfraManagerError("OpenTofu input не содержит guests")

    lxc_guests = [
        guest
        for guest in guests.values()
        if isinstance(guest, dict) and guest.get("type") == "lxc"
    ]
    if not lxc_guests:
        return

    client = PveClient.from_opentofu_env()
    for guest in lxc_guests:
        selector = guest.get("ostemplate")
        node = guest.get("node")
        if not isinstance(selector, str) or not isinstance(node, str):
            raise InfraManagerError("LXC имеет неполное описание template/node")
        guest["ostemplate_file_id"] = client.latest_lxc_template(
            node=node,
            selector=selector,
        )


def _prepare_input(
    repo_root: Path,
    *,
    include_vmids: set[int] | None = None,
    exclude_vmids: set[int] | None = None,
) -> tuple[Path, dict[str, Any]]:
    require_command("tofu")
    _require_env("TF_VAR_pve_endpoint")
    _require_env("TF_VAR_pve_api_token")
    _require_env("TF_VAR_ansible_ssh_public_key")
    opentofu_dir, renderer = _paths(repo_root)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        str(renderer),
        "--output",
        str(GUEST_STATE_FILE),
    ]
    for vmid in sorted(include_vmids or ()):
        command.extend(["--include-vmid", str(vmid)])
    for vmid in sorted(exclude_vmids or ()):
        command.extend(["--exclude-vmid", str(vmid)])
    run(command)

    try:
        payload = json.loads(GUEST_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfraManagerError(
            f"Не удалось прочитать {GUEST_STATE_FILE}: {exc}"
        ) from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("guests"),
        dict,
    ):
        raise InfraManagerError(
            f"{GUEST_STATE_FILE} имеет некорректный формат"
        )

    _resolve_lxc_templates(payload)
    GUEST_STATE_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return opentofu_dir, payload


def prepare_workspace(
    repo_root: Path,
    *,
    include_vmids: set[int] | None = None,
    exclude_vmids: set[int] | None = None,
) -> OpenTofuWorkspace:
    """Собрать input и вернуть публичный интерфейс рабочей области."""
    actual_exclude = exclude_vmids
    if include_vmids is None and exclude_vmids is None:
        actual_exclude = CONTROL_VMIDS

    opentofu_dir, payload = _prepare_input(
        repo_root,
        include_vmids=include_vmids,
        exclude_vmids=actual_exclude,
    )
    return OpenTofuWorkspace(
        directory=opentofu_dir,
        state_dir=STATE_DIR,
        env=_opentofu_env(),
        payload=payload,
    )


def _init(opentofu_dir: Path, env: dict[str, str]) -> None:
    run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "init",
            "-input=false",
            "-no-color",
            "-lockfile=readonly",
        ],
        env=env,
    )


def run_plan(repo_root: Path) -> int:
    """Построить общий план OpenTofu без применения изменений."""

    workspace = prepare_workspace(repo_root)

    console.info("Инициализация OpenTofu")
    workspace.initialize()

    console.info("Построение общего плана OpenTofu")
    result = run(
        [
            "tofu",
            f"-chdir={workspace.directory}",
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=30s",
            "-detailed-exitcode",
        ],
        check=False,
        env=workspace.env,
    )
    if result.returncode == 0:
        console.ok("OpenTofu: изменений нет")
        return 0
    if result.returncode == 2:
        console.ok("OpenTofu: план содержит изменения")
        return 0
    raise InfraManagerError(
        f"OpenTofu plan завершился с кодом {result.returncode}"
    )


def _state_status(
    opentofu_dir: Path,
    target: str,
    env: dict[str, str],
) -> tuple[bool, str]:
    if not STATE_FILE.is_file():
        return False, ""

    target_prefix = target.split("[", 1)[0]
    parts = target_prefix.split(".", 1)
    if len(parts) != 2:
        raise InfraManagerError(f"Некорректный адрес OpenTofu: {target}")
    expected_type, expected_name = parts

    pulled = run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "state",
            "pull",
        ],
        capture_output=True,
        env=env,
    )
    try:
        state = json.loads(pulled.stdout)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            "OpenTofu state содержит некорректный JSON"
        ) from exc
    if not isinstance(state, dict):
        raise InfraManagerError(
            "OpenTofu state должен быть JSON-объектом"
        )

    resources = state.get("resources", [])
    if not isinstance(resources, list):
        raise InfraManagerError(
            "OpenTofu state содержит некорректный resources"
        )

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if (
            resource.get("type") != expected_type
            or resource.get("name") != expected_name
        ):
            continue
        instances = resource.get("instances", [])
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            address_key = str(instance.get("index_key", ""))
            if target.endswith(f'["{address_key}"]'):
                return True, str(instance.get("status") or "ready")
    return False, ""
