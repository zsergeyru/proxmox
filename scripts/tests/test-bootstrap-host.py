#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts/bootstrap-runner/bootstrap-host.py"

spec = importlib.util.spec_from_file_location("bootstrap_host", MODULE_PATH)
if spec is None or spec.loader is None:
    raise SystemExit("Не удалось загрузить bootstrap-host.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

BootstrapError = module.BootstrapError
BootstrapHost = module.BootstrapHost


def assert_equal(actual, expected, message: str) -> None:
    if actual != expected:
        raise AssertionError(f"{message}\nОжидалось: {expected!r}\nПолучено: {actual!r}")


def test_infra_manager_role_can_move_to_another_vmid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        guest_dir = root / "infrastructure/guests/920-infra-manager"
        guest_dir.mkdir(parents=True)
        (guest_dir / "guest.yaml").write_text(
            "schema_version: 12\n"
            "vmid: 920\n"
            "name: infra-manager\n"
            "role: infra-manager\n",
            encoding="utf-8",
        )

        vmid, name = module._find_role_guest(root, "infra-manager")
        assert_equal(vmid, 920, "Роль infra-manager не должна зависеть от VMID 910")
        assert_equal(name, "infra-manager", "Имя должно читаться из guest.yaml")


class OwnershipHarness(BootstrapHost):
    def __init__(self, config: str) -> None:
        super().__init__("apply")
        self._config = config

    def infra_exists(self) -> bool:
        return True

    def pct_config(self, ctid: int) -> str:
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        return self._config


def test_strict_ownership_marker() -> None:
    good = """hostname: infra-manager
unprivileged: 1
description: Постоянный LXC [owner=proxmox-project;role=infra-manager]
"""
    if not OwnershipHarness(good).infra_config_is_expected():
        raise AssertionError("Ожидаемый 910 должен проходить строгую проверку владения")

    variants = (
        good.replace("hostname: infra-manager", "hostname: foreign"),
        good.replace("unprivileged: 1", "unprivileged: 0"),
        good.replace("owner=proxmox-project;", ""),
        good.replace("role=infra-manager", "role=other"),
    )
    for config in variants:
        if OwnershipHarness(config).infra_config_is_expected():
            raise AssertionError(
                "Неполный контракт владения не должен приниматься за infra-manager"
            )


class PveHelperHarness(BootstrapHost):
    def __init__(
        self,
        *,
        token_payload: str = '{"value":"test-secret"}',
        addresses: str = "192.168.1.152 STREAM pve\n",
    ) -> None:
        super().__init__("apply")
        self.token_payload = token_payload
        self.addresses = addresses
        self.events: list[tuple[str, ...] | str] = []

    def run(self, *args: str, **kwargs):
        del kwargs
        command = tuple(args)
        self.events.append(command)

        if command[:5] == (
            "pveum",
            "user",
            "token",
            "add",
            "root@pam",
        ):
            return SimpleNamespace(
                returncode=0,
                stdout=self.token_payload,
                stderr="",
            )
        if command == ("hostname", "-s"):
            return SimpleNamespace(returncode=0, stdout="pve\n", stderr="")
        if command == ("getent", "ahostsv4", "pve"):
            return SimpleNamespace(
                returncode=0,
                stdout=self.addresses,
                stderr="",
            )
        raise AssertionError(f"Неожиданная команда: {command!r}")

    def remove_named_token(self, user: str, token_name: str) -> None:
        self.events.append(f"remove:{user}!{token_name}")


def test_full_pve_token_contract() -> None:
    host = PveHelperHarness()
    token_id, secret = host.create_full_pve_token("infra-manager")

    assert_equal(
        (token_id, secret),
        ("root@pam!infra-manager", "test-secret"),
        "Должны возвращаться полный token ID и secret",
    )
    expected = (
        "pveum",
        "user",
        "token",
        "add",
        "root@pam",
        "infra-manager",
        "--privsep",
        "0",
        "--output-format",
        "json",
    )
    if expected not in host.events:
        raise AssertionError(
            "Полный PVE token должен создаваться явно с --privsep 0"
        )


def test_full_pve_token_missing_secret_is_removed() -> None:
    host = PveHelperHarness(token_payload="{}")
    try:
        host.create_full_pve_token("infra-manager")
    except BootstrapError:
        pass
    else:
        raise AssertionError("Token без secret должен считаться ошибкой")

    if "remove:root@pam!infra-manager" not in host.events:
        raise AssertionError("Token без secret должен быть сразу удалён")


def test_full_pve_token_invalid_json_is_removed() -> None:
    host = PveHelperHarness(token_payload="{invalid")
    try:
        host.create_full_pve_token("bootstrap-runner")
    except BootstrapError:
        pass
    else:
        raise AssertionError("Некорректный JSON PVE должен считаться ошибкой")

    if "remove:root@pam!bootstrap-runner" not in host.events:
        raise AssertionError("Token после некорректного ответа PVE должен быть удалён")


def test_pve_node_address() -> None:
    host = PveHelperHarness()
    assert_equal(
        host.pve_node_address(),
        ("pve", "192.168.1.152"),
        "Должны определяться имя и первый IPv4 PVE-узла",
    )

    missing = PveHelperHarness(addresses="")
    try:
        missing.pve_node_address()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Отсутствие IPv4 PVE должно считаться ошибкой")


class ProgressHarness(BootstrapHost):
    def __init__(self, log_file: Path) -> None:
        super().__init__("apply")
        self.log_file = log_file
        self.events: list[str] = []

    def info(self, message: str) -> None:
        self.events.append(message)


def test_progress_streaming() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log_file = Path(tmp) / "bootstrap.log"
        host = ProgressHarness(log_file)
        result = host.run(
            sys.executable,
            "-c",
            (
                'print("TASK [Собрать infra-runtime] ************************")\n'
                'print("PLAY RECAP ****************************************")'
            ),
            quiet=True,
            progress=True,
        )

        assert_equal(result.returncode, 0, "Потоковый запуск должен завершаться успешно")
        assert_equal(
            host.events,
            [
                "Ansible: Собрать infra-runtime",
                "Ansible: формирование итогов",
            ],
            "В основной консоли должны появляться названия долгих Ansible-задач",
        )
        log_text = log_file.read_text(encoding="utf-8")
        if "TASK [Собрать infra-runtime]" not in log_text or "PLAY RECAP" not in log_text:
            raise AssertionError("Полный вывод должен одновременно сохраняться в журнале")


class BootstrapStepHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("apply")
        self.calls: list[dict[str, object]] = []

    def log(self, message: str) -> None:
        del message

    def ok(self, message: str) -> None:
        del message

    def ct_exec(self, *args: str, **kwargs):
        del args
        self.calls.append(kwargs)
        return SimpleNamespace(returncode=0)


def test_bootstrap_steps_enable_progress() -> None:
    host = BootstrapStepHarness()

    host.create_infra_manager()
    host.configure_infra_manager_base()
    host.configure_infra_manager()

    assert_equal(
        [call.get("progress") for call in host.calls],
        [False, True, True],
        "Шаги настройки 990 должны показывать потоковый прогресс",
    )
    if not all(call.get("quiet") is True for call in host.calls):
        raise AssertionError("Подробный вывод должен продолжать сохраняться в журнале")


class InfraReadyHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("apply")
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

    def verify_infra_object(self) -> None:
        return

    def infra_test(self, flag: str, path: str) -> bool:
        return flag == "-x" and path == "/usr/local/sbin/infra-manager-status"

    def infra_exec(self, *args: str, **kwargs):
        self.calls.append((tuple(args), dict(kwargs)))
        return SimpleNamespace(returncode=0)


def test_infra_ready_uses_status_as_final_screen() -> None:
    host = InfraReadyHarness()

    host.verify_infra_ready(quiet=True)
    host.verify_infra_ready()

    quiet_args, quiet_kwargs = host.calls[0]
    visible_args, visible_kwargs = host.calls[1]

    if quiet_args[-2:] != ("--full", "--quiet"):
        raise AssertionError("Предварительная проверка должна скрывать итоговый экран")
    if quiet_kwargs.get("quiet") is not True:
        raise AssertionError("Предварительная проверка не должна попадать в консоль")

    if visible_args[-1:] != ("--full",) or "--quiet" in visible_args:
        raise AssertionError("Финальная проверка должна показывать полный экран состояния")
    if visible_kwargs.get("quiet") is not False:
        raise AssertionError("Финальный экран состояния должен выводиться в консоль")


class PersistentAttachHarness(BootstrapHost):
    def __init__(self, *, fail_mount: bool = False) -> None:
        super().__init__("apply")
        self.fail_mount = fail_mount
        self.running = True
        self.events: list[str] = []

    def infra_exists(self) -> bool:
        self.events.append("infra_exists")
        return True

    def infra_config_is_expected(self) -> bool:
        self.events.append("infra_expected")
        return True

    def pct_config(self, ctid: int) -> str:
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        self.events.append("pct_config")
        return "protection: 1\n"

    def pct_status(self, ctid: int) -> str:
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        self.events.append("status")
        return "running" if self.running else "stopped"

    def pct(self, *args: str, **kwargs):
        check = kwargs.get("check", True)
        command = " ".join(args)
        self.events.append(
            f"pct:{command}" + (":nocheck" if check is False else "")
        )
        if args[:2] == ("stop", str(self.infra_ctid)):
            self.running = False
        elif args[:2] == ("start", str(self.infra_ctid)):
            self.running = True
        elif "--mp0" in args and self.fail_mount:
            raise BootstrapError("тестовая ошибка подключения mount point")
        return SimpleNamespace(returncode=0)

    def verify_persistent_layout(self) -> None:
        self.events.append("verify_layout")

    def ok(self, message: str) -> None:
        del message
        self.events.append("ok")


def test_attach_persistent_layout_temporarily_disables_protection() -> None:
    host = PersistentAttachHarness()
    host.attach_persistent_layout()

    protection_off = "pct:set 910 --protection 0"
    mount_event = next(event for event in host.events if "--mp0" in event)
    protection_on = "pct:set 910 --protection 1"

    if protection_off not in host.events:
        raise AssertionError("Перед добавлением mount point защита 910 должна сниматься")
    if protection_on not in host.events:
        raise AssertionError("После добавления mount point защита 910 должна возвращаться")
    if host.events.index(protection_off) > host.events.index(mount_event):
        raise AssertionError("Защита должна сниматься до изменения mount point")
    if host.events.index(protection_on) < host.events.index(mount_event):
        raise AssertionError("Защита должна возвращаться после изменения mount point")
    if not host.running:
        raise AssertionError("После успешного подключения 910 должен быть запущен")
    if "verify_layout" not in host.events:
        raise AssertionError("Новая схема должна проверяться после подключения")


def test_attach_persistent_layout_restores_protection_on_failure() -> None:
    host = PersistentAttachHarness(fail_mount=True)
    try:
        host.attach_persistent_layout()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Ошибка подключения mount point должна передаваться выше")

    if "pct:set 910 --protection 1" not in host.events:
        raise AssertionError("При ошибке protection=1 должен быть восстановлен")
    if not host.running:
        raise AssertionError("При ошибке ранее запущенный 910 должен быть возвращён в работу")
    if "verify_layout" in host.events:
        raise AssertionError("Неуспешное подключение не должно считаться проверенным")


class RecoveryRootfsHarness(BootstrapHost):
    def __init__(self, *, owned: bool = True) -> None:
        super().__init__("recover")
        self.owned = owned
        self.exists = True
        self.events: list[str] = []

    def infra_exists(self) -> bool:
        return self.exists

    def infra_config_is_expected(self) -> bool:
        return self.owned

    def pct_status(self, ctid: int) -> str:
        if ctid != self.infra_ctid:
            raise AssertionError(f"Неожиданный VMID: {ctid}")
        return "running"

    def pct(self, *args: str, **kwargs):
        del kwargs
        self.events.append("pct:" + " ".join(args))
        return SimpleNamespace(returncode=0)

    def run(self, *args: str, **kwargs):
        del kwargs
        self.events.append("run:" + " ".join(args))
        if args[:2] == ("pct", "destroy"):
            self.exists = False
        return SimpleNamespace(returncode=0)

    def ok(self, message: str) -> None:
        del message


def test_recovery_rootfs_removal_is_strict() -> None:
    host = RecoveryRootfsHarness()
    host.remove_infra_rootfs_for_recovery()
    if not any("--protection 0" in event for event in host.events):
        raise AssertionError("Recovery должен снять protection перед удалением rootfs")
    if not any(event.startswith("pct:stop ") for event in host.events):
        raise AssertionError("Recovery должен остановить работающий infra-manager")
    if not any("pct destroy" in event and "--purge 1" in event for event in host.events):
        raise AssertionError("Recovery должен удалить только объект/rootfs infra-manager")

    foreign = RecoveryRootfsHarness(owned=False)
    try:
        foreign.remove_infra_rootfs_for_recovery()
    except BootstrapError:
        pass
    else:
        raise AssertionError("Recovery не должен удалять чужой объект")
    if foreign.events:
        raise AssertionError("До проверки владения разрушительные команды запрещены")


class ApplyHarness(BootstrapHost):
    def __init__(
        self,
        mode: str,
        *,
        infra_exists: bool,
    ) -> None:
        super().__init__(mode)
        self._infra_exists = infra_exists
        self.events: list[str] = []

    def verify_recovery_state(self) -> None:
        self.events.append("verify_recovery_state")

    def infra_exists(self) -> bool:
        self.events.append("infra_exists")
        return self._infra_exists

    def prepare_new_persistent_layout(self) -> None:
        self.events.append("prepare_new_layout")

    def verify_persistent_layout(self) -> None:
        self.events.append("verify_layout")

    def attach_persistent_layout(self) -> None:
        self.events.append("attach_layout")

    def prepare_runner(self) -> None:
        self.events.append("prepare_runner")

    def remove_infra_rootfs_for_recovery(self) -> None:
        self.events.append("remove_rootfs")
        self._infra_exists = False

    def create_infra_manager(self) -> None:
        self.events.append("create_infra")
        self._infra_exists = True

    def configure_infra_manager_base(self) -> None:
        self.events.append("configure_base")

    def configure_infra_manager(self) -> None:
        self.events.append("configure_full")

    def handoff_infra(self, access_mode: str = "apply") -> None:
        self.events.append(f"handoff:{access_mode}")

    def initialize_infra_openbao(self) -> None:
        self.events.append("initialize_openbao")

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        self.events.append("verify_ready:quiet" if quiet else "verify_ready")

    def finalize_runner(self) -> None:
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        self.events.append("check_ready")

    def info(self, message: str) -> None:
        self.events.append("info")


def test_new_install_flow() -> None:
    host = ApplyHarness("apply", infra_exists=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "prepare_new_layout",
            "prepare_runner",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:apply",
            "configure_full",
            "initialize_openbao",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Новая установка должна проходить один линейный bootstrap",
    )


def test_existing_infra_requires_normal_operations() -> None:
    host = ApplyHarness("apply", infra_exists=True)
    try:
        host.apply()
    except BootstrapError as exc:
        if "обычный deploy/repair" not in str(exc):
            raise
    else:
        raise AssertionError("990 не должен обслуживать существующий рабочий 910")

    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_layout",
        ],
        "Обычный bootstrap должен остановиться до подготовки 990",
    )


def test_recovery_recreates_existing_infra() -> None:
    host = ApplyHarness("recover", infra_exists=True)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_recovery_state",
            "verify_layout",
            "remove_rootfs",
            "prepare_runner",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:recover",
            "configure_full",
            "initialize_openbao",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Recovery должен всегда создавать новый rootfs через новый bootstrap-сеанс",
    )


def test_recovery_recreates_missing_infra() -> None:
    host = ApplyHarness("recover", infra_exists=False)
    host.apply()
    assert_equal(
        host.events,
        [
            "infra_exists",
            "verify_recovery_state",
            "info",
            "prepare_runner",
            "create_infra",
            "attach_layout",
            "configure_base",
            "handoff:recover",
            "configure_full",
            "initialize_openbao",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "Recovery отсутствующего 910 должен использовать тот же новый bootstrap-сеанс",
    )


class RecoveryStateHarness(BootstrapHost):
    def __init__(self, root: Path) -> None:
        super().__init__("recover")
        self.host_persistent_root = root
        self.host_pve_only_dir = root / "pve-only"
        self.host_access_dir = root / "access"
        self.host_state_dir = root / "state"
        self.host_openbao_dir = self.host_pve_only_dir / "openbao"
        self.host_recovery_dir = self.host_pve_only_dir / "recovery"
        self.host_recovery_github_key = (
            self.host_recovery_dir / "github_proxmox_repo_ed25519"
        )
        self.host_openbao_unseal_key = self.host_openbao_dir / "unseal.key"
        self.host_openbao_ssh_access = self.host_openbao_dir / "ssh-access.json"
        self.host_openbao_kv_access = self.host_openbao_dir / "kv-access.json"
        self.host_state_openbao_dir = self.host_state_dir / "openbao"
        self.host_state_openbao_raft_dir = self.host_state_openbao_dir / "raft"
        self.host_state_opentofu_file = (
            self.host_state_dir / "opentofu" / "state" / "proxmox.tfstate"
        )
        self.host_state_semaphore_db = (
            self.host_state_dir / "semaphore" / "semaphore.sqlite"
        )
        self.messages: list[str] = []

    def ok(self, message: str) -> None:
        self.messages.append(message)

    def managed_guests_exist(self) -> bool:
        return False


def _prepare_complete_recovery_state(host: RecoveryStateHarness) -> None:
    for directory in (
        host.host_openbao_dir,
        host.host_recovery_dir,
        host.host_state_openbao_raft_dir,
        host.host_state_opentofu_file.parent,
        host.host_state_semaphore_db.parent,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for path in (
        host.host_openbao_unseal_key,
        host.host_recovery_github_key,
        host.host_openbao_ssh_access,
        host.host_openbao_kv_access,
        host.host_state_openbao_raft_dir / "raft.db",
        host.host_state_opentofu_file,
        host.host_state_semaphore_db,
    ):
        path.write_text("test\n", encoding="utf-8")


def test_recovery_preflight_accepts_complete_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.verify_recovery_state()
        if not host.messages:
            raise AssertionError("Полное recovery-состояние должно подтверждаться")


def test_recovery_preflight_allows_missing_approle_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_openbao_ssh_access.unlink()
        host.host_openbao_kv_access.unlink()

        host.verify_recovery_state()


def test_recovery_preflight_allows_missing_access_directory() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)

        if host.host_access_dir.exists():
            raise AssertionError(
                "Recovery-state больше не должен требовать каталог access/"
            )
        host.verify_recovery_state()


def test_recovery_preflight_rejects_partial_state() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = RecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_openbao_unseal_key.unlink()
        try:
            host.verify_recovery_state()
        except BootstrapError as exc:
            if "unseal.key" not in str(exc):
                raise
        else:
            raise AssertionError("Recovery без unseal key должен быть запрещён")


class ManagedRecoveryStateHarness(RecoveryStateHarness):
    def managed_guests_exist(self) -> bool:
        return True


def test_recovery_preflight_requires_opentofu_state_for_managed_pool() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        host = ManagedRecoveryStateHarness(Path(tmp))
        _prepare_complete_recovery_state(host)
        host.host_state_opentofu_file.unlink()
        try:
            host.verify_recovery_state()
        except BootstrapError as exc:
            if "OpenTofu state" not in str(exc):
                raise
        else:
            raise AssertionError(
                "Recovery managed-гостей без OpenTofu state должен быть запрещён"
            )


class MigrationGuardHarness(ApplyHarness):
    def verify_persistent_layout(self) -> None:
        self.events.append("verify_layout")
        raise BootstrapError(
            "автоматическая миграция существующих данных запрещена"
        )


def test_existing_layout_requires_manual_migration() -> None:
    host = MigrationGuardHarness(
        "apply",
        infra_exists=True,
    )
    try:
        host.apply()
    except BootstrapError as exc:
        if "автоматическая миграция" not in str(exc):
            raise
    else:
        raise AssertionError(
            "Существующий 910 без новой схемы должен требовать ручную миграцию"
        )

    assert_equal(
        host.events,
        ["infra_exists", "verify_layout"],
        "До ручной миграции стандартный bootstrap не должен изменять 910",
    )


class ExecuteHarness(BootstrapHost):
    def __init__(self, mode: str) -> None:
        super().__init__(mode)
        self.events: list[str] = []

    def require_host(self) -> None:
        self.events.append("require_host")

    def verify_runner_contract(self) -> None:
        self.events.append("verify_runner")

    def info(self, message: str) -> None:
        self.events.append("info")

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        self.events.append("verify_ready:quiet" if quiet else "verify_ready")

    def finalize_runner(self) -> None:
        self.events.append("finalize_runner")

    def check_ready(self) -> None:
        self.events.append("check_ready")


def test_check_mode_finishes_temporary_runner() -> None:
    host = ExecuteHarness("check")
    host.execute()
    assert_equal(
        host.events,
        [
            "require_host",
            "verify_runner",
            "info",
            "verify_ready:quiet",
            "finalize_runner",
            "check_ready",
        ],
        "--check через публичный bootstrap должен удалить временный 990 после проверки 910",
    )


class ForeignRemoveHarness(BootstrapHost):
    def __init__(self) -> None:
        super().__init__("remove")
        self.events: list[str] = []

    def remove_runner_if_present(self) -> None:
        self.events.append("remove_runner")

    def infra_exists(self) -> bool:
        return True

    def infra_config_is_expected(self) -> bool:
        return False

    def pct(self, *args: str, **kwargs):
        self.events.append("pct:" + " ".join(args))
        raise AssertionError("Разрушительная команда pct не должна выполняться для чужого 910")


def test_remove_rejects_foreign_910() -> None:
    host = ForeignRemoveHarness()
    try:
        host.remove_infra()
    except BootstrapError:
        pass
    else:
        raise AssertionError("--remove должен отказаться удалять чужой VMID 910")

    assert_equal(
        host.events,
        ["remove_runner"],
        "После обнаружения чужого 910 не должно быть разрушительных действий над ним",
    )


def main() -> None:
    tests = [
        test_infra_manager_role_can_move_to_another_vmid,
        test_strict_ownership_marker,
        test_full_pve_token_contract,
        test_full_pve_token_missing_secret_is_removed,
        test_full_pve_token_invalid_json_is_removed,
        test_pve_node_address,
        test_progress_streaming,
        test_bootstrap_steps_enable_progress,
        test_infra_ready_uses_status_as_final_screen,
        test_attach_persistent_layout_temporarily_disables_protection,
        test_attach_persistent_layout_restores_protection_on_failure,
        test_recovery_rootfs_removal_is_strict,
        test_new_install_flow,
        test_existing_infra_requires_normal_operations,
        test_recovery_recreates_existing_infra,
        test_recovery_recreates_missing_infra,
        test_recovery_preflight_accepts_complete_state,
        test_recovery_preflight_allows_missing_approle_files,
        test_recovery_preflight_allows_missing_access_directory,
        test_recovery_preflight_rejects_partial_state,
        test_recovery_preflight_requires_opentofu_state_for_managed_pool,
        test_existing_layout_requires_manual_migration,
        test_check_mode_finishes_temporary_runner,
        test_remove_rejects_foreign_910,
    ]
    for test in tests:
        test()
    print(f"[ОК] Проверки bootstrap-host.py пройдены: {len(tests)}")


if __name__ == "__main__":
    main()
