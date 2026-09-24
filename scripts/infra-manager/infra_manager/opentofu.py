"""Задания OpenTofu для infra-manager."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .pve import PveClient
from .settings import PATHS

CA_BUNDLE = PATHS.ca_bundle
STATE_DIR = PATHS.opentofu_dir
GUEST_STATE_FILE = PATHS.opentofu_input


@dataclass(frozen=True)
class DeploymentPaths:
    """Пути, используемые на нескольких этапах развёртывания VM."""

    opentofu_dir: Path
    guest_dir: Path
    private_key: Path
    playbook: Path
    known_hosts: Path
    plan_file: Path


@dataclass(frozen=True)
class DeploymentContext:
    """Общие неизменяемые данные одного развёртывания VM."""

    client: PveClient
    vmid: int
    name: str
    node: str
    template_vmid: int
    address: str
    target: str
    env: dict[str, str]
    paths: DeploymentPaths


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


def _prepare_input(repo_root: Path) -> tuple[Path, dict[str, Any]]:
    require_command("tofu")
    _require_env("TF_VAR_pve_endpoint")
    _require_env("TF_VAR_pve_api_token")
    _require_env("TF_VAR_ansible_ssh_public_key")
    opentofu_dir, renderer = _paths(repo_root)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    run(
        [
            sys.executable,
            str(renderer),
            "--output",
            str(GUEST_STATE_FILE),
        ]
    )
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
    return opentofu_dir, payload


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

    opentofu_dir, _ = _prepare_input(repo_root)
    env = _opentofu_env()

    console.info("Инициализация OpenTofu")
    _init(opentofu_dir, env)

    console.info("Построение общего плана OpenTofu")
    result = run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=30s",
            "-detailed-exitcode",
        ],
        check=False,
        env=env,
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
    show = run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "state",
            "show",
            target,
        ],
        check=False,
        capture_output=True,
        env=env,
    )
    if show.returncode != 0:
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

    resources = state.get("resources", [])
    if not isinstance(resources, list):
        return True, ""

    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if (
            resource.get("type") != "proxmox_virtual_environment_vm"
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
    return True, ""


def _find_guest_directory(repo_root: Path, vmid: int) -> Path:
    guests_root = repo_root / "infrastructure" / "guests"
    matches = sorted(
        path
        for path in guests_root.glob(f"{vmid}-*")
        if path.is_dir() and (path / "guest.yaml").is_file()
    )
    if len(matches) != 1:
        raise InfraManagerError(
            f"Для VMID {vmid} ожидается ровно один каталог гостя, "
            f"найдено: {len(matches)}"
        )
    return matches[0]


def _remove_known_host(known_hosts: Path, address: str) -> None:
    if not known_hosts.exists():
        return
    run(
        [
            "ssh-keygen",
            "-R",
            address,
            "-f",
            str(known_hosts),
        ],
        check=False,
        capture_output=True,
    )


def _recover_tainted_vm(context: DeploymentContext) -> None:
    resource = context.client.find_vm(context.vmid)
    if resource is None:
        raise InfraManagerError(
            f"VM {context.vmid} есть в OpenTofu state, но отсутствует в PVE"
        )

    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    if actual_type != "qemu" or actual_name != context.name:
        raise InfraManagerError(
            f"VMID {context.vmid} занят объектом '{actual_name}' "
            f"типа '{actual_type}'; автоматическое удаление запрещено"
        )

    console.info(
        f"Удаление незавершённой VM {context.vmid} после неудачного применения"
    )
    if (
        context.client.vm_status(
            node=context.node,
            vmid=context.vmid,
        )
        == "running"
    ):
        context.client.run_task(
            "POST",
            f"/nodes/{context.node}/qemu/{context.vmid}/status/stop",
            node=context.node,
        )

    context.client.put(
        f"/nodes/{context.node}/qemu/{context.vmid}/config",
        form={"protection": 0},
    )
    context.client.run_task(
        "DELETE",
        f"/nodes/{context.node}/qemu/{context.vmid}",
        node=context.node,
        query={"purge": 1},
    )
    if context.client.find_vm(context.vmid) is not None:
        raise InfraManagerError(
            f"Незавершённую VM {context.vmid} не удалось удалить"
        )

    run(
        [
            "tofu",
            f"-chdir={context.paths.opentofu_dir}",
            "state",
            "rm",
            context.target,
        ],
        env=env,
    )
    _remove_known_host(context.paths.known_hosts, context.address)
    console.ok(
        f"Незавершённая VM {context.vmid} удалена; "
        "OpenTofu state очищен"
    )

def _validate_pve_and_state(
    context: DeploymentContext,
    *,
    state_present: bool,
    state_status: str,
) -> tuple[bool, str]:
    resource = context.client.find_vm(context.vmid)

    if resource is not None:
        actual_type = str(resource.get("type") or "")
        actual_name = str(resource.get("name") or "")
        if actual_type != "qemu" or actual_name != context.name:
            raise InfraManagerError(
                f"VMID {context.vmid} уже занят объектом '{actual_name}' "
                f"типа '{actual_type}'; автоматическое управление запрещено"
            )
        if not state_present:
            raise InfraManagerError(
                f"VM {context.vmid} существует в PVE, но отсутствует "
                "в OpenTofu state; автоматический импорт клонированной "
                "VM запрещён"
            )
        if state_status == "tainted":
            _recover_tainted_vm(context)
            return False, ""
        return state_present, state_status

    if state_present:
        raise InfraManagerError(
            f"VM {context.vmid} есть в OpenTofu state, но отсутствует в PVE"
        )
    return state_present, state_status

def _set_template_protection(
    client: PveClient,
    *,
    node: str,
    template_vmid: int,
    enabled: bool,
) -> None:
    client.put(
        f"/nodes/{node}/qemu/{template_vmid}/config",
        form={"protection": 1 if enabled else 0},
    )


def _apply_plan(
    context: DeploymentContext,
    actions: list[str],
) -> None:
    changed_template_protection = False
    try:
        if "create" in actions:
            config = context.client.vm_config(
                node=context.node,
                vmid=context.template_vmid,
            )
            if config.get("template") not in (1, True, "1", "true"):
                raise InfraManagerError(
                    f"VMID {context.template_vmid} не является шаблоном"
                )
            if config.get("protection") in (1, True, "1", "true"):
                console.info(
                    "Временное снятие защиты шаблона "
                    f"{context.template_vmid} для клонирования"
                )
                _set_template_protection(
                    context.client,
                    node=context.node,
                    template_vmid=context.template_vmid,
                    enabled=False,
                )
                changed_template_protection = True

        console.info(f"Применение состояния VM {context.vmid}")
        run(
            [
                "tofu",
                f"-chdir={context.paths.opentofu_dir}",
                "apply",
                "-input=false",
                "-no-color",
                "-lock-timeout=30s",
                str(context.paths.plan_file),
            ],
            env=env,
        )
    finally:
        if changed_template_protection:
            _set_template_protection(
                context.client,
                node=context.node,
                template_vmid=context.template_vmid,
                enabled=True,
            )
            console.ok(
                f"Защита шаблона {context.template_vmid} восстановлена"
            )

def _ensure_ssh_host_key(
    known_hosts: Path,
    address: str,
) -> None:
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.touch(exist_ok=True)
    known_hosts.chmod(0o600)

    found = run(
        [
            "ssh-keygen",
            "-F",
            address,
            "-f",
            str(known_hosts),
        ],
        check=False,
        capture_output=True,
    )
    if found.returncode == 0:
        return

    console.info(f"Первичное получение SSH host key {address}")
    for _ in range(30):
        scan = run(
            [
                "ssh-keyscan",
                "-T",
                "5",
                "-H",
                address,
            ],
            check=False,
            capture_output=True,
        )
        if scan.returncode == 0 and scan.stdout.strip():
            with known_hosts.open("a", encoding="utf-8") as stream:
                stream.write(scan.stdout)
                if not scan.stdout.endswith("\n"):
                    stream.write("\n")
            known_hosts.chmod(0o600)
            console.ok(f"SSH host key {address} принят")
            return
        time.sleep(2)

    raise InfraManagerError(
        f"Не удалось получить SSH host key по адресу {address}"
    )


def _build_deployment_context(
    repo_root: Path,
    vmid: int,
    opentofu_dir: Path,
    env: dict[str, str],
    payload: dict[str, Any],
) -> DeploymentContext:
    """Проверить описание VM и собрать общий контекст развёртывания."""
    guests = payload["guests"]
    guest = guests.get(str(vmid))
    if not isinstance(guest, dict):
        raise InfraManagerError(
            f"VMID {vmid} отсутствует в итоговом состоянии OpenTofu"
        )
    if guest.get("type") != "vm":
        raise InfraManagerError(f"VMID {vmid} должен иметь type=vm")

    name = str(guest.get("name") or "")
    node = str(guest.get("node") or "")
    template_vmid = guest.get("template_vmid")
    network = guest.get("network")
    if not name or not node or not isinstance(template_vmid, int):
        raise InfraManagerError(
            f"VMID {vmid}: неполное итоговое описание VM"
        )
    if not isinstance(network, dict):
        raise InfraManagerError(f"VMID {vmid}: отсутствует network")

    ipv4 = str(network.get("ipv4") or "")
    if not ipv4 or ipv4 == "dhcp":
        raise InfraManagerError(
            f"VMID {vmid}: для текущего deploy-guest требуется "
            "статический IPv4"
        )
    address = ipv4.split("/", 1)[0]

    guest_dir = _find_guest_directory(repo_root, vmid)
    provision_file = guest_dir / "provision.yaml"
    if not provision_file.is_file():
        raise InfraManagerError(
            f"Не найден provision.yaml: {provision_file}"
        )

    private_key = PATHS.ansible_private_key
    if not private_key.is_file():
        raise InfraManagerError(
            f"Не найден закрытый ключ Ansible: {private_key}"
        )

    playbook = (
        repo_root
        / "automation"
        / "ansible"
        / "playbooks"
        / "configure-guest.yml"
    )
    if not playbook.is_file():
        raise InfraManagerError(f"Не найден playbook: {playbook}")

    paths = DeploymentPaths(
        opentofu_dir=opentofu_dir,
        guest_dir=guest_dir,
        private_key=private_key,
        playbook=playbook,
        known_hosts=Path("/var/lib/semaphore/guest-known-hosts"),
        plan_file=STATE_DIR / f"{vmid}.tfplan",
    )
    return DeploymentContext(
        client=PveClient.from_opentofu_env(),
        vmid=vmid,
        name=name,
        node=node,
        template_vmid=template_vmid,
        address=address,
        target=f'proxmox_virtual_environment_vm.guest["{vmid}"]',
        env=env,
        paths=paths,
    )


def run_deploy_guest(repo_root: Path, vmid: int) -> int:
    """Привести одну VM к состоянию guest.yaml + provision.yaml."""

    if vmid <= 0:
        raise InfraManagerError("VMID должен быть положительным числом")

    for command in (
        "tofu",
        "ansible-playbook",
        "ssh-keygen",
        "ssh-keyscan",
    ):
        require_command(command)

    console.info("Проверка проекта")
    run([sys.executable, str(repo_root / "scripts" / "validate_repo.py")])

    opentofu_dir, payload = _prepare_input(repo_root)
    env = _opentofu_env()
    context = _build_deployment_context(
        repo_root,
        vmid,
        opentofu_dir,
        env,
        payload,
    )

    console.info("Инициализация OpenTofu")
    _init(context.paths.opentofu_dir, context.env)

    state_present, state_status = _state_status(
        context.paths.opentofu_dir,
        context.target,
        context.env,
    )
    state_present, state_status = _validate_pve_and_state(
        context,
        state_present=state_present,
        state_status=state_status,
    )

    console.info(f"План OpenTofu для {context.vmid}")
    try:
        run(
            [
                "tofu",
                f"-chdir={context.paths.opentofu_dir}",
                "plan",
                "-input=false",
                "-no-color",
                "-lock-timeout=30s",
                f"-target={context.target}",
                f"-out={context.paths.plan_file}",
            ],
            env=env,
        )

        shown = run(
            [
                "tofu",
                f"-chdir={context.paths.opentofu_dir}",
                "show",
                "-json",
                str(context.paths.plan_file),
            ],
            capture_output=True,
            env=env,
        )
        try:
            plan = json.loads(shown.stdout)
        except json.JSONDecodeError as exc:
            raise InfraManagerError(
                "OpenTofu plan содержит некорректный JSON"
            ) from exc

        changes = plan.get("resource_changes", [])
        if not isinstance(changes, list):
            raise InfraManagerError(
                "OpenTofu plan содержит некорректный resource_changes"
            )

        unexpected = [
            str(change.get("address"))
            for change in changes
            if isinstance(change, dict)
            and change.get("address") != context.target
        ]
        if unexpected:
            raise InfraManagerError(
                f"План VM {context.vmid} содержит изменения других ресурсов: "
                + ", ".join(unexpected)
            )

        actions: list[str] = []
        for change in changes:
            if not isinstance(change, dict) or change.get("address") != context.target:
                continue
            change_data = change.get("change")
            if isinstance(change_data, dict):
                raw_actions = change_data.get("actions")
                if isinstance(raw_actions, list):
                    actions = [str(action) for action in raw_actions]

        run(
            [
                "tofu",
                f"-chdir={context.paths.opentofu_dir}",
                "show",
                "-no-color",
                str(context.paths.plan_file),
            ],
            env=env,
        )

        if not actions or actions == ["no-op"]:
            console.ok(
                f"OpenTofu: состояние VM {context.vmid} уже соответствует конфигурации"
            )
        else:
            _apply_plan(context, actions)
            console.ok(f"Состояние VM {context.vmid} применено")
    finally:
        context.paths.plan_file.unlink(missing_ok=True)

    _ensure_ssh_host_key(context.paths.known_hosts, context.address)

    console.info(f"Настройка ОС {context.vmid} через Ansible")
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    ansible_env["ANSIBLE_SSH_ARGS"] = (
        f"-o UserKnownHostsFile={context.paths.known_hosts} "
        "-o StrictHostKeyChecking=yes"
    )
    run(
        [
            "ansible-playbook",
            "-i",
            f"{context.address},",
            "-u",
            "root",
            "--private-key",
            str(context.paths.private_key),
            "-e",
            f"guest={context.paths.guest_dir.name}",
            str(context.paths.playbook),
        ],
        env=ansible_env,
    )

    console.ok(f"{context.vmid} {context.name} создан и базово настроен")
    return 0
