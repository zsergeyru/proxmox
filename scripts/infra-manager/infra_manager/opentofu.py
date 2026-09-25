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
from .settings import PATHS, SETTINGS

CA_BUNDLE = PATHS.ca_bundle
STATE_DIR = PATHS.opentofu_dir
STATE_FILE = PATHS.opentofu_state_dir / "proxmox.tfstate"
GUEST_STATE_FILE = PATHS.opentofu_input


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


def _resolve_lxc_templates(payload: dict[str, Any]) -> None:
    """Заменить логические имена LXC-шаблонов на фактические volume ID."""

    guests = payload.get("guests")
    if not isinstance(guests, dict):
        raise InfraManagerError("OpenTofu input не содержит guests")

    unresolved = [
        guest
        for guest in guests.values()
        if isinstance(guest, dict)
        and guest.get("type") == "lxc"
        and isinstance(guest.get("ostemplate"), str)
        and not str(guest["ostemplate"]).endswith((".tar.zst", ".tar.gz"))
    ]
    if not unresolved:
        return

    client = PveClient.from_opentofu_env()
    cache: dict[tuple[str, str], str] = {}

    for guest in unresolved:
        logical = str(guest["ostemplate"])
        storage_prefix, separator, template_prefix = logical.partition(":vztmpl/")
        if not separator or not storage_prefix or not template_prefix:
            raise InfraManagerError(
                f"Некорректное логическое имя LXC-шаблона: {logical}"
            )

        node = str(guest.get("node") or "")
        if not node:
            raise InfraManagerError(
                f"Для LXC {guest.get('vmid')} не задан PVE-узел"
            )

        cache_key = (node, logical)
        resolved = cache.get(cache_key)
        if resolved is None:
            data = client.data(
                f"/nodes/{node}/storage/{storage_prefix}/content",
                query={"content": "vztmpl"},
            )
            if not isinstance(data, list):
                raise InfraManagerError(
                    f"PVE вернул неожиданный список LXC-шаблонов для {storage_prefix}"
                )

            prefix = f"{storage_prefix}:vztmpl/{template_prefix}_"
            candidates = sorted(
                str(item.get("volid"))
                for item in data
                if isinstance(item, dict)
                and isinstance(item.get("volid"), str)
                and str(item["volid"]).startswith(prefix)
                and str(item["volid"]).endswith(
                    ("_amd64.tar.zst", "_amd64.tar.gz")
                )
            )
            if not candidates:
                raise InfraManagerError(
                    f"Не найден LXC-шаблон для {logical}"
                )
            resolved = candidates[-1]
            cache[cache_key] = resolved

        guest["ostemplate"] = resolved


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


def _prepare_input(
    repo_root: Path,
    *,
    only_vmids: set[int] | None = None,
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
    for vmid in sorted(only_vmids or set()):
        command.extend(["--only-vmid", str(vmid)])
    for vmid in sorted(exclude_vmids or set()):
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
    only_vmids: set[int] | None = None,
    exclude_vmids: set[int] | None = None,
) -> OpenTofuWorkspace:
    """Собрать input и вернуть публичный интерфейс рабочей области."""

    if only_vmids is None and exclude_vmids is None:
        exclude_vmids = set(SETTINGS.bootstrap_managed_vmids)

    opentofu_dir, payload = _prepare_input(
        repo_root,
        only_vmids=only_vmids,
        exclude_vmids=exclude_vmids,
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

    target_type = target.split(".", 1)[0]
    if target_type not in {
        "proxmox_virtual_environment_vm",
        "proxmox_virtual_environment_container",
    }:
        raise InfraManagerError(
            f"Неподдерживаемый адрес ресурса OpenTofu: {target}"
        )

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if (
            resource.get("type") != target_type
            or resource.get("name") != "guest"
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
