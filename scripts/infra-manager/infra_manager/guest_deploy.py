"""Развёртывание гостя с проверкой доступа и состояния OpenTofu.

Перед изменением гостя выполняются проверки принадлежности VMID,
постоянных томов и одноразовых ключей первоначального доступа.
Выполнение OpenTofu и Ansible может оставить часть изменений применённой;
при сбое нельзя автоматически стирать данные существующего гостя.
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

from .access import load_access_policy
from .common import InfraManagerError, _ansible_failure_summary, console, require_command, run
from .guest_catalog import find_guest_by_role, guest_identity
from .guest_deploy_infrastructure import (
    DeploymentContext,
    DeploymentPaths,
    GuestPlan,
    _apply_plan,
    _backup_state_before_recreation,
    _build_deployment_context,
    _build_guest_plan,
    _find_guest_directory,
    _ensure_ssh_host_key,
    _forget_recreated_guest_host_key,
    _preflight_missing_guest_recreation,
    _prepare_self_update_workspace,
    _validate_pve_and_state,
)
from .opentofu import prepare_workspace
from .pve_host import (
    apply_host_requirements,
    ensure_guest_storage,
    ensure_pve_host_storage,
    install_openbao_host_support,
    trigger_openbao_unseal,
    install_recovery_host_support,
    install_backup_host_support,
    issue_openbao_machine_credentials,
    read_openbao_tls_ca,
    sign_ssh_client_key,
    sign_ssh_host_key,
)
from .settings import PATHS, SETTINGS
from .guest_ssh import GuestSshSession

BOOTSTRAP_INFRA_MANAGER_PHASES = {
    "create": "infrastructure",
    "configure-base": "provision-base",
    "configure-control-plane": "provision-control-plane",
    "configure-recovery-control-plane": "provision-recovery-control-plane",
    "configure-ssh-trust": "provision-ssh-trust",
    "configure": "provision",
}

# Каждая фаза развёртывания соответствует одному этапу Ansible.
# Для infrastructure настройка ОС не запускается.
ANSIBLE_PHASE_BY_DEPLOY_PHASE: dict[str, str | None] = {
    "all": "full",
    "infrastructure": None,
    "provision-base": "base",
    "provision-control-plane": "bootstrap",
    "provision-recovery-control-plane": "recovery",
    "provision-ssh-trust": "base",
    "provision": "full",
    "sync": "sync",
}


def _bootstrap_scope_private_key() -> Path:
    """Получить ключ только из закрытого начального контура 990."""

    raw = os.environ.get("INFRA_BOOTSTRAP_SSH_PRIVATE_KEY", "").strip()
    if not raw:
        raise InfraManagerError(
            "Начальный контур не передал INFRA_BOOTSTRAP_SSH_PRIVATE_KEY"
        )
    path = Path(raw)
    if not path.is_absolute() or not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(
            f"Некорректный bootstrap SSH-ключ начального контура: {path}"
        )
    return path


def _bootstrap_identity_path(vmid: int) -> Path:
    """Путь одноразовой SSH-идентичности незавершённого нового гостя."""

    return PATHS.opentofu_dir / "bootstrap-ssh" / str(vmid) / "id_ed25519"


def _bootstrap_public_key(private_key: Path, vmid: int) -> str:
    """Прочитать открытый ключ одноразовой пары нового гостя.

        Неполная пара или неверный тип ключа запрещают продолжать
        первоначальную установку, чтобы не использовать чужие полномочия."""
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
    """Проверить, сохранилась ли одноразовая пара после незавершённого запуска.

        Отсутствие обоих файлов допустимо; неполный или повреждённый набор
        вызывает отказ вместо незаметной генерации нового ключа."""
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
    session: GuestSshSession,
) -> None:
    """После успешного сертификатного входа убрать устаревшие разрешения."""
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
    command = shlex.join([
        "python3", "-c", script, f"infra-manager-bootstrap-{context.vmid}",
    ])
    run(session.ssh_command(command))


def _create_temporary_host_certificate(
    context: DeploymentContext,
    session: GuestSshSession,
    *,
    directory: Path,
) -> Path:
    """Подписать действующий ключ SSH-сервера через OpenBao и PVE."""
    result = run(
        session.ssh_command("cat /etc/ssh/ssh_host_ed25519_key.pub"),
        capture_output=True,
    )
    public_key = result.stdout.strip()
    if not public_key.startswith("ssh-ed25519 "):
        raise InfraManagerError(
            f"Гость {context.vmid} не вернул корректный Ed25519 host key"
        )

    certificate = sign_ssh_host_key(
        context.node, public_key,
        vmid=context.vmid, hostname=context.name, address=context.address,
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
    console.ok(f"Гость {context.vmid}: SSH host key подписан OpenBao")
    return certificate_path


def _host_certificate_if_available(
    context: DeploymentContext,
    session: GuestSshSession,
    *,
    directory: Path,
    provision_phase: str,
    bootstrap_transition: bool = False,
) -> Path | None:
    """Не обновлять сертификат на базовой фазе, кроме первого перехода на CA."""
    if provision_phase == "base" and not bootstrap_transition:
        return None
    host_ca = getattr(PATHS, "ssh_host_ca_public_key", None)
    if not isinstance(host_ca, Path) or not host_ca.is_file():
        if bootstrap_transition:
            raise InfraManagerError("Первый переход на SSH CA требует Host CA")
        return None
    return _create_temporary_host_certificate(
        context, session, directory=directory,
    )

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
    session: GuestSshSession,
    *,
    target: str,
) -> bool:
    """Проверить действующую машинную идентичность, не читая секрет."""
    result = run(
        session.ssh_command(
            "test -s /etc/infra-manager/openbao/machine.env "
            "&& test -x /usr/local/sbin/infra-openbao-otp "
            f"&& /usr/local/sbin/infra-openbao-otp {target} >/dev/null"
        ),
        check=False,
        capture_output=True,
    )
    return result.returncode == 0

def _prepare_openbao_machine_ansible_vars(
    context: DeploymentContext,
    *,
    session: GuestSshSession,
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
        context, session, target=sorted(set(target_ips))[0],
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
    session: GuestSshSession,
    provision_phase: str,
    self_update: bool,
    project_branch: str | None,
    host_certificate: Path | None = None,
    openbao_machine_args: list[str] | None = None,
) -> None:
    """Применить Ansible с тем же SSH-доверием, что и проверка подключения."""
    if session.private_key is None:
        raise InfraManagerError("SSH-сеанс Ansible не подготовлен")
    repo_root = context.paths.guest_dir.parents[2]
    ansible_env = os.environ.copy()
    ansible_env["ANSIBLE_CONFIG"] = str(repo_root / "ansible.cfg")
    ansible_env["ANSIBLE_HOST_KEY_CHECKING"] = "True"
    ansible_env["ANSIBLE_SSH_ARGS"] = session.ansible_ssh_args()

    started = time.monotonic()
    completed = False
    interrupted = False
    try:
        result = run(
            [
                "ansible-playbook",
                "-i", f"{context.address},",
                "-u", "root",
                "--private-key", str(session.private_key),
                "-e", f"guest={context.paths.guest_dir.name}",
                "-e", f"provision_phase={provision_phase}",
                "-e", f"infra_self_update={'true' if self_update else 'false'}",
                "-e", f"infra_project_branch={project_branch or SETTINGS.project_branch()}",
                "-e", f"infra_pve_node={context.node}",
                *(
                    ["-e", f"infra_ssh_client_ca_file={PATHS.ssh_client_ca_public_key}"]
                    if PATHS.ssh_client_ca_public_key.is_file() else []
                ),
                *(_project_git_ansible_vars(context) if provision_phase != "sync" else []),
                *(
                    openbao_machine_args
                    if openbao_machine_args is not None
                    else ["-e", "infra_openbao_machine_enabled=false"]
                ),
                *(
                    ["-e", f"infra_ssh_host_certificate_file={host_certificate}"]
                    if host_certificate is not None else []
                ),
                str(context.paths.playbook),
            ],
            env=ansible_env,
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            # Не публиковать сырой Ansible stdout/stderr: внутри могут
            # находиться рабочие секреты. Показывать только название задания.
            summary = _ansible_failure_summary(
                "\n".join((result.stdout or "", result.stderr or ""))
            )
            reason = f" {summary}" if summary else " Название задания определить не удалось."
            raise InfraManagerError(
                f"Ansible гостя {context.vmid} ({provision_phase}) завершился "
                f"с кодом {result.returncode}.{reason}"
            )
        completed = True
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        console.timing(
            f"Ansible гостя {context.vmid} ({provision_phase})",
            time.monotonic() - started,
            interrupted=interrupted,
            failed=not completed and not interrupted,
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
    """Подготовить доверенный SSH-сеанс и применить настройку Ansible.

    Для штатного обновления нужен и клиентский, и серверный CA OpenBao.
    При первом создании гость может ещё не иметь host-сертификата; только
    предъявленный одноразовый начальный ключ разрешает установить его.
    Отказ штатного сертификата сам по себе не разрешает такой переход.
    """
    console.info(f"Настройка ОС {context.vmid}")
    repo_root = context.paths.guest_dir.parents[2]
    if provision_phase != "sync":
        console.detail("Обновление PVE-only служебного контура")
        install_openbao_host_support(context.node, repo_root)
        if self_update:
            install_recovery_host_support(context.node, repo_root)
            # Код резервирования устанавливается атомарно
            # до отложенной замены Docker, без запуска таймера или дисков.
            install_backup_host_support(context.node, repo_root)
            # Если предыдущее самообновление остановилось после рестарта
            # OpenBao, Raft остаётся запечатанным. Безопасно восстановить
            # существующий OpenBao до первого запроса SSH-сертификата.
            console.info("Проверка и разблокировка OpenBao перед SSH-доступом")
            trigger_openbao_unseal(context.node)

    bootstrap_key = bootstrap_private_key
    if (
        bootstrap_key is None
        and allow_legacy_bootstrap
        and context.paths.private_key is not None
    ):
        bootstrap_key = context.paths.private_key

    if not PATHS.ssh_client_ca_public_key.is_file():
        if not allow_legacy_bootstrap or bootstrap_key is None:
            raise InfraManagerError(
                f"Гость {context.vmid}: клиентский SSH CA OpenBao отсутствует; "
                "начальный одноразовый доступ недоступен"
            )
        # До появления CA разрешён только одноразовый начальный ключ.
        _ensure_ssh_host_key(context.paths.known_hosts, context.address)
        console.info(f"Гость {context.vmid}: используется начальный SSH-доступ")
        with GuestSshSession(
            context.node, context.address,
            bootstrap_key=bootstrap_key,
            bootstrap_known_hosts=context.paths.known_hosts,
            execute=run,
        ) as initial_session:
            _run_guest_ansible(
                context, session=initial_session,
                provision_phase=provision_phase,
                self_update=self_update, project_branch=project_branch,
            )
        return

    with tempfile.TemporaryDirectory(
        prefix=f"infra-manager-ssh-{context.vmid}-"
    ) as temporary_dir:
        temporary = Path(temporary_dir)
        with GuestSshSession(
            context.node,
            context.address,
            host_ca=PATHS.ssh_host_ca_public_key,
            sign_client=sign_ssh_client_key,
            execute=run,
        ) as session:
            certificate_ready, reason = session.verify_connection()
            if not certificate_ready and bootstrap_key is not None and allow_legacy_bootstrap:
                # Не менять доверие рабочего гостя: переход разрешён только
                # при наличии одноразового ключа конкретного создания.
                _ensure_ssh_host_key(context.paths.known_hosts, context.address)
                console.info(
                    f"Гость {context.vmid}: первоначальная установка доверия SSH CA"
                )
                with GuestSshSession(
                    context.node, context.address,
                    bootstrap_key=bootstrap_key,
                    bootstrap_known_hosts=context.paths.known_hosts,
                    execute=run,
                ) as initial_session:
                    host_certificate = _host_certificate_if_available(
                        context, initial_session,
                        directory=temporary,
                        provision_phase="base",
                        bootstrap_transition=True,
                    )
                    _run_guest_ansible(
                        context, session=initial_session,
                        provision_phase="base",
                        self_update=self_update,
                        project_branch=project_branch,
                        host_certificate=host_certificate,
                    )
                certificate_ready, reason = session.verify_connection()

            if not certificate_ready:
                raise InfraManagerError(
                    f"Гость {context.vmid}: вход по краткоживущему SSH-сертификату "
                    f"не прошёл ({reason}); исправьте SSH CA/host-сертификат. "
                    "Для полного восстановления управляющего гостя есть "
                    "infra-manager recover"
                )

            console.ok(
                f"Гость {context.vmid}: вход по временному SSH-сертификату подтверждён"
            )
            host_certificate = (
                _host_certificate_if_available(
                    context, session,
                    directory=temporary,
                    provision_phase=provision_phase,
                )
                if provision_phase != "sync" else None
            )
            openbao_machine_args = (
                _prepare_openbao_machine_ansible_vars(
                    context, session=session, directory=temporary,
                )
                if provision_phase == "full"
                else ["-e", "infra_openbao_machine_enabled=false"]
            )
            _run_guest_ansible(
                context, session=session,
                provision_phase=provision_phase,
                self_update=self_update,
                project_branch=project_branch,
                host_certificate=host_certificate,
                openbao_machine_args=openbao_machine_args,
            )
            if provision_phase != "sync":
                _remove_obsolete_guest_authorizations(context, session)

def _validate_existing_guest_object(context: DeploymentContext) -> None:
    """Проверить существующий объект без требования OpenTofu state."""
    resource = context.client.find_vm(context.vmid)
    if resource is None:
        raise InfraManagerError(
            f"Гость {context.vmid} отсутствует в PVE"
        )
    actual_type = str(resource.get("type") or "")
    actual_name = str(resource.get("name") or "")
    expected_type = "qemu" if context.kind == "vm" else "lxc"
    if actual_type != expected_type or actual_name != context.name:
        raise InfraManagerError(
            f"VMID {context.vmid} занят объектом '{actual_name}' "
            f"типа '{actual_type}'; настройка запрещена"
        )


def _reconcile_guest_infrastructure(
    context: DeploymentContext,
    *,
    missing_recreation: bool = False,
) -> None:
    """Применить состояние одной VM и всегда удалить временный plan."""

    console.info(f"Проверка состояния гостя {context.vmid}")
    try:
        plan = _build_guest_plan(context)
        if missing_recreation:
            # OpenTofu refresh обновляет исчезнувший объект при plan/apply
            # без опасного ручного удаления записи из общего state.
            if plan.actions != ("create",):
                raise InfraManagerError(
                    f"Гость {context.vmid}: для пересоздания ожидался "
                    f"план только create; получено {plan.actions!r}"
                )
            # Проверка повторяется непосредственно перед применением:
            # другой процесс мог создать объект или том после первой проверки.
            _preflight_missing_guest_recreation(context)
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


def _run_deploy_guest(
    repo_root: Path,
    vmid: int,
    *,
    bootstrap_scope: bool,
    phase: str,
) -> int:
    """Выполнить развёртывание гостя с контролем роли и этапа.

    Обычный запуск и первоначальная настройка infra-manager имеют
    разные границы доступа. До изменения PVE проверяются VMID, роль
    гостя и допустимая фаза. OpenTofu и Ansible могут успеть выполнить
    часть действий до ошибки; автоматическое удаление гостя не делается.
    Повторная попытка должна опираться на фактическое состояние.
    """

    if vmid <= 0:
        raise InfraManagerError("VMID должен быть положительным числом")
    if phase not in ANSIBLE_PHASE_BY_DEPLOY_PHASE:
        raise InfraManagerError(f"Неизвестная фаза deploy-guest: {phase}")
    identity = guest_identity(repo_root, vmid)
    is_infra_manager = identity.role == SETTINGS.infra_manager_role

    if bootstrap_scope and not is_infra_manager:
        raise InfraManagerError(
            f"Гость {vmid} не имеет роль {SETTINGS.infra_manager_role!r} "
            "и не принадлежит начальному контуру"
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
    if not self_update and phase != "sync":
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
        private_key=_bootstrap_scope_private_key() if bootstrap_scope else None,
    )
    bootstrap_private_key: Path | None = None
    missing_recreation = False

    if self_update:
        console.info(
            f"Проверка существующего {context.vmid} {context.name} "
            "без собственного OpenTofu state"
        )
        try:
            _validate_existing_guest_object(context)
            # Сверка ВСЕХ общих LVM PVE живёт в deploy 910, а не в deploy
            # первого гостя-потребителя. На PVE никаких пакетов/служб.
            ensure_pve_host_storage(context.node, repo_root)
            _configure_guest_os(
                context,
                provision_phase="full",
                self_update=True,
                project_branch=project_branch,
                allow_legacy_bootstrap=False,
            )
        except InfraManagerError as exc:
            raise InfraManagerError(
                f"Штатный deploy {context.name} остановлен: {exc} "
                "Проверьте причину и состояние: infra-manager status "
                f"{context.vmid}. После исправления повторите deploy. "
                "Команда recover нужна только при подтверждённой "
                "невозможности штатно восстановить управляющий контур."
            ) from exc
        console.result(
            f"{context.vmid} {context.name} обновлён через Ansible; "
            "активация новой управляющей среды назначена "
            "после завершения текущей операции"
        )
        return 0

    bootstrap_configure = bootstrap_scope and phase in {
        "provision-base",
        "provision-control-plane",
        "provision-recovery-control-plane",
        "provision-ssh-trust",
        "provision",
    }

    if bootstrap_configure or phase == "sync":
        # Sync не изменяет состояние PVE и не требует OpenTofu state.
        # Начальный контур также проверяет уже созданный объект отдельно.
        _validate_existing_guest_object(context)
        state_present = False
    else:
        console.info("Инициализация OpenTofu")
        context.workspace.initialize()

        state_present, state_status = context.workspace.get_resource_state(
            context.target
        )
        missing_recreation = bool(
            not bootstrap_scope
            and phase == "all"
            and state_present
            and state_status == "ready"
            and identity.source.get("recreate_if_missing") is True
            and context.client.find_vm(context.vmid) is None
        )
        if missing_recreation:
            console.info(
                f"Гость {context.vmid} отсутствует: "
                "проверка хранилищ и резервирование OpenTofu state"
            )
            _preflight_missing_guest_recreation(context)
            _backup_state_before_recreation(context)
        _validate_pve_and_state(
            context,
            state_present=state_present,
            state_status=state_status,
            missing_recreation_allowed=missing_recreation,
        )

    if not bootstrap_scope and phase != "sync":
        if (not state_present or missing_recreation) and phase in {
            "all", "infrastructure"
        }:
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
        _reconcile_guest_infrastructure(
            context,
            missing_recreation=missing_recreation,
        )
        if missing_recreation:
            _forget_recreated_guest_host_key(
                context.paths.known_hosts,
                context.address,
            )

    # Некоторые свойства LXC Proxmox разрешает менять только самому root@pam,
    # а не API token. Они остаются частью общего guest/profile-контракта и
    # применяются здесь одинаково для первоначального infra-manager
    # и обычных гостей.
    if phase != "sync":
        apply_host_requirements(
            node=context.node,
            vmid=context.vmid,
            kind=context.kind,
            features=context.features,
        )
        ensure_guest_storage(
            context.node,
            context.vmid,
            context.kind,
            context.paths.guest_dir,
        )

    ansible_phase = ANSIBLE_PHASE_BY_DEPLOY_PHASE[phase]
    if ansible_phase is not None:
        _configure_guest_os(
            context,
            provision_phase=ansible_phase,
            project_branch=project_branch,
            allow_legacy_bootstrap=(
                phase != "sync"
                and (phase != "provision" or not bootstrap_scope)
            ),
            bootstrap_private_key=(
                None if phase == "provision" and bootstrap_scope
                else bootstrap_private_key
            ),
        )

    if bootstrap_private_key is not None and phase != "infrastructure":
        _remove_bootstrap_identity(context.vmid)

    if phase == "infrastructure":
        console.result(
            f"{context.vmid} {context.name}: основа создана"
        )
    elif phase == "provision-base":
        console.result(
            f"{context.vmid} {context.name}: базовая настройка завершена"
        )
    elif phase in {"provision-control-plane", "provision-recovery-control-plane"}:
        console.result(
            f"{context.vmid} {context.name}: управляющий контур запущен"
        )
    elif phase == "provision-ssh-trust":
        console.result(
            f"{context.vmid} {context.name}: доверие к SSH CA установлено"
        )
    elif phase == "provision":
        console.result(
            f"{context.vmid} {context.name}: полная настройка завершена"
        )
    elif phase == "sync":
        console.result(
            f"{context.vmid} {context.name}: безопасная синхронизация завершена"
        )
    else:
        console.result(
            f"{context.vmid} {context.name}: полное развёртывание завершено"
        )
    return 0


def run_deploy_guest(
    repo_root: Path,
    vmid: int,
    *,
    phase: str = "all",
) -> int:
    """Развернуть гостя, настроить ОС либо синхронизировать безопасные файлы.

    Фаза all меняет инфраструктуру; provision применяет полный Ansible.
    Фаза sync не запускает OpenTofu, установку программ или PVE-операции.
    Первоначальные полномочия 990 через эту точку входа не передаются.
    """

    if phase not in {"all", "provision", "sync"}:
        raise InfraManagerError(
            f"Штатный deploy гостя не поддерживает фазу {phase!r}"
        )
    return _run_deploy_guest(
        repo_root,
        vmid,
        bootstrap_scope=False,
        phase=phase,
    )


def run_bootstrap_infra_manager_step(
    repo_root: Path,
    vmid: int,
    *,
    step: str,
) -> int:
    """Выполнить внутренний линейный шаг bootstrap infra-manager."""

    identity = guest_identity(repo_root, vmid)
    if identity.role != SETTINGS.infra_manager_role:
        raise InfraManagerError(
            f"Гость {vmid} не имеет роль {SETTINGS.infra_manager_role!r}"
        )
    phase = BOOTSTRAP_INFRA_MANAGER_PHASES.get(step)
    if phase is None:
        raise InfraManagerError(
            f"Недопустимый bootstrap-шаг infra-manager: {step}"
        )
    return _run_deploy_guest(
        repo_root,
        vmid,
        bootstrap_scope=True,
        phase=phase,
    )
