"""Развёртывание одной гостевой VM через OpenTofu и Ansible."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .access import load_access_policy
from .common import InfraManagerError, console, require_command, run
from .opentofu import OpenTofuWorkspace, prepare_workspace
from .pve import PveClient
from .pve_host import (
    apply_host_requirements,
    ensure_infra_self_access,
    sign_ssh_client_key,
    sign_ssh_host_key,
)
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


def _create_temporary_client_certificate(
    context: DeploymentContext,
    directory: Path,
) -> tuple[Path, Path]:
    """Создать одноразовый SSH-ключ и получить сертификат OpenBao."""

    private_key = directory / "ansible_ed25519"
    public_key = directory / "ansible_ed25519.pub"
    certificate = directory / "ansible_ed25519-cert.pub"

    run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"infra-manager-ansible-{context.vmid}",
            "-f",
            str(private_key),
        ]
    )
    if not private_key.is_file() or not public_key.is_file():
        raise InfraManagerError(
            "ssh-keygen не создал временную пару ключей Ansible"
        )
    private_key.chmod(0o600)

    signed = sign_ssh_client_key(
        context.node,
        public_key.read_text(encoding="utf-8").strip(),
    )
    certificate.write_text(signed + "\n", encoding="utf-8")
    certificate.chmod(0o644)

    inspected = run(
        ["ssh-keygen", "-L", "-f", str(certificate)],
        check=False,
        capture_output=True,
    )
    if inspected.returncode:
        raise InfraManagerError(
            "OpenSSH не принял временный сертификат OpenBao"
        )
    return private_key, certificate


def _certificate_auth_ready(
    context: DeploymentContext,
    private_key: Path,
    certificate: Path,
) -> bool:
    """Проверить реальный вход root по временному SSH-сертификату."""

    result = run(
        [
            "ssh",
            "-i",
            str(private_key),
            "-o",
            f"CertificateFile={certificate}",
            "-o",
            f"UserKnownHostsFile={context.paths.known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            f"root@{context.address}",
            "true",
        ],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _run_ansible_guest(
    context: DeploymentContext,
    *,
    private_key: Path,
    certificate: Path | None,
    provision_phase: str,
    self_update: bool,
    project_branch: str | None,
) -> None:
    """Запустить общий Ansible playbook выбранным SSH-доступом."""

    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    ssh_args = [
        f"-o UserKnownHostsFile={context.paths.known_hosts}",
        "-o StrictHostKeyChecking=yes",
        "-o IdentitiesOnly=yes",
    ]
    if certificate is not None:
        ssh_args.append(f"-o CertificateFile={certificate}")
    ansible_env["ANSIBLE_SSH_ARGS"] = " ".join(ssh_args)

    run(
        [
            "ansible-playbook",
            "-i",
            f"{context.address},",
            "-u",
            "root",
            "--private-key",
            str(private_key),
            "-e",
            f"guest={context.paths.guest_dir.name}",
            "-e",
            f"provision_phase={provision_phase}",
            "-e",
            f"infra_self_update={'true' if self_update else 'false'}",
            "-e",
            f"infra_project_branch={project_branch or SETTINGS.project_branch()}",
            "-e",
            f"infra_pve_node={context.node}",
            str(context.paths.playbook),
        ],
        env=ansible_env,
    )


def _ssh_identity_args(
    context: DeploymentContext,
    private_key: Path,
    *,
    certificate: Path | None = None,
) -> list[str]:
    args = [
        "ssh",
        "-i",
        str(private_key),
        "-o",
        f"UserKnownHostsFile={context.paths.known_hosts}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if certificate is not None:
        args.extend(["-o", f"CertificateFile={certificate}"])
    args.append(f"root@{context.address}")
    return args


def _guest_trusts_client_ca(context: DeploymentContext) -> bool:
    ca_path = PATHS.ssh_client_ca_public_key
    if not ca_path.is_file():
        return False
    expected = ca_path.read_text(encoding="utf-8").strip()
    if not expected:
        return False

    result = run(
        [
            *_ssh_identity_args(context, context.paths.private_key),
            "cat /etc/ssh/trusted-user-ca-keys.pem 2>/dev/null || true",
        ],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip() == expected


def _create_temporary_ssh_identity(
    context: DeploymentContext,
    directory: Path,
) -> tuple[Path, Path]:
    private_key = directory / "ansible_ed25519"
    run(
        [
            "ssh-keygen",
            "-q",
            "-t",
            "ed25519",
            "-N",
            "",
            "-C",
            f"infra-manager-ansible-{context.vmid}",
            "-f",
            str(private_key),
        ]
    )
    public_key = private_key.with_suffix(".pub").read_text(
        encoding="utf-8"
    ).strip()
    certificate = sign_ssh_client_key(context.node, public_key)
    certificate_path = Path(str(private_key) + "-cert.pub")
    certificate_path.write_text(certificate + "\n", encoding="utf-8")
    certificate_path.chmod(0o600)

    run(["ssh-keygen", "-L", "-f", str(certificate_path)])
    return private_key, certificate_path


def _create_temporary_host_certificate(
    context: DeploymentContext,
    private_key: Path,
    *,
    client_certificate: Path | None,
    directory: Path,
) -> Path:
    """Подписать реальный Ed25519 host key гостя через OpenBao."""

    result = run(
        [
            *_ssh_identity_args(
                context,
                private_key,
                certificate=client_certificate,
            ),
            "cat /etc/ssh/ssh_host_ed25519_key.pub",
        ],
        capture_output=True,
    )
    public_key = result.stdout.strip()
    if not public_key.startswith("ssh-ed25519 "):
        raise InfraManagerError(
            f"Гость {context.vmid} не вернул корректный Ed25519 host key"
        )

    certificate = sign_ssh_host_key(
        context.node,
        public_key,
        vmid=context.vmid,
        hostname=context.name,
        address=context.address,
    )
    certificate_path = directory / "ssh_host_ed25519_key-cert.pub"
    certificate_path.write_text(certificate + "\n", encoding="utf-8")
    certificate_path.chmod(0o600)

    inspected = run(
        ["ssh-keygen", "-L", "-f", str(certificate_path)],
        capture_output=True,
    )
    if (
        "host certificate" not in inspected.stdout
        or context.name not in inspected.stdout
        or context.address not in inspected.stdout
    ):
        raise InfraManagerError(
            f"Host-сертификат гостя {context.vmid} не прошёл проверку"
        )
    console.ok(
        f"Гость {context.vmid}: SSH host key подписан OpenBao"
    )
    return certificate_path


def _host_certificate_if_available(
    context: DeploymentContext,
    private_key: Path,
    *,
    client_certificate: Path | None,
    directory: Path,
    provision_phase: str,
) -> Path | None:
    if provision_phase == "base":
        return None
    host_ca = getattr(PATHS, "ssh_host_ca_public_key", None)
    if not isinstance(host_ca, Path) or not host_ca.is_file():
        return None
    return _create_temporary_host_certificate(
        context,
        private_key,
        client_certificate=client_certificate,
        directory=directory,
    )


def _verify_temporary_ssh_identity(
    context: DeploymentContext,
    private_key: Path,
    certificate: Path,
) -> bool:
    result = run(
        [
            *_ssh_identity_args(
                context,
                private_key,
                certificate=certificate,
            ),
            "true",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        return False
    console.ok(
        f"Гость {context.vmid}: вход по временному SSH-сертификату подтверждён"
    )
    return True


def _project_git_ansible_vars(
    context: DeploymentContext,
) -> list[str]:
    """Собрать Ansible vars для Git read-доступа из access.yaml."""

    repo_root = context.paths.guest_dir.parents[2]
    policy = load_access_policy(repo_root)
    allowed = (
        context.vmid != 910
        and policy.project_repository_read_allowed(context.vmid)
    )
    args = [
        "-e",
        f"infra_project_git_read={'true' if allowed else 'false'}",
    ]
    if not allowed:
        return args

    private_key = PATHS.github_key
    if not private_key.is_file() or private_key.stat().st_size == 0:
        raise InfraManagerError(
            "Для Git read-доступа гостя не материализован "
            f"OpenBao credential: {private_key}"
        )
    args.extend(
        [
            "-e",
            f"infra_project_git_private_key_file={private_key}",
        ]
    )
    return args


def _run_guest_ansible(
    context: DeploymentContext,
    *,
    private_key: Path,
    certificate: Path | None,
    provision_phase: str,
    self_update: bool,
    project_branch: str | None,
    host_certificate: Path | None = None,
) -> None:
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    ssh_args = (
        f"-o UserKnownHostsFile={context.paths.known_hosts} "
        "-o StrictHostKeyChecking=yes "
        "-o IdentitiesOnly=yes"
    )
    if certificate is not None:
        ssh_args += f" -o CertificateFile={certificate}"
    ansible_env["ANSIBLE_SSH_ARGS"] = ssh_args

    run(
        [
            "ansible-playbook",
            "-i",
            f"{context.address},",
            "-u",
            "root",
            "--private-key",
            str(private_key),
            "-e",
            f"guest={context.paths.guest_dir.name}",
            "-e",
            f"provision_phase={provision_phase}",
            "-e",
            f"infra_self_update={'true' if self_update else 'false'}",
            "-e",
            f"infra_project_branch={project_branch or SETTINGS.project_branch()}",
            "-e",
            f"infra_pve_node={context.node}",
            *_project_git_ansible_vars(context),
            *(
                [
                    "-e",
                    f"infra_ssh_host_certificate_file={host_certificate}",
                ]
                if host_certificate is not None
                else []
            ),
            str(context.paths.playbook),
        ],
        env=ansible_env,
    )


def _configure_guest_os(
    context: DeploymentContext,
    *,
    provision_phase: str = "full",
    self_update: bool = False,
    project_branch: str | None = None,
) -> None:
    """Проверить SSH и применить конфигурацию Ansible."""

    _ensure_ssh_host_key(context.paths.known_hosts, context.address)
    console.info(f"Настройка ОС {context.vmid}")

    if not PATHS.ssh_client_ca_public_key.is_file():
        console.info(
            f"Гость {context.vmid}: SSH CA ещё не опубликован; "
            "используется переходный постоянный ключ Ansible"
        )
        _run_guest_ansible(
            context,
            private_key=context.paths.private_key,
            certificate=None,
            provision_phase=provision_phase,
            self_update=self_update,
            project_branch=project_branch,
        )
        return

    with tempfile.TemporaryDirectory(
        prefix=f"infra-manager-ssh-{context.vmid}-"
    ) as temporary_dir:
        private_key, certificate = _create_temporary_ssh_identity(
            context,
            Path(temporary_dir),
        )
        if _verify_temporary_ssh_identity(
            context,
            private_key,
            certificate,
        ):
            host_certificate = _host_certificate_if_available(
                context,
                private_key,
                client_certificate=certificate,
                directory=Path(temporary_dir),
                provision_phase=provision_phase,
            )
            _run_guest_ansible(
                context,
                private_key=private_key,
                certificate=certificate,
                provision_phase=provision_phase,
                self_update=self_update,
                project_branch=project_branch,
                host_certificate=host_certificate,
            )
            return

        if _guest_trusts_client_ca(context):
            raise InfraManagerError(
                f"Гость {context.vmid} уже доверяет SSH CA, "
                "но вход по временному сертификату не прошёл"
            )

        console.info(
            f"Гость {context.vmid}: доверие к SSH CA ещё не установлено; "
            "используется переходный постоянный ключ Ansible"
        )
        host_certificate = _host_certificate_if_available(
            context,
            context.paths.private_key,
            client_certificate=None,
            directory=Path(temporary_dir),
            provision_phase=provision_phase,
        )
        _run_guest_ansible(
            context,
            private_key=context.paths.private_key,
            certificate=None,
            provision_phase=provision_phase,
            self_update=self_update,
            project_branch=project_branch,
            host_certificate=host_certificate,
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


def _project_branch(repo_root: Path) -> str:
    """Вернуть фактическую ветку рабочей копии проекта."""
    result = run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=False,
        capture_output=True,
    )
    if not result.returncode and result.stdout.strip():
        return result.stdout.strip()
    return SETTINGS.project_branch()


def _show_guest_summary(
    repo_root: Path,
    context: DeploymentContext,
) -> None:
    """Показать итог полного развёртывания обычного гостя."""
    separator = "=" * 60
    kind = "VM" if context.kind == "vm" else "LXC"
    branch = _project_branch(repo_root)
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
    print(f"  Ветка:   {branch}")
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

    self_update = (
        not bootstrap_scope
        and vmid in SETTINGS.bootstrap_managed_vmids
    )
    if self_update and phase != "all":
        raise InfraManagerError(
            "Для 910 из постоянного контура разрешено только полное обновление"
        )

    commands = [
        "ansible-playbook",
        "ssh-keygen",
        "ssh-keyscan",
        "ssh",
    ]
    if not self_update:
        commands.insert(0, "tofu")
    for command in commands:
        require_command(command)

    console.info("Проверка проекта")
    run([sys.executable, str(repo_root / "scripts" / "validate_repo.py")])
    project_branch = _project_branch(repo_root)

    workspace = (
        _prepare_self_update_workspace(repo_root, vmid)
        if self_update
        else prepare_workspace(
            repo_root,
            only_vmids={vmid} if bootstrap_scope else None,
            exclude_vmids=set() if bootstrap_scope else None,
        )
    )
    context = _build_deployment_context(repo_root, vmid, workspace)

    if self_update:
        console.info(
            "Проверка существующего 910 без собственного OpenTofu state"
        )
        _validate_existing_guest_object(context)
        if not PATHS.ansible_public_key.is_file():
            raise InfraManagerError(
                f"Не найден открытый ключ Ansible: {PATHS.ansible_public_key}"
            )
        ensure_infra_self_access(
            context.node,
            context.vmid,
            hostname=context.name,
            public_key=PATHS.ansible_public_key.read_text(
                encoding="utf-8"
            ).strip(),
        )
        _configure_guest_os(
            context,
            provision_phase="full",
            self_update=True,
            project_branch=project_branch,
        )
        console.result(
            "910 infra-manager обновлён через Ansible; "
            "активация новой управляющей среды назначена "
            "после завершения задания Semaphore"
        )
        return 0

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
        _configure_guest_os(context, project_branch=project_branch)
    elif phase == "provision-base":
        _configure_guest_os(
            context,
            provision_phase="base",
            project_branch=project_branch,
        )
    elif phase in {"provision", "provision-existing"}:
        _configure_guest_os(
            context,
            provision_phase="full",
            project_branch=project_branch,
        )

    if phase == "infrastructure":
        console.result(
            f"{context.vmid} {context.name}: основа создана"
        )
    elif phase == "provision-base":
        console.result(
            f"{context.vmid} {context.name}: базовая настройка завершена"
        )
    elif phase == "provision":
        console.result(
            f"{context.vmid} {context.name}: полная настройка завершена"
        )
    elif phase == "provision-existing":
        console.result(
            f"{context.vmid} {context.name}: существующий гость "
            "настроен через provision.yaml без владения OpenTofu state"
        )
    else:
        _show_guest_summary(repo_root, context)
    return 0
