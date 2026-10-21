#!/usr/bin/env python3
"""Проверки стандартных операций над гостями."""

from __future__ import annotations

import contextlib
import copy
import os
import runpy
import stat
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager import access as access_module
from infra_manager import guest_deploy as guest_deploy_module
from infra_manager import guest_deploy_infrastructure as guest_infra_module
from infra_manager import guest_operations as operations
from infra_manager import operation_lock
from infra_manager import semaphore as semaphore_module
from infra_manager.common import InfraManagerError
from infra_manager.guest_catalog import (
    deployable_guests,
    guest_identity,
    guest_management_address,
    resolve_guest_configuration,
)


def fail(message: str) -> None:
    raise SystemExit(message)


def check_operation_catalog() -> None:
    expected = {guest.vmid for guest in deployable_guests(ROOT)}
    for operation in ("deploy", "status", "repair"):
        actual = {
            guest.vmid
            for guest in operations.operation_guests(ROOT, operation)
        }
        if actual != expected:
            fail(
                f"Операция {operation} должна использовать общий каталог гостей"
            )

    actual_sync = {
        guest.vmid
        for guest in operations.operation_guests(ROOT, "sync")
    }
    # Не предлагать sync гостю без отдельных безопасных задач.
    if actual_sync != expected - {310, 311}:
        fail("Sync Guest должен исключать гостей без безопасной синхронизации")


def check_management_network() -> None:
    """Несколько интерфейсов не должны менять выбранный SSH/OTP-адрес."""

    identity = guest_identity(ROOT, 109)
    defaults_path = ROOT / "infrastructure" / "guests" / "defaults.yaml"
    defaults = yaml.safe_load(defaults_path.read_text(encoding="utf-8"))
    effective, address, subnet = resolve_guest_configuration(
        ROOT, identity.source, defaults
    )
    interfaces = effective["network"]["interfaces"]
    if (
        address != "192.168.1.9"
        or subnet != "192.168.0.0/16"
        or len(interfaces) != 2
        or interfaces[0]["ipv4"] != "10.0.0.2/30"
        or interfaces[1]["ipv4"] != "192.168.1.9/16"
        or not interfaces[1]["management"]
        or interfaces[0]["management"]
    ):
        fail("Resolver должен сохранять оба интерфейса 109 и выбирать LAN")

    if guest_management_address(ROOT, identity) != address:
        fail("Каталог гостей выбрал не административный адрес 109")
    if identity.address != address:
        fail("Свойство GuestIdentity.address должно учитывать management=true")
    if access_module.load_access_policy(ROOT).guests[109].address != address:
        fail("OpenBao/access.yaml выбрал неверный административный адрес 109")

    workspace = SimpleNamespace(
        payload={"guests": {"109": effective}},
        state_dir=ROOT / "automation" / "opentofu",
    )
    with patch.object(
        guest_infra_module.PveClient,
        "from_opentofu_env",
        return_value=object(),
    ):
        context = guest_infra_module._build_deployment_context(
            ROOT, 109, workspace
        )
    if context.address != address:
        fail("Deploy Guest не поддерживает management IP многосетевого гостя")

    # Проверяем, что адрес выбирается по флагу, а не случайно совпадает с VMID.
    altered = copy.deepcopy(identity.source)
    altered["network"]["interfaces"][1]["ipv4"] = "192.168.1.20/16"
    altered_identity = replace(identity, source=altered)
    if guest_management_address(ROOT, altered_identity) != "192.168.1.20":
        fail("Каталог игнорирует явно назначенный административный IP")

    original_load = access_module._load_yaml

    def load_with_changed_address(path: Path) -> dict:
        if path == identity.manifest:
            return altered
        return original_load(path)

    with patch.object(access_module, "_load_yaml", load_with_changed_address):
        changed = access_module.load_access_policy(ROOT)
    if changed.guests[109].address != "192.168.1.20":
        fail("Правила доступа не учитывают изменённый адрес management")

    altered["network"]["interfaces"][0]["management"] = True
    try:
        resolve_guest_configuration(ROOT, altered, defaults)
    except InfraManagerError:
        pass
    else:
        fail("Нельзя допускать два интерфейса management=true")


class FakePveClient:
    def __init__(self, resource: dict[str, object]) -> None:
        self.resource = resource
        self.actions: list[tuple[str, str, str]] = []

    def find_vm(self, vmid: int):
        if str(self.resource.get("vmid")) != str(vmid):
            return None
        return dict(self.resource)

    def run_task(
        self,
        method: str,
        endpoint: str,
        *,
        node: str,
        **kwargs: object,
    ) -> None:
        del kwargs
        self.actions.append((method, endpoint, node))
        if endpoint.endswith("/status/start"):
            self.resource["status"] = "running"


def check_status_operation() -> None:
    cases = (
        (
            guest_identity(ROOT, 109),
            {
                "vmid": 109,
                "name": "network-gateway",
                "type": "qemu",
                "node": "pve",
                "status": "running",
            },
        ),
        (
            guest_identity(ROOT, 910),
            {
                "vmid": 910,
                "name": "infra-manager",
                "type": "lxc",
                "node": "pve",
                "status": "running",
            },
        ),
    )

    for identity, resource in cases:
        client = FakePveClient(resource)
        with (
            patch.object(operations, "console") as console,
            patch.object(operations, "show_guest_status") as show,
            patch.object(
                operations,
                "project_branch_for_checkout",
                return_value="feature/unified-guest-status",
            ),
            patch.object(
                operations,
                "project_revision",
                return_value="abc1234",
            ),
        ):
            if operations._run_status(client, ROOT, identity) != 0:
                fail(
                    f"Status Guest должен завершаться успешно для {identity.vmid}"
                )
        console.info.assert_called_once_with(
            f"Проверка состояния гостя {identity.vmid} — {identity.name}"
        )
        show.assert_called_once()
        args, kwargs = show.call_args
        if args[:3] != (ROOT, identity, resource):
            fail("Status Guest передал неверные данные общему выводу")
        if kwargs != {
            "full": True,
            "show_secrets": False,
            "project_branch": "feature/unified-guest-status",
            "project_revision": "abc1234",
        }:
            fail(
                "Status Guest должен показывать полный статус без секретов "
                "и версию проекта"
            )

    stopped = FakePveClient(
        {
            "vmid": 109,
            "name": "network-gateway",
            "type": "qemu",
            "node": "pve",
            "status": "stopped",
        }
    )
    try:
        operations._run_status(stopped, ROOT, guest_identity(ROOT, 109))
    except InfraManagerError:
        pass
    else:
        fail("Status Guest должен считать остановленный гость ошибкой")

def check_repair_operation() -> None:
    identity = guest_identity(ROOT, 109)
    client = FakePveClient(
        {
            "vmid": 109,
            "name": "network-gateway",
            "type": "qemu",
            "node": "pve",
            "status": "stopped",
        }
    )
    if operations._run_repair(client, ROOT, identity) != 0:
        fail("Repair Guest не завершился после безопасного запуска гостя")
    if client.actions != [
        ("POST", "/nodes/pve/qemu/109/status/start", "pve")
    ]:
        fail(f"Repair Guest выполнил неожиданные действия: {client.actions!r}")


def check_infra_manager_repair_operation() -> None:
    identity = guest_identity(ROOT, 910)
    client = FakePveClient(
        {
            "vmid": 910,
            "name": "infra-manager",
            "type": "lxc",
            "node": "pve",
            "status": "running",
        }
    )
    with (
        patch.object(
            operations,
            "install_openbao_host_support",
        ) as install_host,
        patch.object(
            operations,
            "repair_openbao_on_host",
        ) as repair_openbao,
    ):
        if operations._run_repair(client, ROOT, identity) != 0:
            fail("Repair Guest infra-manager должен завершаться успешно")

    install_host.assert_called_once_with("pve", ROOT)
    repair_openbao.assert_called_once_with("pve")


def check_sync_operation() -> None:
    identity = guest_identity(ROOT, 910)
    with patch.object(
        operations,
        "_infra_manager_sync",
    ) as sync:
        if operations._run_sync(ROOT, identity) != 0:
            fail("Sync Guest infra-manager должен завершаться успешно")
    sync.assert_called_once_with(ROOT, identity)

    regular = guest_identity(ROOT, 109)
    # Подменяем внутренний исполнитель, а не run_deploy_guest:
    # реальный вызов Sync Guest должен принимать отдельную фазу sync.
    with patch.object(
        guest_deploy_module,
        "_run_deploy_guest",
        return_value=0,
    ) as deploy:
        if operations._run_sync(ROOT, regular) != 0:
            fail("Sync Guest обычного гостя должен завершаться успешно")
    deploy.assert_called_once_with(
        ROOT, 109, bootstrap_scope=False, phase="sync"
    )

    # Deploy Guest по умолчанию сохраняет полное развёртывание.
    with patch.object(
        guest_deploy_module,
        "_run_deploy_guest",
        return_value=0,
    ) as deploy:
        if guest_deploy_module.run_deploy_guest(ROOT, 109) != 0:
            fail("Deploy Guest должен использовать полное развёртывание")
    deploy.assert_called_once_with(
        ROOT, 109, bootstrap_scope=False, phase="all"
    )

    # Штатная точка входа не должна принимать фазы начального контура 990.
    try:
        guest_deploy_module.run_deploy_guest(
            ROOT, 109, phase="provision-control-plane"
        )
    except InfraManagerError:
        pass
    else:
        fail("Штатный deploy не должен принимать фазы начального контура")


def check_deploy_operation() -> None:
    identity = guest_identity(ROOT, 410)
    resource = {
        "vmid": 410,
        "name": "ai-control",
        "type": "qemu",
        "node": "pve",
        "status": "running",
    }
    client = FakePveClient(resource)

    with (
        patch.object(
            operations,
            "run_deploy_guest",
            return_value=0,
        ) as deploy,
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
            return_value=client,
        ),
        patch.object(operations, "show_guest_status") as show,
        patch.object(
            operations,
            "project_branch_for_checkout",
            return_value="feature/unified-guest-status",
        ),
        patch.object(
            operations,
            "project_revision",
            return_value="abc1234",
        ),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
    ):
        if operations._run_deploy(ROOT, identity) != 0:
            fail("Deploy Guest должен возвращать результат общего deploy")
    deploy.assert_called_once_with(ROOT, 410)
    show.assert_called_once()
    args, kwargs = show.call_args
    if args[:3] != (ROOT, identity, resource):
        fail("Deploy Guest передал неверные данные общему выводу")
    if kwargs != {
        "full": True,
        "show_secrets": False,
        "project_branch": "feature/unified-guest-status",
        "project_revision": "abc1234",
    }:
        fail(
            "Успешный Deploy Guest должен показывать итог без секретов "
            "и версию проекта"
        )
    reserve.assert_not_called()
    cancel.assert_not_called()

    # Прямой deploy через оператор PVE показывает статус снаружи;
    # стандартное задание Semaphore по-прежнему показывает его здесь.
    with (
        patch.dict(
            operations.os.environ,
            {"INFRA_MANAGER_OPERATOR_FINAL_STATUS": "1"},
        ),
        patch.object(
            operations,
            "run_deploy_guest",
            return_value=0,
        ) as deploy,
        patch.object(operations, "show_guest_status") as show,
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
        ) as client_factory,
    ):
        if operations._run_deploy(ROOT, identity) != 0:
            fail("Прямой Deploy Guest завершился ошибкой")
    deploy.assert_called_once_with(ROOT, 410)
    show.assert_not_called()
    client_factory.assert_not_called()

    infra = guest_identity(ROOT, 910)
    # При прямом обновлении PVE единый итоговый статус выводит оператор,
    # а не infra-runtime; блокировка активации остаётся зарезервированной.
    with (
        patch.dict(
            operations.os.environ,
            {
                "INFRA_MANAGER_OPERATOR_FINAL_STATUS": "1",
                "INFRA_MANAGER_OPERATOR_STATUS_VERSION": "2",
            },
        ),
        patch.object(
            operations, "run_deploy_guest", return_value=0,
        ) as deploy,
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
        patch.object(operations, "show_guest_status") as show,
        patch.object(
            operations.PveClient, "from_opentofu_env",
        ) as client_factory,
    ):
        if operations._run_deploy(ROOT, infra) != 0:
            fail("Прямой deploy infra-manager должен завершиться успешно")
    deploy.assert_called_once_with(ROOT, infra.vmid)
    reserve.assert_called_once_with()
    cancel.assert_not_called()
    show.assert_not_called()
    client_factory.assert_not_called()

    # Ни первая загрузка через старую PVE-оболочку, ни Semaphore не
    # должны показывать «полностью готов» до смены Docker-контейнера.
    with (
        patch.dict(
            operations.os.environ,
            {
                "INFRA_MANAGER_OPERATOR_FINAL_STATUS": "1",
                "INFRA_MANAGER_OPERATOR_STATUS_VERSION": "",
            },
        ),
        patch.object(operations, "run_deploy_guest", return_value=0),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
        patch.object(operations.PveClient, "from_opentofu_env") as client_factory,
        patch.object(operations, "show_guest_status") as show,
    ):
        if operations._run_deploy(ROOT, infra) != 0:
            fail("Первый deploy должен назначить отложенную активацию")
    reserve.assert_called_once_with()
    cancel.assert_not_called()
    show.assert_not_called()
    client_factory.assert_not_called()

    with (
        patch.object(
            operations, "run_deploy_guest", return_value=0,
        ),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
        patch.object(
            operations.PveClient, "from_opentofu_env",
            return_value=FakePveClient({
                "vmid": infra.vmid, "name": infra.name, "type": "lxc",
                "node": "pve", "status": "running",
            }),
        ),
        patch.object(operations, "show_guest_status") as show,
        patch.object(
            operations, "project_branch_for_checkout", return_value="main",
        ),
        patch.object(
            operations, "project_revision", return_value="abc1234",
        ),
    ):
        if operations._run_deploy(ROOT, infra) != 0:
            fail("Deploy infra-manager через Semaphore должен завершиться успешно")
    reserve.assert_called_once_with()
    cancel.assert_not_called()
    show.assert_not_called()

    with (
        patch.object(
            operations,
            "run_deploy_guest",
            return_value=1,
        ),
        patch.object(operations, "reserve_runtime_activation") as reserve,
        patch.object(operations, "cancel_runtime_activation") as cancel,
    ):
        if operations._run_deploy(ROOT, infra) != 1:
            fail("Deploy Guest должен сохранять код ошибки самообновления")
    reserve.assert_called_once_with()
    cancel.assert_called_once_with()

def check_status_during_self_update() -> None:
    # Только итоговый статус управляющего гостя может обходить блокировку
    # отложенной активации. Все обычные запросы сохраняют проверку.
    from contextlib import nullcontext

    with (
        patch.object(
            operations, "guest_identity",
            return_value=guest_identity(ROOT, 910),
        ),
        patch.object(
            operations, "project_checkout_lock",
            return_value=nullcontext(),
        ),
        patch.object(operations, "require_runtime_activation_idle") as idle,
        patch.object(operations, "_run_status", return_value=0) as show,
        patch.object(operations, "PveClient") as client,
    ):
        if operations.run_local_guest_status(
            ROOT, 910, show_secrets=True, allow_pending_activation=True,
        ) != 0:
            fail("Итоговый статус управляющего гостя недоступен")
    idle.assert_not_called()
    show.assert_called_once()
    if show.call_args.kwargs != {"show_secrets": True}:
        fail("Доверенный итог должен сохранить показ секретов")
    client.assert_called_once_with()

    with (
        patch.object(
            operations, "guest_identity",
            return_value=guest_identity(ROOT, 410),
        ),
        patch.object(
            operations, "project_checkout_lock",
            return_value=nullcontext(),
        ),
        patch.object(operations, "require_runtime_activation_idle") as idle,
        patch.object(operations, "_run_status") as show,
    ):
        try:
            operations.run_local_guest_status(
                ROOT, 410, allow_pending_activation=True,
            )
        except InfraManagerError as exc:
            if "управляющему гостю" not in str(exc):
                raise
        else:
            fail("Другим гостям нельзя обходить блокировку активации")
    idle.assert_not_called()
    show.assert_not_called()


def check_dispatch() -> None:
    try:
        operations.run_guest_operation(ROOT, "test", 109)
    except InfraManagerError:
        pass
    else:
        fail("Удалённая операция Test Guest не должна быть доступна")

    with (
        patch.object(operations, "require_runtime_activation_idle") as idle,
        patch.object(
            operations,
            "guest_identity",
            return_value=SimpleNamespace(
                vmid=109,
                name="network-gateway",
                role="network-gateway",
            ),
        ),
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
            return_value=SimpleNamespace(),
        ),
        patch.object(operations, "_run_status", return_value=0) as status,
    ):
        if operations.run_guest_operation(ROOT, "status", 109) != 0:
            fail("Диспетчер Status Guest вернул ошибку")
    idle.assert_called_once_with()
    status.assert_called_once()
    if status.call_args.kwargs != {"show_secrets": False}:
        fail("Диспетчер Semaphore должен запрещать вывод секретов")

    status.reset_mock()
    with (
        patch.object(operations, "require_runtime_activation_idle"),
        patch.object(
            operations,
            "guest_identity",
            return_value=SimpleNamespace(
                vmid=109,
                name="network-gateway",
                role="network-gateway",
            ),
        ),
        patch.object(
            operations.PveClient,
            "from_opentofu_env",
            return_value=SimpleNamespace(),
        ),
        patch.object(operations, "_run_status", status),
    ):
        if operations.run_guest_operation(
            ROOT,
            "status",
            109,
            show_secrets=True,
        ) != 0:
            fail("Доверенный Status Guest вернул ошибку")
    if status.call_args.kwargs != {"show_secrets": True}:
        fail("Явный доверенный вызов должен разрешать вывод секретов")



def check_operation_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock_dir = Path(tmp)
        lock_path = lock_dir / "410.lock"

        with operation_lock.guest_operation_lock(
            410,
            "deploy",
            lock_dir=lock_dir,
        ):
            mode = stat.S_IMODE(lock_path.stat().st_mode)
            if mode != 0o660:
                fail(f"Lock-файл должен иметь режим 0660, получен {mode:o}")
            try:
                with operation_lock.guest_operation_lock(
                    410,
                    "repair",
                    lock_dir=lock_dir,
                ):
                    fail("Повторная операция над одним VMID не должна запускаться")
            except InfraManagerError as exc:
                if "410" not in str(exc) or "deploy" not in str(exc):
                    fail("Ошибка блокировки должна показывать занятого гостя и операцию")

            with operation_lock.guest_operation_lock(
                420,
                "deploy",
                lock_dir=lock_dir,
            ):
                pass

        lock_path.write_text(
            '{"vmid": 410, "operation": "old", "pid": 999999}\n',
            encoding="utf-8",
        )
        with operation_lock.guest_operation_lock(
            410,
            "sync",
            lock_dir=lock_dir,
        ):
            pass

        try:
            with operation_lock.guest_operation_lock(
                410,
                "sync",
                lock_dir=lock_dir,
            ):
                raise RuntimeError("test")
        except RuntimeError:
            pass

        with operation_lock.guest_operation_lock(
            410,
            "repair",
            lock_dir=lock_dir,
        ):
            pass


        (lock_dir / "locks").mkdir()
        with patch.object(
            operation_lock,
            "PATHS",
            SimpleNamespace(data_dir=lock_dir),
        ):
            with operation_lock.project_checkout_lock(exclusive=False):
                with operation_lock.project_checkout_lock(exclusive=False):
                    pass
                try:
                    with operation_lock.project_checkout_lock(exclusive=True):
                        fail("Update не должен идти одновременно с чтением проекта")
                except InfraManagerError:
                    pass

            with operation_lock.project_checkout_lock(exclusive=True):
                try:
                    with operation_lock.project_checkout_lock(exclusive=False):
                        fail("Операция не должна читать проект во время update")
                except InfraManagerError:
                    pass


        # Semaphore больше не является запасным локальным хранилищем
        # блокировок: отсутствие общей state/locks должно останавливать
        # изменяющую операцию, не создавая каталог в rootfs.
        missing = lock_dir / "missing-data"
        with patch.object(
            operation_lock, "PATHS", SimpleNamespace(data_dir=missing),
        ):
            try:
                with operation_lock.guest_operation_lock(410, "deploy"):
                    fail("Отсутствующие общие блокировки не должны обходиться")
            except InfraManagerError as exc:
                if "state/locks" not in str(exc):
                    fail("Нужна понятная диагностика отсутствия state/locks")
        if missing.exists():
            fail("При отсутствии общего диска нельзя создавать lock-dir")

        with patch.object(
            operation_lock, "PATHS", SimpleNamespace(data_dir=lock_dir),
        ):
            with operation_lock.guest_operation_lock(410, "deploy"):
                if not (lock_dir / "locks/410.lock").is_file():
                    fail("Должен использоваться только постоянный каталог locks")

        # Внутри LXC используется безопасный symlink на общий state,
        # а в runtime — bind mount. Нельзя отвергать штатный symlink.
        persistent = lock_dir / "persistent/locks"
        persistent.mkdir(parents=True)
        lxc_dir = lock_dir / "lxc"
        lxc_dir.mkdir()
        lxc_link = lxc_dir / "locks"
        lxc_link.symlink_to(persistent, target_is_directory=True)
        with (
            patch.object(operation_lock, "PATHS", SimpleNamespace(data_dir=lxc_dir)),
            patch.object(operation_lock, "TRUSTED_STATE_LOCKS", persistent),
            patch.object(operation_lock, "_is_mount", return_value=True),
        ):
            with operation_lock.guest_operation_lock(410, "deploy"):
                if not (persistent / "410.lock").exists():
                    fail("Штатная ссылка LXC не использует общие блокировки")
        with (
            patch.object(operation_lock, "PATHS", SimpleNamespace(data_dir=lxc_dir)),
            patch.object(operation_lock, "TRUSTED_STATE_LOCKS", persistent),
            patch.object(operation_lock, "_is_mount", return_value=False),
        ):
            try:
                with operation_lock.guest_operation_lock(410, "deploy"):
                    fail("Ссылка на неподключённое состояние недопустима")
            except InfraManagerError:
                pass
        lxc_link.unlink()
        lxc_link.symlink_to(lock_dir, target_is_directory=True)
        with (
            patch.object(operation_lock, "PATHS", SimpleNamespace(data_dir=lxc_dir)),
            patch.object(operation_lock, "TRUSTED_STATE_LOCKS", persistent),
            patch.object(operation_lock, "_is_mount", return_value=True),
        ):
            try:
                with operation_lock.guest_operation_lock(410, "deploy"):
                    fail("Подмена доверенной ссылки недопустима")
            except InfraManagerError:
                pass


def check_guest_operation_working_directory() -> None:
    """Оператор запускает OpenTofu из доступного снимка, а не Docker WORKDIR."""
    entrypoint = ROOT / "scripts" / "infra-manager" / "jobs" / "guest-operation.py"
    main_fn = runpy.run_path(str(entrypoint))["main"]
    calls: list[tuple[Path, Path, str, int]] = []

    def fake_operation(repo_root: Path, operation: str, vmid: int, **_kwargs: object) -> int:
        calls.append((Path.cwd(), repo_root, operation, vmid))
        return 0

    with tempfile.TemporaryDirectory() as temp_dir:
        previous = Path.cwd()
        try:
            os.chdir(temp_dir)
            with (
                patch.object(sys, "argv", ["guest-operation.py", "deploy", "310"]),
                patch.dict(
                    main_fn.__globals__,
                    {
                        "prepare_operation_environment": lambda: None,
                        "run_guest_operation": fake_operation,
                    },
                ),
            ):
                code = main_fn()
        finally:
            os.chdir(previous)

    if code != 0:
        fail("Операция гостя не прошла проверку рабочего каталога")
    if calls != [(ROOT, ROOT, "deploy", 310)]:
        fail(
            "Штатная операция должна переходить в снимок проекта "
            f"до запуска OpenTofu, получено {calls!r}"
        )


def check_semaphore_secret_policy() -> None:
    entrypoint = (
        ROOT / "scripts" / "infra-manager" / "jobs" / "guest-operation.py"
    ).read_text(encoding="utf-8")
    if "show_secrets=False" not in entrypoint:
        fail("Точка входа операций должна явно запрещать вывод секретов")
    if "prepare_operation_environment()" not in entrypoint:
        fail("Точка входа операций должна готовить среду без Semaphore API")
    if "PveClient()" not in entrypoint:
        fail("Прямой запуск должен получать PVE credential из локального файла")
    if "PATHS.ansible_public_key" in entrypoint:
        fail("Прямой запуск не должен зависеть от постоянного Ansible-ключа")
    if "TF_VAR_bootstrap_ssh_public_key" in entrypoint:
        fail("Одноразовый bootstrap key не должен жить в общей среде операций")


def main() -> None:
    check_operation_catalog()
    check_management_network()
    check_status_operation()
    check_repair_operation()
    check_infra_manager_repair_operation()
    check_sync_operation()
    check_deploy_operation()
    check_status_during_self_update()
    check_dispatch()
    check_operation_lock()
    check_guest_operation_working_directory()
    check_semaphore_secret_policy()
    print("[ОК] Стандартные операции над гостями проверены")


if __name__ == "__main__":
    main()
