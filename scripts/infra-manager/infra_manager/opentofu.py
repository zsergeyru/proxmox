"""Задания OpenTofu для infra-manager."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .guest_catalog import find_guest_by_role
from .pve import PveClient
from .settings import PATHS, SETTINGS

CA_BUNDLE = PATHS.ca_bundle
STATE_DIR = PATHS.opentofu_dir
STATE_FILE = PATHS.opentofu_state_dir / "proxmox.tfstate"
GUEST_STATE_FILE = PATHS.opentofu_input
PROVIDER_MIRROR_DIR = STATE_DIR / "provider-mirror"
PROXMOX_PROVIDER_SOURCE = "registry.opentofu.org/bpg/proxmox"
PROXMOX_PROVIDER_RELEASE = (
    "https://github.com/bpg/terraform-provider-proxmox/releases/download"
)


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
        infra_manager = find_guest_by_role(
            repo_root,
            SETTINGS.infra_manager_role,
        )
        exclude_vmids = {infra_manager.vmid}

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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _proxmox_provider_lock(
    opentofu_dir: Path,
) -> tuple[str, frozenset[str]]:
    lock_file = opentofu_dir / ".terraform.lock.hcl"
    if not lock_file.is_file():
        raise InfraManagerError(
            f"Не найден lock-файл OpenTofu: {lock_file}"
        )
    text = lock_file.read_text(encoding="utf-8")
    block_match = re.search(
        r'provider\s+"'
        + re.escape(PROXMOX_PROVIDER_SOURCE)
        + r'"\s*\{(?P<body>.*?)\n\}',
        text,
        re.DOTALL,
    )
    if block_match is None:
        raise InfraManagerError(
            "Lock-файл OpenTofu не содержит bpg/proxmox"
        )
    body = block_match.group("body")
    version_match = re.search(
        r'^\s*version\s*=\s*"([^"]+)"',
        body,
        re.MULTILINE,
    )
    if version_match is None:
        raise InfraManagerError(
            "Lock-файл OpenTofu не содержит версию bpg/proxmox"
        )
    hashes = frozenset(
        match.lower()
        for match in re.findall(r'"zh:([0-9a-fA-F]{64})"', body)
    )
    if not hashes:
        raise InfraManagerError(
            "Lock-файл OpenTofu не содержит SHA256 bpg/proxmox"
        )
    return version_match.group(1), hashes


def _provider_platform() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if system != "linux" or arch is None:
        raise InfraManagerError(
            f"Неподдерживаемая платформа провайдера: {system}/{machine}"
        )
    return system, arch


def _download_text(url: str) -> str:
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.read().decode("utf-8")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        UnicodeDecodeError,
        OSError,
    ) as exc:
        raise InfraManagerError(
            f"Не удалось загрузить {url}: {exc}"
        ) from exc


def _download_file(url: str, target: Path) -> None:
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            with target.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        target.unlink(missing_ok=True)
        raise InfraManagerError(
            f"Не удалось загрузить {url}: {exc}"
        ) from exc


def _prepare_proxmox_provider_mirror(opentofu_dir: Path) -> Path:
    """Подготовить проверенный локальный пакет bpg/proxmox без Registry."""

    version, locked_hashes = _proxmox_provider_lock(opentofu_dir)
    system, arch = _provider_platform()
    filename = (
        f"terraform-provider-proxmox_{version}_{system}_{arch}.zip"
    )
    mirror = (
        PROVIDER_MIRROR_DIR
        / "registry.opentofu.org"
        / "bpg"
        / "proxmox"
    )
    package = mirror / filename
    if package.is_file() and _sha256(package) in locked_hashes:
        return PROVIDER_MIRROR_DIR

    release_base = f"{PROXMOX_PROVIDER_RELEASE}/v{version}"
    sums_url = (
        f"{release_base}/terraform-provider-proxmox_{version}_SHA256SUMS"
    )
    sums = _download_text(sums_url)
    expected = None
    for line in sums.splitlines():
        checksum, separator, name = line.strip().partition("  ")
        if separator and name == filename:
            expected = checksum.lower()
            break
    if expected is None or expected not in locked_hashes:
        raise InfraManagerError(
            f"SHA256 {filename} отсутствует в доверенном lock-файле"
        )

    mirror.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{filename}.",
        dir=mirror,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        _download_file(
            f"{release_base}/{filename}",
            temporary_path,
        )
        actual = _sha256(temporary_path)
        if actual != expected:
            raise InfraManagerError(
                f"SHA256 {filename} не совпадает: {actual}"
            )
        temporary_path.replace(package)
    finally:
        temporary_path.unlink(missing_ok=True)

    return PROVIDER_MIRROR_DIR


def _init(opentofu_dir: Path, env: dict[str, str]) -> None:
    run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "init",
            "-input=false",
            "-no-color",
            "-lockfile=readonly",
            f"-backend-config=path={STATE_FILE}",
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
        console.result("OpenTofu: изменений нет")
        return 0
    if result.returncode == 2:
        console.result("OpenTofu: план содержит изменения")
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
