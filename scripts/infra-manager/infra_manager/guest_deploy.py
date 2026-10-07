"""Развёртывание одной гостевой VM через OpenTofu и Ansible."""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path

from .access import load_access_policy
from .common import InfraManagerError, console, require_command, run
from .guest_catalog import find_guest_by_role, guest_identity
from .guest_deploy_infrastructure import (
    DeploymentContext,
    DeploymentPaths,
    GuestPlan,
    _apply_plan,
    _build_deployment_context,
    _build_guest_plan,
    _find_guest_directory,
    _ensure_ssh_host_key,
    _prepare_self_update_workspace,
    _validate_pve_and_state,
)
from .opentofu import prepare_workspace
from .pve_host import (
    apply_host_requirements,
    install_openbao_host_support,
    install_recovery_host_support,
    issue_openbao_machine_credentials,
    read_openbao_tls_ca,
    sign_ssh_client_key,
    sign_ssh_host_key,
)
from .settings import PATHS, SETTINGS


def _bootstrap_identity_path(vmid: int) -> Path:
    """Путь одноразовой SSH-идентичности незавершённого нового гостя."""

    return PATHS.opentofu_dir / "bootstrap-ssh" / str(vmid) / "id_ed25519"


def _bootstrap_public_key(private_key: Path, vmid: int) -> str:
    public_key = Path(str(private_key) + ".pub")
    if not private_key.is_file() or not public_key.is_file():
        raise InfraManagerError(
            f"Гость {vmid}: одноразовый bootstrap SSH-ключ неполон"
        )
    value = public_key.read_text(encoding="utf-8").strip()
    parts = value.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise InfraManagerError(
            f"Гость {vmid}: одноразовый bootstrap SSH-ключ некорректен"
        )
    return value


def _ensure_bootstrap_identity(vmid: int) -> Path:
    """Создать или продолжить одноразовую identity первого входа гостя."""

    private_key = _bootstrap_identity_path(vmid)
    public_key = Path(str(private_key) + ".pub")
    if private_key.exists() != public_key.exists():
        raise InfraManagerError(
            f"Гость {vmid}: найден неполный одноразовый bootstrap SSH-ключ"
        )
    if not private_key.exists():
        private_key.parent.mkdir(parents=True, exist_ok=True)
        private_key.parent.chmod(0o700)
        run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"infra-manager-bootstrap-{vmid}",
                "-f",
                str(private_key),
            ]
        )
    private_key.chmod(0o600)
    public_key.chmod(0o644)
    _bootstrap_public_key(private_key, vmid)
    return private_key


def _existing_bootstrap_identity(vmid: int) -> Path | None:
    private_key = _bootstrap_identity_path(vmid)
    public_key = Path(str(private_key) + ".pub")
    if not private_key.exists() and not public_key.exists():
        return None
    _bootstrap_public_key(private_key, vmid)
    return private_key


def _remove_bootstrap_identity(vmid: int) -> None:
    """Удалить локальную одноразовую identity после перехода на SSH CA."""

    private_key = _bootstrap_identity_path(vmid)
    directory = private_key.parent
    public_key = Path(str(private_key) + ".pub")
    private_key.unlink(missing_ok=True)
    public_key.unlink(missing_ok=True)
    if directory.is_dir():
        unknown = list(directory.iterdir())
        if unknown:
            raise InfraManagerError(
                f"Гость {vmid}: bootstrap-каталог содержит неизвестные файлы"
            )
        directory.rmdir()


def _remove_obsolete_guest_authorizations(
    context: DeploymentContext,
    private_key: Path,
    certificate: Path,
) -> None:
    """Удалить bootstrap и старый постоянный guest key через сертификат."""

    script = r"""
from pathlib import Path
import os
import sys

marker = sys.argv[1]
target = Path("/root/.ssh/authorized_keys")
if not target.exists():
    raise SystemExit(0)

begin = "# BEGIN infra-manager ansible self"
end = "# END infra-manager ansible self"
result = []
inside = False
for line in target.read_text(encoding="utf-8").splitlines():
    if line == begin:
        inside = True
        continue
    if line == end:
        inside = False
        continue
    if inside:
        continue
    if " infra-manager ansible guest" in line:
        continue
    if marker in line:
        continue
    result.append(line)

target.write_text(
    ("\n".join(result) + "\n") if result else "",
    encoding="utf-8",
)
os.chmod(target, 0o600)
"""
    command = shlex.join(
        [
            "python3",
            "-c",
            script,
            f"infra-manager-bootstrap-{context.vmid}",
        ]
    )
    run(
        [
            *_ssh_identity_args(
                context,
                private_key,
                certificate=certificate,
            ),
            command,
        ]
    )


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

    repo_root = context.paths.guest_dir.parents[2]
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_CONFIG"] = str(repo_root / "ansible.cfg")
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
        cwd=repo_root,
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
    identity = guest_identity(repo_root, context.vmid)
    allowed = (
        identity.role != SETTINGS.infra_manager_role
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


def _guest_has_openbao_machine_identity(
    context: DeploymentContext,
    private_key: Path,
    *,
    certificate: Path | None,
    target: str,
) -> bool:
    """Проверить действующую машинную identity, не читая secret."""
    result = run(
        [
            *_ssh_identity_args(
                context,
                private_key,
                certificate=certificate,
            ),
            "test -s /etc/infra-manager/openbao/machine.env "
            "&& test -x /usr/local/sbin/infra-openbao-otp "
            f"&& /usr/local/sbin/infra-openbao-otp {target} >/dev/null",
        ],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _prepare_openbao_machine_ansible_vars(
    context: DeploymentContext,
    *,
    private_key: Path,
    certificate: Path | None,
    directory: Path,
) -> list[str]:
    """Подготовить только собственную машинную OpenBao identity гостя."""
    repo_root = context.paths.guest_dir.parents[2]
    policy = load_access_policy(repo_root)
    enabled = (
        context.vmid in policy.machine_identity_vmids
        and bool(policy.targets_for(context.vmid))
    )
    allowed_sources = policy.sources_for(context.vmid)
    target_enabled = bool(allowed_sources)
    args = [
        "-e",
        f"infra_openbao_machine_enabled={'true' if enabled else 'false'}",
        "-e",
        (
            "infra_openbao_otp_target_enabled="
            f"{'true' if target_enabled else 'false'}"
        ),
    ]

    if not enabled and not target_enabled:
        return args

    ca_file = PATHS.openbao_tls_ca
    if not ca_file.is_file() or ca_file.stat().st_size == 0:
        ca_file = directory / "openbao-ca.crt"
        ca_file.write_text(read_openbao_tls_ca(context.node), encoding="utf-8")
        ca_file.chmod(0o600)
    args.extend(["-e", f"infra_openbao_ca_file={ca_file}"])

    infra_manager = find_guest_by_role(
        repo_root,
        SETTINGS.infra_manager_role,
    )
    manager = policy.guests.get(infra_manager.vmid)
    if manager is None or manager.address is None:
        raise InfraManagerError(
            "Не определён доверенный адрес infra-manager для машинного OpenBao"
        )
    args.extend(
        [
            "-e",
            f"infra_openbao_addr=https://{manager.address}:8202",
        ]
    )

    if target_enabled:
        target = policy.guests.get(context.vmid)
        if target is None or target.address is None:
            raise InfraManagerError(
                f"guest:{context.vmid}: OTP-цель не имеет доверенного статического адреса"
            )
        args.extend(
            [
                "-e",
                f"infra_openbao_otp_target_ip={target.address}",
                "-e",
                (
                    "infra_openbao_otp_allowed_roles="
                    + ",".join(f"guest-{vmid}" for vmid in allowed_sources)
                ),
            ]
        )

    if not enabled:
        return args

    target_ips: list[str] = []
    for target_vmid in policy.targets_for(context.vmid):
        target = policy.guests.get(target_vmid)
        if target is None or target.address is None:
            raise InfraManagerError(
                f"guest:{target_vmid}: SSH OTP-цель не имеет доверенного статического адреса"
            )
        target_ips.append(target.address)

    host_ca_file = PATHS.ssh_host_ca_public_key
    if not host_ca_file.is_file() or host_ca_file.stat().st_size == 0:
        raise InfraManagerError(
            f"Не найден SSH host CA для OTP-клиента: {host_ca_file}"
        )
    args.extend(
        [
            "-e",
            f"infra_openbao_ssh_host_ca_file={host_ca_file}",
            "-e",
            "infra_openbao_otp_target_ips=" + ",".join(sorted(set(target_ips))),
        ]
    )

    if _guest_has_openbao_machine_identity(
        context,
        private_key,
        certificate=certificate,
        target=sorted(set(target_ips))[0],
    ):
        return args

    credentials = issue_openbao_machine_credentials(
        context.node,
        context.vmid,
    )
    role_id = credentials["role_id"]
    secret_id = credentials["secret_id"]
    if any(character.isspace() for character in role_id + secret_id):
        raise InfraManagerError(
            "OpenBao вернул машинную identity с недопустимыми пробелами"
        )

    env_file = directory / "openbao-machine.env"
    env_file.write_text(
        "\n".join(
            (
                f"OPENBAO_ADDR=https://{manager.address}:8202",
                f"OPENBAO_ROLE_ID={role_id}",
                f"OPENBAO_SECRET_ID={secret_id}",
                "OPENBAO_CA=/etc/infra-manager/openbao/ca.crt",
                f"OPENBAO_SSH_OTP_ROLE=guest-{context.vmid}",
                "",
            )
        ),
        encoding="utf-8",
    )
    env_file.chmod(0o600)
    args.extend(
        [
            "-e",
            f"infra_openbao_machine_env_file={env_file}",
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
    openbao_machine_args: list[str] | None = None,
) -> None:
    repo_root = context.paths.guest_dir.parents[2]
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_CONFIG"] = str(repo_root / "ansible.cfg")
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
                openbao_machine_args
                if openbao_machine_args is not None
                else [
                    "-e",
                    "infra_openbao_machine_enabled=false",
                ]
            ),
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
        cwd=repo_root,
    )


def _configure_guest_os(
    context: DeploymentContext,
    *,
    provision_phase: str = "full",
    self_update: bool = False,
    project_branch: str | None = None,
    allow_legacy_bootstrap: bool = True,
    bootstrap_private_key: Path | None = None,
) -> None:
    """Проверить SSH и применить конфигурацию Ansible."""

    _ensure_ssh_host_key(context.paths.known_hosts, context.address)
    console.info(f"Настройка ОС {context.vmid}")

    repo_root = context.paths.guest_dir.parents[2]
    console.detail("Обновление PVE-only служебного контура")
    install_openbao_host_support(context.node, repo_root)
    if self_update:
        install_recovery_host_support(context.node, repo_root)

    if not PATHS.ssh_client_ca_public_key.is_file():
        if not allow_legacy_bootstrap:
            raise InfraManagerError(
                f"Гость {context.vmid}: SSH CA не опубликован; "
                "самообновление без краткоживущего сертификата запрещено"
            )
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
        certificate_ready = _verify_temporary_ssh_identity(
            context,
            private_key,
            certificate,
        )
        if not certificate_ready and bootstrap_private_key is not None:
            console.info(
                f"Гость {context.vmid}: первоначальная установка доверия к SSH CA"
            )
            _run_guest_ansible(
                context,
                private_key=bootstrap_private_key,
                certificate=None,
                provision_phase="base",
                self_update=self_update,
                project_branch=project_branch,
            )
            certificate_ready = _verify_temporary_ssh_identity(
                context,
                private_key,
                certificate,
            )
            if not certificate_ready:
                raise InfraManagerError(
                    f"Гость {context.vmid}: после bootstrap не принимает "
                    "краткоживущий SSH-сертификат"
                )

        if certificate_ready:
            host_certificate = _host_certificate_if_available(
                context,
                private_key,
                client_certificate=certificate,
                directory=Path(temporary_dir),
                provision_phase=provision_phase,
            )
            openbao_machine_args = (
                _prepare_openbao_machine_ansible_vars(
                    context,
                    private_key=private_key,
                    certificate=certificate,
                    directory=Path(temporary_dir),
                )
                if provision_phase == "full"
                else ["-e", "infra_openbao_machine_enabled=false"]
            )
            _run_guest_ansible(
                context,
                private_key=private_key,
                certificate=certificate,
                provision_phase=provision_phase,
                self_update=self_update,
                project_branch=project_branch,
                host_certificate=host_certificate,
                openbao_machine_args=openbao_machine_args,
            )
            _remove_obsolete_guest_authorizations(
                context,
                private_key,
                certificate,
            )
            if (
                bootstrap_private_key is not None
                and bootstrap_private_key == _bootstrap_identity_path(context.vmid)
            ):
                _remove_bootstrap_identity(context.vmid)
            return

        if _guest_trusts_client_ca(context):
            raise InfraManagerError(
                f"Гость {context.vmid} уже доверяет SSH CA, "
                "но вход по временному сертификату не прошёл"
            )
        if not allow_legacy_bootstrap:
            raise InfraManagerError(
                f"Гость {context.vmid}: вход по краткоживущему "
                "SSH-сертификату не прошёл; постоянный ключ для "
                "самообновления запрещён"
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
        openbao_machine_args = (
            _prepare_openbao_machine_ansible_vars(
                context,
                private_key=context.paths.private_key,
                certificate=None,
                directory=Path(temporary_dir),
            )
            if provision_phase == "full"
            else ["-e", "infra_openbao_machine_enabled=false"]
        )
        _run_guest_ansible(
            context,
            private_key=context.paths.private_key,
            certificate=None,
            provision_phase=provision_phase,
            self_update=self_update,
            project_branch=project_branch,
            host_certificate=host_certificate,
            openbao_machine_args=openbao_machine_args,
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


def project_revision(repo_root: Path) -> str:
    """Вернуть короткий хэш выполняемой рабочей копии проекта."""
    result = run(
        ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
    )
    if result.returncode:
        return "не определена"
    return result.stdout.strip() or "не определена"


def project_branch_for_checkout(repo_root: Path) -> str:
    """Вернуть фактическую ветку содержимого рабочей копии проекта."""

    current_result = run(
        ["git", "-C", str(repo_root), "branch", "--show-current"],
        check=False,
        capture_output=True,
    )
    current = (
        current_result.stdout.strip()
        if not current_result.returncode
        else ""
    )

    remote_result = run(
        [
            "git",
            "-C",
            str(repo_root),
            "for-each-ref",
            "--format=%(refname:short)",
            "--points-at",
            "HEAD",
            "refs/remotes/origin/",
        ],
        check=False,
        capture_output=True,
    )
    remote_branches: list[str] = []
    if not remote_result.returncode:
        for raw in remote_result.stdout.splitlines():
            ref = raw.strip()
            if not ref.startswith("origin/") or ref == "origin/HEAD":
                continue
            remote_branches.append(ref.removeprefix("origin/"))

    configured = SETTINGS.project_branch()
    if configured in remote_branches:
        return configured
    if current in remote_branches:
        return current
    if len(remote_branches) == 1:
        return remote_branches[0]
    if len(remote_branches) > 1:
        raise InfraManagerError(
            "Не удалось однозначно определить Git-ветку текущего задания: "
            + ", ".join(sorted(remote_branches))
        )
    if current:
        return current
    return configured


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
    identity = guest_identity(repo_root, vmid)
    is_infra_manager = identity.role == SETTINGS.infra_manager_role

    if bootstrap_scope and not is_infra_manager:
        raise InfraManagerError(
            f"Гость {vmid} не имеет роль {SETTINGS.infra_manager_role!r} "
            "и не принадлежит начальному контуру"
        )
    if phase == "provision-existing" and not bootstrap_scope:
        raise InfraManagerError(
            "provision-existing разрешён только начальному контуру"
        )

    self_update = not bootstrap_scope and is_infra_manager
    if self_update and phase != "all":
        raise InfraManagerError(
            f"Для {identity.name} из постоянного контура "
            "разрешено только полное обновление"
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
    project_branch = project_branch_for_checkout(repo_root)

    workspace = (
        _prepare_self_update_workspace(repo_root, vmid)
        if self_update
        else prepare_workspace(
            repo_root,
            only_vmids={vmid} if bootstrap_scope else None,
            exclude_vmids=set() if bootstrap_scope else None,
        )
    )
    context = _build_deployment_context(
        repo_root,
        vmid,
        workspace,
        private_key=None if self_update else PATHS.ansible_private_key,
    )
    bootstrap_private_key: Path | None = None

    if self_update:
        console.info(
            f"Проверка существующего {context.vmid} {context.name} "
            "без собственного OpenTofu state"
        )
        _validate_existing_guest_object(context)
        _configure_guest_os(
            context,
            provision_phase="full",
            self_update=True,
            project_branch=project_branch,
            allow_legacy_bootstrap=False,
        )
        console.result(
            f"{context.vmid} {context.name} обновлён через Ansible; "
            "активация новой управляющей среды назначена "
            "после завершения текущей операции"
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

    if not bootstrap_scope:
        if not state_present and phase in {"all", "infrastructure"}:
            bootstrap_private_key = _ensure_bootstrap_identity(context.vmid)
        else:
            bootstrap_private_key = _existing_bootstrap_identity(context.vmid)
        if bootstrap_private_key is not None:
            context.workspace.env["TF_VAR_bootstrap_ssh_public_key"] = (
                _bootstrap_public_key(
                    bootstrap_private_key,
                    context.vmid,
                )
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
    # применяются здесь одинаково для первоначального infra-manager
    # и обычных гостей.
    apply_host_requirements(
        node=context.node,
        vmid=context.vmid,
        kind=context.kind,
        features=context.features,
    )

    if phase == "all":
        _configure_guest_os(
            context,
            project_branch=project_branch,
            bootstrap_private_key=bootstrap_private_key,
        )
    elif phase == "provision-base":
        _configure_guest_os(
            context,
            provision_phase="base",
            project_branch=project_branch,
            bootstrap_private_key=bootstrap_private_key,
        )
    elif phase in {"provision", "provision-existing"}:
        _configure_guest_os(
            context,
            provision_phase="full",
            project_branch=project_branch,
            bootstrap_private_key=bootstrap_private_key,
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
        console.result(
            f"{context.vmid} {context.name}: полное развёртывание завершено"
        )
    return 0
