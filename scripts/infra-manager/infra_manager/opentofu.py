"""Задания OpenTofu для infra-manager."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console, require_command, run
from .pve import CA_BUNDLE, PveClient

STATE_DIR = Path("/var/lib/infra-manager/opentofu")
GUEST_STATE_FILE = STATE_DIR / "guests.json"


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


def _recover_tainted_vm(
    *,
    client: PveClient,
    opentofu_dir: Path,
    env: dict[str, str],
    target: str,
    node: str,
    vmid: int,
    expected_name: str,
    known_hosts: Path,
    address: str,
) -> None:
    resource = client.find_vm(vmid)
    if resource is None:
        raise InfraManagerError(
            f"VM {vmid} есть в OpenTofu state, но отсутствует в PVE"
        )

    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    if actual_type != "qemu" or actual_name != expected_name:
        raise InfraManagerError(
            f"VMID {vmid} занят объектом '{actual_name}' "
            f"типа '{actual_type}'; автоматическое удаление запрещено"
        )

    console.info(
        f"Удаление незавершённой VM {vmid} после неудачного применения"
    )
    if client.vm_status(node=node, vmid=vmid) == "running":
        client.run_task(
            "POST",
            f"/nodes/{node}/qemu/{vmid}/status/stop",
            node=node,
        )

    client.put(
        f"/nodes/{node}/qemu/{vmid}/config",
        form={"protection": 0},
    )
    client.run_task(
        "DELETE",
        f"/nodes/{node}/qemu/{vmid}",
        node=node,
        query={"purge": 1},
    )
    if client.find_vm(vmid) is not None:
        raise InfraManagerError(
            f"Незавершённую VM {vmid} не удалось удалить"
        )

    run(
        [
            "tofu",
            f"-chdir={opentofu_dir}",
            "state",
            "rm",
            target,
        ],
        env=env,
    )
    _remove_known_host(known_hosts, address)
    console.ok(
        f"Незавершённая VM {vmid} удалена; OpenTofu state очищен"
    )


def _validate_pve_and_state(
    *,
    client: PveClient,
    opentofu_dir: Path,
    env: dict[str, str],
    target: str,
    node: str,
    vmid: int,
    expected_name: str,
    state_present: bool,
    state_status: str,
    known_hosts: Path,
    address: str,
) -> tuple[bool, str]:
    resource = client.find_vm(vmid)

    if resource is not None:
        actual_type = str(resource.get("type") or "")
        actual_name = str(resource.get("name") or "")
        if actual_type != "qemu" or actual_name != expected_name:
            raise InfraManagerError(
                f"VMID {vmid} уже занят объектом '{actual_name}' "
                f"типа '{actual_type}'; автоматическое управление запрещено"
            )
        if not state_present:
            raise InfraManagerError(
                f"VM {vmid} существует в PVE, но отсутствует "
                "в OpenTofu state; автоматический импорт клонированной "
                "VM запрещён"
            )
        if state_status == "tainted":
            _recover_tainted_vm(
                client=client,
                opentofu_dir=opentofu_dir,
                env=env,
                target=target,
                node=node,
                vmid=vmid,
                expected_name=expected_name,
                known_hosts=known_hosts,
                address=address,
            )
            return False, ""
        return state_present, state_status

    if state_present:
        raise InfraManagerError(
            f"VM {vmid} есть в OpenTofu state, но отсутствует в PVE"
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
    *,
    client: PveClient,
    opentofu_dir: Path,
    env: dict[str, str],
    plan_file: Path,
    actions: list[str],
    node: str,
    template_vmid: int,
    vmid: int,
) -> None:
    changed_template_protection = False
    try:
        if "create" in actions:
            config = client.vm_config(node=node, vmid=template_vmid)
            if config.get("template") not in (1, True, "1", "true"):
                raise InfraManagerError(
                    f"VMID {template_vmid} не является шаблоном"
                )
            if config.get("protection") in (1, True, "1", "true"):
                console.info(
                    f"Временное снятие защиты шаблона "
                    f"{template_vmid} для клонирования"
                )
                _set_template_protection(
                    client,
                    node=node,
                    template_vmid=template_vmid,
                    enabled=False,
                )
                changed_template_protection = True

        console.info(f"Применение состояния VM {vmid}")
        run(
            [
                "tofu",
                f"-chdir={opentofu_dir}",
                "apply",
                "-input=false",
                "-no-color",
                "-lock-timeout=30s",
                str(plan_file),
            ],
            env=env,
        )
    finally:
        if changed_template_protection:
            _set_template_protection(
                client,
                node=node,
                template_vmid=template_vmid,
                enabled=True,
            )
            console.ok(
                f"Защита шаблона {template_vmid} восстановлена"
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
    guests = payload["guests"]
    guest = guests.get(str(vmid))
    if not isinstance(guest, dict):
        raise InfraManagerError(
            f"VMID {vmid} отсутствует в итоговом состоянии OpenTofu"
        )
    if guest.get("type") != "vm":
        raise InfraManagerError(
            f"VMID {vmid} должен иметь type=vm"
        )

    name = str(guest.get("name") or "")
    node = str(guest.get("node") or "")
    template_vmid = guest.get("template_vmid")
    network = guest.get("network")
    if not name or not node or not isinstance(template_vmid, int):
        raise InfraManagerError(
            f"VMID {vmid}: неполное итоговое описание VM"
        )
    if not isinstance(network, dict):
        raise InfraManagerError(
            f"VMID {vmid}: отсутствует network"
        )

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

    private_key = Path("/etc/infra-manager/ansible/guest_ed25519")
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

    known_hosts = Path("/var/lib/semaphore/guest-known-hosts")
    plan_file = STATE_DIR / f"{vmid}.tfplan"
    target = f'proxmox_virtual_environment_vm.guest["{vmid}"]'

    console.info("Инициализация OpenTofu")
    _init(opentofu_dir, env)

    state_present, state_status = _state_status(
        opentofu_dir,
        target,
        env,
    )
    client = PveClient.from_opentofu_env()
    state_present, state_status = _validate_pve_and_state(
        client=client,
        opentofu_dir=opentofu_dir,
        env=env,
        target=target,
        node=node,
        vmid=vmid,
        expected_name=name,
        state_present=state_present,
        state_status=state_status,
        known_hosts=known_hosts,
        address=address,
    )

    console.info(f"План OpenTofu для {vmid}")
    try:
        run(
            [
                "tofu",
                f"-chdir={opentofu_dir}",
                "plan",
                "-input=false",
                "-no-color",
                "-lock-timeout=30s",
                f"-target={target}",
                f"-out={plan_file}",
            ],
            env=env,
        )

        shown = run(
            [
                "tofu",
                f"-chdir={opentofu_dir}",
                "show",
                "-json",
                str(plan_file),
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
            and change.get("address") != target
        ]
        if unexpected:
            raise InfraManagerError(
                f"План VM {vmid} содержит изменения других ресурсов: "
                + ", ".join(unexpected)
            )

        actions: list[str] = []
        for change in changes:
            if not isinstance(change, dict) or change.get("address") != target:
                continue
            change_data = change.get("change")
            if isinstance(change_data, dict):
                raw_actions = change_data.get("actions")
                if isinstance(raw_actions, list):
                    actions = [str(action) for action in raw_actions]

        run(
            [
                "tofu",
                f"-chdir={opentofu_dir}",
                "show",
                "-no-color",
                str(plan_file),
            ],
            env=env,
        )

        if not actions or actions == ["no-op"]:
            console.ok(
                f"OpenTofu: состояние VM {vmid} уже соответствует конфигурации"
            )
        else:
            _apply_plan(
                client=client,
                opentofu_dir=opentofu_dir,
                env=env,
                plan_file=plan_file,
                actions=actions,
                node=node,
                template_vmid=template_vmid,
                vmid=vmid,
            )
            console.ok(f"Состояние VM {vmid} применено")
    finally:
        plan_file.unlink(missing_ok=True)

    _ensure_ssh_host_key(known_hosts, address)

    console.info(f"Настройка ОС {vmid} через Ansible")
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    ansible_env["ANSIBLE_SSH_ARGS"] = (
        f"-o UserKnownHostsFile={known_hosts} "
        "-o StrictHostKeyChecking=yes"
    )
    run(
        [
            "ansible-playbook",
            "-i",
            f"{address},",
            "-u",
            "root",
            "--private-key",
            str(private_key),
            "-e",
            f"guest={guest_dir.name}",
            str(playbook),
        ],
        env=ansible_env,
    )

    console.ok(f"{vmid} {name} создан и базово настроен")
    return 0
