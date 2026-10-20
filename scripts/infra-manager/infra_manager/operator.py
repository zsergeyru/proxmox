"""Операторские команды внутри управляющего гостя infra-manager.

Ветку Git и снимок проекта выбирает общий операторский слой.
Изменяющий запуск защищён блокировкой на период подготовки снимка;
длительная операция использует подготовленную копию. Прямой вывод
служебных секретов разрешён только явно доверенным вызовам с PVE.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from .common import (
    InfraManagerError,
    console,
    require_runtime_activation_idle,
    run,
)
from .guest_catalog import find_guest_by_role, guest_identity
from .guest_deploy import project_branch_for_checkout, project_revision
from .guest_operations import list_local_guests, run_local_guest_status
from .operation_lock import _backup_gate, project_checkout_lock
from .physical_host_state import checked_record_path
from .pve import PveClient
from .pve_host import (
    preflight_recovery_contour,
    read_backup_host_status,
    repair_openbao_on_host,
)
from .settings import PATHS, SETTINGS

ACTIVATE_RUNTIME = Path("/usr/local/sbin/infra-manager-activate-runtime")
COMPOSE_FILE = PATHS.compose_dir / "docker-compose.yml"
VERSIONS_FILE = PATHS.compose_dir / ".versions.env"
RUNTIME_CONTAINER = "infra-runtime"
# systemd может ждать старое задание до 15 минут; PVE ждёт только
# постоянную запись операции, а не сам процесс в заменяемом Docker.
ACTIVATION_WAIT_SECONDS = 25 * 60
ACTIVATION_POLL_SECONDS = 2
ACTIVATION_TERMINAL_PHASES = frozenset({
    "completed", "failed", "rolled-back", "needs-recovery",
})
RUNTIME_PROJECT_ROOT = str(PATHS.repo_root)
RUNTIME_GUEST_OPERATION = (
    PATHS.repo_root / "scripts" / "infra-manager" / "jobs" / "guest-operation.py"
)
RUNTIME_PHYSICAL_OPERATION = (
    PATHS.repo_root / "scripts" / "infra-manager" / "jobs" / "physical-host-operation.py"
)
RUNTIME_SNAPSHOT_SCRIPT = """
# Только подготовка снимка требует root: Git-копия управляющего гостя принадлежит root.
# Код гостевых операций по-прежнему выполняется как Semaphore (1001:0).
umask 077
rm -rf "$1"
mkdir -p "$1"
cp -a "$2"/. "$1"/
chown -R 1001:0 "$1"
""".strip()
RUNTIME_DIRECT_SCRIPT = """
trap 'rm -rf "$1"' EXIT INT TERM
# Итоговый статус любого гостя печатает оператор PVE, а не Docker/Semaphore.
# В обычном задании Semaphore единый итог остаётся внутри задания без секретов.
if [ "$2" = "deploy" ]; then
    export INFRA_MANAGER_OPERATOR_FINAL_STATUS=1
    # Только новый оператор умеет выводить итог после самообновления.
    export INFRA_MANAGER_OPERATOR_STATUS_VERSION=2
fi
python3 "$1/scripts/infra-manager/jobs/guest-operation.py" "$2" "$3"
""".strip()


RUNTIME_PHYSICAL_SCRIPT = """
trap 'rm -rf "$1"' EXIT INT TERM
python3 "$1/scripts/infra-manager/jobs/physical-host-operation.py" "$2" "$3"
""".strip()


def _require_root() -> None:
    """Разрешить служебные операции только пользователю root внутри управляющего гостя."""
    if os.geteuid() != 0:
        raise InfraManagerError(
            "Операторская команда infra-manager должна выполняться от root внутри управляющего гостя"
        )


def _pve_node() -> str:
    """Получить адрес доверенного PVE для привилегированных операций.

    Явно переданное имя проверяется до использования в SSH-команде."""
    explicit = os.environ.get("INFRA_PVE_NODE", "").strip()
    if explicit:
        if any(character.isspace() for character in explicit) or "/" in explicit:
            raise InfraManagerError("INFRA_PVE_NODE имеет некорректный формат. Укажите имя узла PVE без пробелов и символа /")
        return explicit

    client = PveClient()
    node = urlparse(client.url).hostname
    if not node:
        raise InfraManagerError(
            f"Не удалось определить узел PVE из {client.url!r}"
        )
    return node


def _refresh_project_checkout() -> None:
    """Обновить постоянную Git-копию. Блокировку обеспечивает вызывающий код."""

    repo_root = PATHS.repo_root
    git_dir = repo_root / ".git"
    known_hosts = PATHS.config_dir / "github_known_hosts"
    for path, label in (
        (git_dir, "Git-репозиторий проекта"),
        (PATHS.github_key, "GitHub Deploy Key"),
        (known_hosts, "GitHub known_hosts"),
    ):
        if not path.exists():
            raise InfraManagerError(f"Не найден {label}: {path}")

    branch = SETTINGS.project_branch().strip()
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", branch)
        or ".." in branch
        or "//" in branch
        or branch.endswith(("/", "."))
    ):
        raise InfraManagerError("Некорректная Git-ветка проекта")

    ssh_command = shlex.join(
        [
            "ssh",
            "-i",
            str(PATHS.github_key),
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
        ]
    )
    environment = os.environ.copy()
    environment["GIT_SSH_COMMAND"] = ssh_command

    console.info(f"Получение Git-ветки {branch}")
    run(
        [
            "git",
            "-C",
            str(repo_root),
            "fetch",
            "--depth",
            "1",
            "origin",
            branch,
        ],
        env=environment,
    )
    run(
        [
            "git",
            "-C",
            str(repo_root),
            "checkout",
            "-f",
            "-B",
            branch,
            "FETCH_HEAD",
        ]
    )
    run(["git", "-C", str(repo_root), "clean", "-ffdx"])
    console.result(
        f"Рабочая копия проекта обновлена: {branch} "
        f"({project_revision(repo_root)})"
    )


def _standalone_runtime() -> bool:
    """Определить версию исполнения по установленным версиям управляющего гостя."""
    if VERSIONS_FILE.is_symlink() or not VERSIONS_FILE.is_file():
        return False
    return "RUNTIME_MODE=standalone" in VERSIONS_FILE.read_text(
        encoding="utf-8"
    ).splitlines()


def _prepare_runtime_snapshot(
    operation: str, vmid: int | str, *,
    physical: bool = False,
    preflight_storage: bool = False,
) -> str:
    """Подготовить снимок Git и, для deploy 910, проверить носители новым кодом.

    Проверка и создание копии идут под одной блокировкой checkout: старый
    импортированный operator.py не исполняет новую дисковую логику сам.
    """

    snapshot = f"/tmp/infra-manager-direct-{os.getpid()}-{vmid}"

    def copy_snapshot() -> None:
        """Скопировать текущую версию проекта внутрь рабочей среды.

        Отсутствие точки входа останавливает задачу до развёртывания гостя."""
        entrypoint = RUNTIME_PHYSICAL_OPERATION if physical else RUNTIME_GUEST_OPERATION
        if not entrypoint.is_file():
            raise InfraManagerError(
                f"Не найдена точка входа операций: {entrypoint}"
            )
        console.info("Шаг: подготовка снимка проекта в infra-runtime")
        run(
            [
                "docker",
                "exec",
                "--user",
                "0",
                RUNTIME_CONTAINER,
                "sh",
                "-eu",
                "-c",
                RUNTIME_SNAPSHOT_SCRIPT,
                "infra-manager-snapshot",
                snapshot,
                RUNTIME_PROJECT_ROOT,
            ]
        )

    if preflight_storage and (operation != "deploy" or physical or not isinstance(vmid, int)):
        raise InfraManagerError("SSD/NAS preflight разрешён только при deploy управляющего гостя")

    if operation in {"deploy", "sync", "bootstrap", "storage-plan", "storage-prepare"}:
        # Снимок и новая проверка дисков обязаны происходить при одном checkout.
        with project_checkout_lock(exclusive=True):
            phase_started = time.monotonic()
            _refresh_project_checkout()
            console.timing("Git fetch/checkout", time.monotonic() - phase_started)
            phase_started = time.monotonic()
            copy_snapshot()
            console.timing("Снимок проекта для deploy", time.monotonic() - phase_started)
            if preflight_storage:
                # Новая точка входа находится в обновлённом Git, а не в
                # /usr/local/lib/infra-manager текущего процесса. Запуск
                # отдельного Python исключает повторное использование sys.modules.
                entrypoint = PATHS.repo_root / "scripts/infra-manager/jobs/storage-preflight.py"
                if entrypoint.is_symlink() or not entrypoint.is_file():
                    raise InfraManagerError(
                        "В обновлённой Git-версии нет доверенной проверки SSD/NAS"
                    )
                console.info("Проверка SSD/NAS кодом обновлённой Git-версии")
                phase_started = time.monotonic()
                run([sys.executable, str(entrypoint)], stream_output=True)
                console.timing("Предварительная проверка SSD/NAS",
                               time.monotonic() - phase_started)
    else:
        with project_checkout_lock(exclusive=False):
            copy_snapshot()

    return snapshot


def operator_status(*, show_secrets: bool) -> int:
    """Показать общий полный статус управляющего гостя по его роли."""

    identity = find_guest_by_role(PATHS.repo_root, SETTINGS.infra_manager_role)
    return operator_guest_status(identity.vmid, show_secrets=show_secrets)


def operator_guest_status(
    vmid: int,
    *,
    show_secrets: bool,
    allow_pending_activation: bool = False,
) -> int:
    """Показать единый статус выбранного гостя."""

    result = run_local_guest_status(
        PATHS.repo_root,
        vmid,
        show_secrets=show_secrets,
        allow_pending_activation=allow_pending_activation,
    )
    manager = find_guest_by_role(PATHS.repo_root, SETTINGS.infra_manager_role)
    if vmid == manager.vmid:
        # Проверяемые копии нового системного исполнителя не следует путать
        # с прежним PVE-only статусом полной резервной копии.
        try:
            from .operation_store import OperationStore

            record = OperationStore().settings / "backup-last-core.json"
            if record.is_file() and not record.is_symlink():
                contents = json.loads(record.read_text(encoding="utf-8"))
                if (contents.get("includes_vm100") is False
                        and contents.get("restore_tested") is False):
                    console.warning(
                        "Состояние управляющего гостя/PVE резервировалось отдельно; "
                        "копия VM 100 и пробное восстановление не подтверждены"
                    )
        except (InfraManagerError, OSError, ValueError, TypeError):
            pass
        # Результат резервирования независим от других проверок управляющего гостя.
        # Плохой статус Docker/OpenBao не должен скрывать состояние копий.
        try:
            print("\nРезервирование")
            print(read_backup_host_status(_pve_node()))
        except (InfraManagerError, OSError):
            console.warning(
                "Резервирование PVE не подтверждено: "
                "доступен только минимальный PVE-only отчёт"
            )
    return result


def _wait_for_runtime_activation(
    store: object,
    operation_id: str,
    *,
    timeout_seconds: int = ACTIVATION_WAIT_SECONDS,
) -> dict | None:
    """Дождаться проверенного systemd-результата по постоянному ID операции.

    Не ждём Docker-процесс через PVE: systemd обязан иметь возможность
    остановить старый infra-runtime после выхода выполнявшегося в нём
    Ansible. Здесь лишь читаем состояние, независимое от Docker.
    """
    deadline = time.monotonic() + timeout_seconds
    last_phase: str | None = None
    while True:
        record = store.get(operation_id)
        phase = record.get("phase")
        if phase in ACTIVATION_TERMINAL_PHASES:
            return record
        if phase != last_phase:
            details = {
                "queued": "ожидание запуска задания",
                "running": "ожидание передачи задания systemd",
                "activating": "переключение управляющей среды",
                "verifying": "проверка нового infra-runtime, infra-web и OpenBao",
            }.get(phase, "ожидание подтверждения")
            console.info(f"Активация 910: {details}")
            last_phase = phase
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(ACTIVATION_POLL_SECONDS, remaining))


def operator_guest_task(
    operation: str,
    vmid: int,
    *,
    show_secrets: bool,
) -> int:
    """Выполнить операцию и показать полное время работы команды."""
    started = time.monotonic()
    completed = False
    interrupted = False
    deferred = False
    activation_handed_off = False
    store = None
    operation_id = None
    prepared_snapshot = None
    fast_plan = None
    fast_recovery_required = False
    if operation == "deploy":
        identity = guest_identity(PATHS.repo_root, vmid)
        if identity.role == SETTINGS.infra_manager_role:
            from .operation_store import OperationStore

            # История обязана указывать фактически развернутую ревизию.
            # Подготовка снимка обновляет Git под общей блокировкой,
            # поэтому только после неё резервируем UUID операции.
            require_runtime_activation_idle()
            prepared_snapshot = _prepare_runtime_snapshot(
                operation, vmid, preflight_storage=True,
            )
            # Предыдущая успешная активация оставляет подтверждённый
            # манифест файлов. Любое неожиданное изменение -> полный Ansible.
            from .self_update_fast import plan as select_fast_plan
            store = OperationStore()
            fast_plan = select_fast_plan(PATHS.repo_root, store=store)
            if fast_plan.mode == "fast":
                summary = (
                    f"web={len(fast_plan.web)}, "
                    f"системный Python={len(fast_plan.python)}"
                )
                console.info(f"Быстрый путь deploy 910: {summary}")
            else:
                console.info(
                    "Полный безопасный Ansible: " + fast_plan.reason
                )
            # Согласовать создание active/910.json с эксклюзивной
            # блокировкой резервирования PVE. После отпускания shared
            # gate активная запись остаётся и запрещает старт backup
            # на всём протяжении независимой systemd-активации.
            with _backup_gate():
                record, _ = store.reserve(
                    "deploy", vmid, project_revision(PATHS.repo_root),
                    activation_required=fast_plan is None or fast_plan.mode != "fast",
                )
                operation_id = record["id"]
                store.start(operation_id)
            console.info(f"Операция самообновления: {operation_id}")
    try:
        if fast_plan is not None and fast_plan.mode == "fast":
            from .operation_lock import guest_operation_lock
            from .guest_operations import _run_status
            from .self_update_fast import FastRollbackUnconfirmed, apply as apply_fast_update
            assert store is not None and operation_id is not None
            try:
                with guest_operation_lock(vmid, "deploy"):
                    apply_fast_update(
                        fast_plan,
                        # apply() держит эксклюзивный project.lock: нельзя
                        # заходить повторно в run_local_guest_status(),
                        # который пытается получить ещё одну shared-блокировку.
                        lambda: _run_status(
                            PveClient(), PATHS.repo_root,
                            guest_identity(PATHS.repo_root, vmid),
                            show_secrets=show_secrets,
                        ) == 0,
                        repo=PATHS.repo_root,
                        store=store,
                    )
                    store.finish(operation_id, exit_code=0)
            except FastRollbackUnconfirmed:
                # Оставить active/910.json для ручной проверки; не разблокировать
                # следующее изменение, когда rollback не подтверждён.
                fast_recovery_required = True
                raise
            console.result(
                f"Быстрое обновление 910 подтверждено: {operation_id}; "
                "полный Ansible и infra-runtime не запускались"
            )
            completed = True
            return 0
        task_options = {"show_secrets": show_secrets}
        if operation_id is not None:
            task_options["operation_id"] = operation_id
            task_options["prepared_snapshot"] = prepared_snapshot
        result = _operator_guest_task(operation, vmid, **task_options)
        if store is not None and operation_id is not None:
            # Успешный выход Ansible означает передачу активации systemd,
            # а НЕ готовность гостя. Если оператор прервут во время
            # ожидания, нельзя помечать фоновую активацию как неуспешную.
            activation_handed_off = result == 0
            current = store.get(operation_id)
            if result != 0:
                if current["phase"] == "running":
                    store.finish(operation_id, exit_code=result)
                return result

            if current["phase"] == "running":
                # Системная служба также умеет переходить из running к
                # verifying. Учитываем её возможное опережение оператора.
                try:
                    store.transition(operation_id, "activating")
                except InfraManagerError:
                    if store.get(operation_id)["phase"] not in (
                        "activating", "verifying", "completed",
                        "failed", "rolled-back", "needs-recovery",
                    ):
                        raise

            console.info(
                f"Ожидание окончательной проверки активации 910: {operation_id}"
            )
            current = _wait_for_runtime_activation(store, operation_id)
            if current is None:
                deferred = True
                console.warning(
                    f"Активация 910 ещё не подтверждена: {operation_id}. "
                    "Проверьте результат по идентификатору в "
                    "/mnt/persistent-state/operations/history; "
                    "не запускайте повторный deploy до выяснения состояния."
                )
                return 2

            phase = current["phase"]
            if phase != "completed" or current.get("result") != "ok":
                console.error(
                    f"Активация 910 завершилась без подтверждения готовности: "
                    f"{operation_id}; этап {phase}; код {current.get('exit_code')}. "
                    "Проверьте журнал внутри 910: "
                    "/var/log/infra-manager/runtime-activation.log. "
                    "Не повторяйте deploy без проверки состояния."
                )
                return 1

            console.result(f"Активация 910 подтверждена systemd: {operation_id}")
            # Результат уже completed, но EXIT-обработчик службы может ещё
            # удалять последний маркер. Полная проверка статуса допустима
            # только после подтверждённого успешного результата именно
            # этого задания, независимо от короткой гонки удаления маркера.
            status_result = operator_guest_status(
                vmid,
                show_secrets=show_secrets,
                allow_pending_activation=True,
            )
            completed = status_result == 0
            return status_result
        completed = result == 0
        return result
    except KeyboardInterrupt:
        interrupted = True
        raise
    finally:
        # Полный deploy удаляет снимок в RUNTIME_DIRECT_SCRIPT. При fast
        # путь в infra-runtime не вызывается, поэтому очищаем его здесь.
        if fast_plan is not None and fast_plan.mode == "fast" and prepared_snapshot:
            if re.fullmatch(r"/tmp/infra-manager-direct-[0-9]+-910", prepared_snapshot):
                cleaned = run(
                    ["docker", "exec", "--user", "0", RUNTIME_CONTAINER,
                     "rm", "-rf", "--", prepared_snapshot],
                    capture_output=True, check=False,
                )
                if cleaned.returncode:
                    console.warning("Не удалось убрать временный снимок быстрого deploy")
            else:
                console.warning("Неизвестный путь снимка infra-runtime; очистка запрещена")
        if store is not None and operation_id is not None:
            current = store.get(operation_id)
            if (current["phase"] == "running" and not activation_handed_off
                    and not fast_recovery_required):
                store.finish(operation_id, exit_code=1)
        console.timing(
            f"Команда {operation} гостя {vmid}",
            time.monotonic() - started,
            interrupted=interrupted,
            failed=not completed and not interrupted and not deferred,
        )


def _operator_guest_task(
    operation: str,
    vmid: int,
    *,
    show_secrets: bool,
    operation_id: str | None = None,
    prepared_snapshot: str | None = None,
) -> int:
    """Выполнить штатную операцию напрямую внутри infra-runtime."""

    require_runtime_activation_idle()
    snapshot = (
        prepared_snapshot
        if prepared_snapshot is not None
        else _prepare_runtime_snapshot(operation, vmid)
    )
    console.info(f"Шаг: запуск {operation} для гостя {vmid} в infra-runtime")
    result = run(
        [
            "docker",
            "exec",
            "--user",
            "1001:0",
            "-e",
            f"INFRA_MANAGER_COLOR={'1' if console._color(chr(27)) else '0'}",
            "-e",
            f"NO_COLOR={os.environ.get('NO_COLOR', '')}",
            *(["-e", f"INFRA_OPERATION_ID={operation_id}"] if operation_id else []),
            *(["-e", "INFRA_RUNTIME_MODE=standalone"] if _standalone_runtime() else []),
            RUNTIME_CONTAINER,
            "sh",
            "-eu",
            "-c",
            RUNTIME_DIRECT_SCRIPT,
            "infra-manager-direct",
            snapshot,
            operation,
            str(vmid),
        ],
        check=False,
        stream_output=True,
    )
    if result.returncode != 0:
        console.error(
            f"Операция {operation} для гостя {vmid} "
            f"не завершена (код {result.returncode}). "
            f"Выполните infra-manager status {vmid}, устраните указанную выше причину и повторите операцию."
        )
        return result.returncode
    if operation == "deploy":
        identity = guest_identity(PATHS.repo_root, vmid)
        if identity.role == SETTINGS.infra_manager_role:
            # Нельзя сообщать «полностью готов» до внешней
            # systemd-активации и проверки новой среды.
            console.info(
                "Настройка завершена. Systemd активирует infra-runtime "
                "и infra-web, затем оператор дождётся проверки готовности. "
                "Журнал: /var/log/infra-manager/runtime-activation.log"
            )
            return 0

    return operator_guest_status(
        vmid,
        show_secrets=show_secrets,
    )


def operator_physical_bootstrap_state(name: str) -> int:
    """Запросить пароль только до первого подтверждённого SSH-сертификата."""

    import yaml

    if name != "nas":
        raise InfraManagerError("Начальный доступ пока поддерживается только для NAS")
    manifest = PATHS.repo_root / "infrastructure" / "hosts" / name / "host.yaml"
    if not manifest.is_file():
        raise InfraManagerError("Машинное описание NAS отсутствует")
    try:
        document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        connection = document["connection"]
        fingerprint = connection["ssh_host_fingerprint"]
        address = connection["address"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
        raise InfraManagerError("Не удалось проверить адрес и SSH-ключ NAS") from exc
    if not isinstance(address, str) or not address or not isinstance(fingerprint, str) or not fingerprint:
        raise InfraManagerError(
            "Укажите проверенные адрес и SSH-отпечаток NAS в host.yaml до первого входа"
        )
    path = checked_record_path(name, data_root=PATHS.data_dir)
    if not path.exists():
        if checked_record_path(name, storage=True, data_root=PATHS.data_dir).exists():
            raise InfraManagerError(
                "Назначения дисков NAS есть, но отметка SSH-доверия утрачена. "
                "Необходима проверка восстановления доверия"
            )
        return 20
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InfraManagerError("Повреждена отметка SSH-доверия NAS") from exc
    if (
        not isinstance(record, dict)
        or record.get("name") != name
        or record.get("address") != address
        or record.get("fingerprint") != fingerprint
    ):
        raise InfraManagerError(
            "Данные NAS изменились; повторный пароль автоматически запрашивать нельзя"
        )
    return 0


def operator_physical_task(
    operation: str, name: str, *, input_text: str | None = None
) -> int:
    """Передать штатное действие физической машины из PVE управляющему гостю."""

    if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
        raise InfraManagerError("Некорректное имя физического сервера")
    require_runtime_activation_idle()
    snapshot = _prepare_runtime_snapshot(operation, name, physical=True)
    argv = [
        "docker", "exec",
        *(["-i"] if input_text is not None else []),
        "--user", "1001:0",
        "-e", f"INFRA_MANAGER_COLOR={'1' if console._color(chr(27)) else '0'}",
        "-e", f"INFRA_PVE_NODE={_pve_node()}",
        RUNTIME_CONTAINER,
        "sh", "-eu", "-c", RUNTIME_PHYSICAL_SCRIPT,
        "infra-manager-host", snapshot, operation, name,
    ]
    result = run(
        argv, check=False, stream_output=True, input_text=input_text,
    )
    if result.returncode:
        console.error(
            f"Операция {operation} для физического сервера {name} не выполнена "
            f"(код {result.returncode})"
        )
    return result.returncode


def _operator_target(value: str, operation: str, *, show_secrets: bool) -> int:
    """Направить числовую цель гостю, имя — физическому серверу."""

    if value.isdecimal():
        vmid = int(value)
        if operation == "status":
            return operator_guest_status(vmid, show_secrets=show_secrets)
        return operator_guest_task(operation, vmid, show_secrets=show_secrets)
    if operation == "sync":
        raise InfraManagerError(
            "Синхронизация физического сервера не предусмотрена; используйте deploy"
        )
    return operator_physical_task(operation, value)


def operator_repair(*, show_secrets: bool) -> int:
    """Безопасно восстановить службы управляющего гостя."""

    node = _pve_node()

    console.info("Проверка сохранности постоянного состояния")
    preflight_recovery_contour(node)

    console.info("Запуск Docker")
    run(["systemctl", "start", "docker"])

    for required in (COMPOSE_FILE, VERSIONS_FILE):
        if not required.is_file() or required.stat().st_size == 0:
            raise InfraManagerError(
                f"Отсутствует {required}; используйте infra-manager recover на PVE"
            )

    console.info("Запуск OpenBao")
    run(
        [
            "docker",
            "compose",
            "--env-file",
            str(VERSIONS_FILE),
            "-f",
            str(COMPOSE_FILE),
            "up",
            "-d",
            "openbao",
        ]
    )

    console.info("Безопасное восстановление OpenBao через PVE-only контур")
    repair_openbao_on_host(node)

    if not ACTIVATE_RUNTIME.is_file():
        raise InfraManagerError(
            f"Отсутствует {ACTIVATE_RUNTIME}; используйте infra-manager recover на PVE"
        )

    console.info("Активация управляющей среды")
    environment = os.environ.copy()
    environment["INFRA_PROJECT_BRANCH"] = project_branch_for_checkout(
        PATHS.repo_root
    )
    environment["INFRA_PVE_NODE"] = node
    run([str(ACTIVATE_RUNTIME)], env=environment)

    return operator_status(show_secrets=show_secrets)


def build_parser() -> argparse.ArgumentParser:
    """Описать разрешённые команды и скрытый доверенный режим с PVE."""
    parser = argparse.ArgumentParser(
        prog="infra-manager",
        description="Управление инфраструктурой через управляющий гость.",
    )
    parser.add_argument(
        "--trusted-pve",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "guests",
        help="показать поддерживаемых гостей и их состояние",
    )
    subparsers.add_parser(
        "hosts",
        help="показать физические серверы",
    )
    preparer = subparsers.add_parser("storage-prepare", help=argparse.SUPPRESS)
    preparer.add_argument("name")
    state = subparsers.add_parser("bootstrap-state", help=argparse.SUPPRESS)
    state.add_argument("name")
    bootstrap = subparsers.add_parser("bootstrap", help=argparse.SUPPRESS)
    bootstrap.add_argument("name")
    plan_parser = subparsers.add_parser(
        "storage-plan",
        help="предложить назначение дисков NAS без изменений",
    )
    plan_parser.add_argument("name")

    inspect_parser = subparsers.add_parser(
        "inspect",
        help="проверить оборудование физического сервера (без изменений)",
    )
    inspect_parser.add_argument("name", type=str)

    status_parser = subparsers.add_parser(
        "status",
        help="проверить управляющий контур или выбранного гостя",
    )
    status_parser.add_argument(
        "vmid",
        nargs="?",
        type=str,
        help="VMID гостя или имя физического сервера",
    )

    for command, help_text in (
        ("deploy", "развернуть или обновить гостя"),
        ("sync", "синхронизировать гостя"),
    ):
        command_parser = subparsers.add_parser(command, help=help_text)
        command_parser.add_argument("vmid", type=str, help="VMID гостя или имя физического сервера")

    repair_parser = subparsers.add_parser(
        "repair",
        help="исправить управляющий контур или выбранного гостя",
    )
    repair_parser.add_argument(
        "vmid",
        nargs="?",
        type=str,
        help="VMID гостя или имя физического сервера",
    )

    subparsers.add_parser(
        "recover",
        help="аварийное восстановление выполняется оболочкой на PVE",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Выполнить команду оператора и обработать ожидаемые ошибки.

    Доверенный режим выводит секреты только по явному флагу PVE;
    при прерывании предлагается проверить фактическое состояние гостя."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        _require_root()
        show_secrets = bool(args.trusted_pve)

        if args.command == "guests":
            return list_local_guests(PATHS.repo_root)
        if args.command == "hosts":
            return operator_physical_task("hosts", "all")
        if args.command in {"bootstrap", "bootstrap-state"}:
            if not show_secrets:
                raise InfraManagerError("Начальное доверие доступно только из терминала PVE")
            if args.command == "bootstrap-state":
                return operator_physical_bootstrap_state(args.name)
            payload = sys.stdin.readline(16385)
            if not payload or len(payload) > 16384 or not payload.endswith("\n"):
                raise InfraManagerError("Некорректный ввод учётных данных NAS")
            return operator_physical_task("bootstrap", args.name, input_text=payload)
        if args.command == "storage-prepare":
            if not show_secrets:
                raise InfraManagerError("Подготовка дисков разрешена только с доверенного PVE")
            payload = sys.stdin.readline(2048)
            if not payload or len(payload) > 2047 or not payload.endswith("\n"):
                raise InfraManagerError("Подготовка HDD требует отдельного подтверждения")
            return operator_physical_task("storage-prepare", args.name, input_text=payload)
        if args.command == "storage-plan":
            return operator_physical_task("storage-plan", args.name)
        if args.command == "inspect":
            return operator_physical_task("inspect", args.name)
        if args.command == "status":
            if args.vmid is None:
                return operator_status(show_secrets=show_secrets)
            return _operator_target(
                args.vmid, "status", show_secrets=show_secrets,
            )
        if args.command == "repair":
            if args.vmid is None:
                return operator_repair(show_secrets=show_secrets)
            return _operator_target(
                args.vmid, "repair", show_secrets=show_secrets,
            )
        if args.command in {"deploy", "sync"}:
            return _operator_target(
                args.vmid, args.command, show_secrets=show_secrets,
            )
        if args.command == "recover":
            raise InfraManagerError(
                "infra-manager recover разрешён только на физическом PVE"
            )
        raise InfraManagerError(f"Неизвестная команда: {args.command}")
    except (InfraManagerError, OSError) as exc:
        console.failure(exc, vmid=getattr(args, "vmid", None))
        return 1
    except KeyboardInterrupt:
        vmid = getattr(args, "vmid", None)
        command = f"infra-manager status {vmid}" if vmid is not None else "infra-manager status"
        console.warning(f"Операция остановлена пользователем. Перед повторным запуском выполните {command}.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
