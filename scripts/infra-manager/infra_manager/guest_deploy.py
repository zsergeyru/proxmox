"""Развёртывание одной гостевой VM через OpenTofu и Ansible."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common import InfraManagerError, console, require_command, run
from .opentofu import OpenTofuWorkspace, prepare_workspace
from .pve import PveClient
from .pve_host import apply_host_requirements
from .settings import PATHS, SETTINGS


@dataclass(frozen=True)
class DeploymentPaths:
    """Пути, используемые на нескольких этапах развёртывания VM."""

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
    kind: str
    features: tuple[str, ...]
    template_vmid: int | None
    address: str
    target: str
    workspace: OpenTofuWorkspace
    paths: DeploymentPaths


@dataclass(frozen=True)
class GuestPlan:
    """Проверенный OpenTofu plan для одной гостевой VM."""

    actions: tuple[str, ...]


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


def _recover_tainted_guest(context: DeploymentContext) -> None:
    resource = context.client.find_vm(context.vmid)
    if resource is None:
        raise InfraManagerError(
            f"Гость {context.vmid} есть в OpenTofu state, но отсутствует в PVE"
        )

    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    expected_type = "qemu" if context.kind == "vm" else "lxc"
    if actual_type != expected_type or actual_name != context.name:
        raise InfraManagerError(
            f"VMID {context.vmid} занят объектом '{actual_name}' "
            f"типа '{actual_type}'; автоматическое удаление запрещено"
        )

    console.info(
        f"Удаление незавершённого гостя {context.vmid} после неудачного применения"
    )
    api_kind = "qemu" if context.kind == "vm" else "lxc"
    status = context.client.data(
        f"/nodes/{context.node}/{api_kind}/{context.vmid}/status/current"
    )
    if isinstance(status, dict) and status.get("status") == "running":
        context.client.run_task(
            "POST",
            f"/nodes/{context.node}/{api_kind}/{context.vmid}/status/stop",
            node=context.node,
        )

    context.client.put(
        f"/nodes/{context.node}/{api_kind}/{context.vmid}/config",
        form={"protection": 0},
    )
    context.client.run_task(
        "DELETE",
        f"/nodes/{context.node}/{api_kind}/{context.vmid}",
        node=context.node,
        query={"purge": 1},
    )
    if context.client.find_vm(context.vmid) is not None:
        raise InfraManagerError(
            f"Незавершённого гостя {context.vmid} не удалось удалить"
        )

    run(
        [
            "tofu",
            f"-chdir={context.workspace.directory}",
            "state",
            "rm",
            context.target,
        ],
        env=context.workspace.env,
    )
    _remove_known_host(context.paths.known_hosts, context.address)
    console.ok(
        f"Незавершённый гость {context.vmid} удалён; OpenTofu state очищен"
    )


def _validate_pve_and_state(
    context: DeploymentContext,
    *,
    state_present: bool,
    state_status: str,
) -> None:
    resource = context.client.find_vm(context.vmid)

    if resource is not None:
        actual_type = str(resource.get("type") or "")
        actual_name = str(resource.get("name") or "")
        expected_type = "qemu" if context.kind == "vm" else "lxc"
        if actual_type != expected_type or actual_name != context.name:
            raise InfraManagerError(
                f"VMID {context.vmid} уже занят объектом '{actual_name}' "
                f"типа '{actual_type}'; автоматическое управление запрещено"
            )
        if not state_present:
            raise InfraManagerError(
                f"Гость {context.vmid} существует в PVE, но отсутствует "
                "в OpenTofu state; автоматический импорт клонированной "
                "VM запрещён"
            )
        if state_status == "tainted":
            _recover_tainted_guest(context)
        return

    if state_present:
        raise InfraManagerError(
            f"Гость {context.vmid} есть в OpenTofu state, но отсутствует в PVE"
        )


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
        if "create" in actions and context.kind == "vm":
            if context.template_vmid is None:
                raise InfraManagerError("Для VM не задан template_vmid")
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

        console.info(f"Применение состояния гостя {context.vmid}")
        run(
            [
                "tofu",
                f"-chdir={context.workspace.directory}",
                "apply",
                "-input=false",
                "-no-color",
                "-lock-timeout=30s",
                str(context.paths.plan_file),
            ],
            env=context.workspace.env,
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

    console.info(f"Проверка ключа SSH-сервера {address}")
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
            console.ok(f"Ключ SSH-сервера {address} сохранён")
            return
        time.sleep(2)

    raise InfraManagerError(
        f"Не удалось получить SSH host key по адресу {address}"
    )


def _build_deployment_context(
    repo_root: Path,
    vmid: int,
    workspace: OpenTofuWorkspace,
) -> DeploymentContext:
    """Проверить описание VM и собрать общий контекст развёртывания."""
    guests = workspace.payload["guests"]
    guest = guests.get(str(vmid))
    if not isinstance(guest, dict):
        raise InfraManagerError(
            f"VMID {vmid} отсутствует в итоговом состоянии OpenTofu"
        )
    kind = str(guest.get("type") or "")
    if kind not in {"vm", "lxc"}:
        raise InfraManagerError(
            f"VMID {vmid}: неподдерживаемый type={kind!r}"
        )

    name = str(guest.get("name") or "")
    node = str(guest.get("node") or "")
    template_vmid = guest.get("template_vmid") if kind == "vm" else None
    raw_features = guest.get("features", [])
    if not isinstance(raw_features, list) or any(
        not isinstance(item, str) for item in raw_features
    ):
        raise InfraManagerError(
            f"VMID {vmid}: features имеет некорректный формат"
        )
    features = tuple(raw_features)
    network = guest.get("network")
    if not name or not node:
        raise InfraManagerError(
            f"VMID {vmid}: неполное итоговое описание гостя"
        )
    if kind == "vm" and not isinstance(template_vmid, int):
        raise InfraManagerError(
            f"VMID {vmid}: для VM отсутствует template_vmid"
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
        guest_dir=guest_dir,
        private_key=private_key,
        playbook=playbook,
        known_hosts=Path("/var/lib/semaphore/guest-known-hosts"),
        plan_file=workspace.state_dir / f"{vmid}.tfplan",
    )
    return DeploymentContext(
        client=PveClient.from_opentofu_env(),
        vmid=vmid,
        name=name,
        node=node,
        kind=kind,
        features=features,
        template_vmid=template_vmid if isinstance(template_vmid, int) else None,
        address=address,
        target=(
            f'proxmox_virtual_environment_vm.guest["{vmid}"]'
            if kind == "vm"
            else f'proxmox_virtual_environment_container.guest["{vmid}"]'
        ),
        workspace=workspace,
        paths=paths,
    )


def _build_guest_plan(context: DeploymentContext) -> GuestPlan:
    """Построить plan и разрешить изменения только выбранного гостя."""

    run(
        [
            "tofu",
            f"-chdir={context.workspace.directory}",
            "plan",
            "-input=false",
            "-no-color",
            "-lock-timeout=30s",
            f"-target={context.target}",
            f"-out={context.paths.plan_file}",
        ],
        env=context.workspace.env,
    )

    shown = run(
        [
            "tofu",
            f"-chdir={context.workspace.directory}",
            "show",
            "-json",
            str(context.paths.plan_file),
        ],
        capture_output=True,
        env=context.workspace.env,
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
        if isinstance(change, dict) and change.get("address") != context.target
    ]
    if unexpected:
        raise InfraManagerError(
            f"План гостя {context.vmid} содержит изменения других ресурсов: "
            + ", ".join(unexpected)
        )

    actions: tuple[str, ...] = ()
    for change in changes:
        if not isinstance(change, dict) or change.get("address") != context.target:
            continue
        change_data = change.get("change")
        if isinstance(change_data, dict):
            raw_actions = change_data.get("actions")
            if isinstance(raw_actions, list):
                actions = tuple(str(action) for action in raw_actions)

    run(
        [
            "tofu",
            f"-chdir={context.workspace.directory}",
            "show",
            "-no-color",
            str(context.paths.plan_file),
        ],
        env=context.workspace.env,
    )
    return GuestPlan(actions=actions)


def _configure_guest_os(
    context: DeploymentContext,
    *,
    provision_phase: str = "full",
) -> None:
    """Принять SSH host key и применить конфигурацию Ansible."""

    _ensure_ssh_host_key(context.paths.known_hosts, context.address)

    console.info(f"Настройка ОС {context.vmid}")
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
            "-e",
            f"provision_phase={provision_phase}",
            str(context.paths.playbook),
        ],
        env=ansible_env,
    )


def _validate_existing_guest_object(context: DeploymentContext) -> None:
    """Проверить существующий объект без требования OpenTofu state."""
    resource = context.client.find_vm(context.vmid)
    if resource is None:
        raise InfraManagerError(
            f"Гость {context.vmid} отсутствует в PVE; "
            "режим provision-existing не может его создать"
        )
    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    expected_type = "qemu" if context.kind == "vm" else "lxc"
    if actual_type != expected_type or actual_name != context.name:
        raise InfraManagerError(
            f"VMID {context.vmid} занят объектом '{actual_name}' "
            f"типа '{actual_type}'; настройка запрещена"
        )


def _reconcile_guest_infrastructure(context: DeploymentContext) -> None:
    """Применить состояние одной VM и всегда удалить временный plan."""

    console.info(f"Проверка состояния гостя {context.vmid}")
    try:
        plan = _build_guest_plan(context)
        if not plan.actions or plan.actions == ("no-op",):
            console.ok(
                f"OpenTofu: состояние гостя {context.vmid} "
                "уже соответствует конфигурации"
            )
        else:
            _apply_plan(context, list(plan.actions))
            console.ok(f"Состояние гостя {context.vmid} применено")
    finally:
        context.paths.plan_file.unlink(missing_ok=True)


def _project_revision(repo_root: Path) -> str:
    """Вернуть короткий хэш выполняемой рабочей копии проекта."""
    result = run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        return "не определена"
    return result.stdout.strip() or "не определена"


def _show_guest_summary(
    repo_root: Path,
    context: DeploymentContext,
) -> None:
    """Показать итог полного развёртывания обычного гостя."""
    separator = "=" * 60
    kind = "VM" if context.kind == "vm" else "LXC"
    revision = _project_revision(repo_root)

    print()
    print(separator)
    print(f"  Гость {context.vmid} {context.name} готов")
    print(separator)
    print()
    console.ok(f"Гость {context.vmid} запущен")
    console.ok("Состояние Proxmox приведено к описанию гостя")
    console.ok("Настройка ОС через Ansible завершена")

    print("\nСистема")
    print(f"  Тип:     {kind}")
    print(f"  Адрес:   {context.address}")
    print(f"  Узел:    {context.node}")

    print("\nПроект")
    print(f"  Ветка:   {SETTINGS.project_branch()}")
    print(f"  Версия:  {revision}")

    print()
    print(separator)
    print("  Развёртывание завершено без ошибок")
    print(separator)


def run_deploy_guest(
    repo_root: Path,
    vmid: int,
    *,
    bootstrap_scope: bool = False,
    phase: str = "all",
) -> int:
    """Привести одного гостя к состоянию guest.yaml + provision.yaml."""

    if vmid <= 0:
        raise InfraManagerError("VMID должен быть положительным числом")
    if phase not in {
        "all",
        "infrastructure",
        "provision-base",
        "provision",
        "provision-existing",
    }:
        raise InfraManagerError(f"Неизвестная фаза deploy-guest: {phase}")
    if bootstrap_scope and vmid not in SETTINGS.bootstrap_managed_vmids:
        raise InfraManagerError(
            f"VMID {vmid} не принадлежит начальному контуру"
        )
    if phase == "provision-existing" and not bootstrap_scope:
        raise InfraManagerError(
            "provision-existing разрешён только начальному контуру"
        )

    for command in (
        "tofu",
        "ansible-playbook",
        "ssh-keygen",
        "ssh-keyscan",
        "ssh",
    ):
        require_command(command)

    console.info("Проверка проекта")
    run([sys.executable, str(repo_root / "scripts" / "validate_repo.py")])

    workspace = prepare_workspace(
        repo_root,
        only_vmids={vmid} if bootstrap_scope else None,
        exclude_vmids=set() if bootstrap_scope else None,
    )
    context = _build_deployment_context(repo_root, vmid, workspace)

    console.info("Инициализация OpenTofu")
    context.workspace.initialize()

    state_present, state_status = context.workspace.get_resource_state(
        context.target
    )
    if phase == "provision-existing":
        _validate_existing_guest_object(context)
    else:
        _validate_pve_and_state(
            context,
            state_present=state_present,
            state_status=state_status,
        )

    if phase in {"all", "infrastructure"}:
        _reconcile_guest_infrastructure(context)

    if phase in {"provision-base", "provision"}:
        state_present, state_status = context.workspace.get_resource_state(
            context.target
        )
        _validate_pve_and_state(
            context,
            state_present=state_present,
            state_status=state_status,
        )
        if not state_present:
            raise InfraManagerError(
                f"Гость {context.vmid} отсутствует в OpenTofu state; "
                "сначала выполните фазу infrastructure"
            )

    # Некоторые свойства LXC Proxmox разрешает менять только самому root@pam,
    # а не API token. Они остаются частью общего guest/profile-контракта и
    # применяются здесь одинаково для bootstrap 910 и обычных гостей.
    apply_host_requirements(
        node=context.node,
        vmid=context.vmid,
        kind=context.kind,
        features=context.features,
    )

    if phase == "all":
        _configure_guest_os(context)
    elif phase == "provision-base":
        _configure_guest_os(context, provision_phase="base")
    elif phase in {"provision", "provision-existing"}:
        _configure_guest_os(context, provision_phase="full")

    if phase == "infrastructure":
        console.ok(
            f"{context.vmid} {context.name}: основа создана"
        )
    elif phase == "provision-base":
        console.ok(
            f"{context.vmid} {context.name}: базовая настройка завершена"
        )
    elif phase == "provision":
        console.ok(
            f"{context.vmid} {context.name}: полная настройка завершена"
        )
    elif phase == "provision-existing":
        console.ok(
            f"{context.vmid} {context.name}: существующий гость "
            "настроен через provision.yaml без владения OpenTofu state"
        )
    else:
        _show_guest_summary(repo_root, context)
    return 0
