"""Инфраструктурный этап развёртывания гостя через OpenTofu/PVE."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from .common import InfraManagerError, console, run
from .opentofu import OpenTofuWorkspace
from .pve import PveClient
from .settings import PATHS

@dataclass(frozen=True)
class DeploymentPaths:
    """Пути, используемые на нескольких этапах развёртывания VM."""

    guest_dir: Path
    private_key: Path | None
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
            raise InfraManagerError(
                f"Развёртывание гостя {context.vmid} {context.name} остановлено: "
                "в сохранённом состоянии OpenTofu осталась отметка о незавершённом "
                "или повреждённом состоянии гостя (tainted). "
                "Гость может содержать нужные данные, поэтому автоматическое "
                "удаление запрещено. Проверьте гостя в PVE: если он исправен, "
                "вручную снимите отметку tainted; если создание не завершено, "
                "отдельно подтвердите удаление и повторное создание."
            )
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
    if "create" in actions:
        bootstrap_key = context.workspace.env.get(
            "TF_VAR_bootstrap_ssh_public_key",
            "",
        ).strip()
        if not bootstrap_key:
            raise InfraManagerError(
                f"Гость {context.vmid}: создание без одноразового "
                "bootstrap SSH-ключа запрещено"
            )

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


def _prepare_self_update_workspace(
    repo_root: Path,
    vmid: int,
) -> OpenTofuWorkspace:
    """Собрать описание специального гостя без доступа к OpenTofu state."""
    renderer = (
        repo_root
        / "scripts"
        / "guests"
        / "render-opentofu-input.py"
    )
    if not renderer.is_file():
        raise InfraManagerError(f"Не найден генератор состояния: {renderer}")

    result = run(
        [
            sys.executable,
            str(renderer),
            "--only-vmid",
            str(vmid),
        ],
        capture_output=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            f"Не удалось собрать описание гостя {vmid}"
        ) from exc
    guests = payload.get("guests") if isinstance(payload, dict) else None
    if not isinstance(guests, dict) or str(vmid) not in guests:
        raise InfraManagerError(
            f"VMID {vmid} отсутствует в описании для самообновления"
        )

    return OpenTofuWorkspace(
        directory=repo_root / "automation" / "opentofu",
        state_dir=PATHS.opentofu_state_dir,
        env=os.environ.copy(),
        payload=payload,
    )


def _build_deployment_context(
    repo_root: Path,
    vmid: int,
    workspace: OpenTofuWorkspace,
    *,
    private_key: Path | None = None,
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

    if private_key is not None and not private_key.is_file():
        raise InfraManagerError(
            f"Не найден закрытый SSH-ключ: {private_key}"
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
