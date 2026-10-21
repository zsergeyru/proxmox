"""Безопасный read-only обзор дисков и резервных копий для infra-web.

Источник истины: storage.yaml/backup.yaml, приватное состояние backup_controller
и systemd. Каталог не является подтверждением физических подключений и не
разрешает запись, инициализацию дисков или восстановление.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .common import InfraManagerError, run
from .guest_catalog import load_manifest
from .operation_store import OperationStore, _read
from .settings import PATHS
from .storage_catalog import load_physical_storage
from .volume_backup import load_jobs

DESTINATIONS = {
    "fast-ssd": ("/mnt/pve/backup-fast", "ext4", "infra-core"),
    "nas-backup": ("/mnt/pve/nas-backup", "nfs4", "pve-infra-core"),
}
# Идентификатор PVE storage отличается от ID назначения restic для SSD.
PVE_STORAGE_IDS = {"backup-fast": "fast-ssd", "nas-backup": "nas-backup"}
RECORD_NAMES = {
    "core": "backup-last-core.json",
    "full": "backup-last-full.json",
    "vm100": "backup-vm100.json",
}
MAX_CORE_AGE_HOURS = 9
MAX_VM100_AGE_DAYS = 1


def _record(store: OperationStore, name: str) -> tuple[dict | None, str]:
    """Отличать отсутствие записи от повреждённой; не читать ссылки и чужие пути."""
    path = store.settings / RECORD_NAMES[name]
    if not path.exists() and not path.is_symlink():
        return None, "missing"
    try:
        return _read(path), "present"
    except InfraManagerError:
        return None, "invalid"


def _targets(value: Any) -> list[str]:
    if (not isinstance(value, list) or not value
            or any(not isinstance(x, str) or x not in DESTINATIONS for x in value)
            or len(value) != len(set(value))):
        raise ValueError("Неизвестное место резервирования")
    return sorted(value)


def _backup_record(name: str, raw: dict | None, state: str, now: datetime) -> dict:
    result: dict[str, Any] = {
        "state": state, "created_utc": None, "destinations": [],
        "check": "not_confirmed", "restore_tested": False,
    }
    if state != "present" or raw is None:
        return result
    try:
        if name == "vm100":
            stamp = date.fromisoformat(raw["date"])
            if raw.get("verified") is not True:
                raise ValueError("VM 100 не подтверждена")
            targets = _targets(raw["destinations"])
            age = (now.date() - stamp).days
            if not 0 <= age <= 36500:
                raise ValueError("Некорректное время VM 100")
            result.update(
                created_utc=stamp.isoformat(), destinations=targets,
                check="backup_checked", state="verified" if age <= MAX_VM100_AGE_DAYS else "stale",
            )
        else:
            if raw.get("schema_version") != 1:
                raise ValueError("Неверная версия записи")
            stamp = datetime.fromisoformat(raw["created_utc"])
            if stamp.tzinfo is None:
                raise ValueError("Нет UTC")
            age = (now - stamp).total_seconds() / 3600
            if not -.5 <= age <= 24 * 36500:
                raise ValueError("Некорректное время")
            targets = _targets(raw["destinations"])
            if name == "core":
                if (raw.get("verified") != "restic-check-read-data-subset-1-percent"
                        or raw.get("includes_vm100") is not False):
                    raise ValueError("Не подтверждена выборочная проверка core")
                check = "restic_subset_1_percent"
            else:
                if raw.get("includes_vm100") is not True:
                    raise ValueError("Копия не содержит VM 100")
                check = "backup_checked"
            result.update(
                created_utc=stamp.astimezone(UTC).isoformat(timespec="seconds"),
                destinations=targets, check=check,
                state="verified" if age <= MAX_CORE_AGE_HOURS else "stale",
                restore_tested=raw.get("restore_tested") is True,
            )
    except (KeyError, TypeError, ValueError, OverflowError):
        result["state"] = "invalid"
        result["created_utc"] = None
        result["destinations"] = []
        result["check"] = "not_confirmed"
        result["restore_tested"] = False
    return result


# Никаких сырых журналов из PVE/OpenBao в HTTP API. Распознаём только
# известные формулировки собственного backup_controller.
# Причины, которые выдаёт контроллер из закрытого словаря, а не из сырого stderr.
# Сначала распознаём причину, потом общее имя этапа restic.
BACKUP_CAUSES = {
    "причина: на носителе закончилось место": "На носителе резервирования закончилось место",
    "причина: файловая система доступна только для чтения": "Хранилище доступно только для чтения",
    "причина: нет прав на файл или каталог": "Нет прав на запись или чтение хранилища",
    "причина: не подходит пароль существующего хранилища": "Пароль Restic не подходит к существующему архиву",
    "причина: хранилище заблокировано другой операцией": "Restic: хранилище занято другой операцией",
    "причина: ошибка SSH/SFTP-соединения": "Ошибка SSH/SFTP при записи архива",
    "причина: недоступен кеш restic": "Restic не может использовать кеш",
    "причина: не удалось прочитать входные данные": "Restic не смог прочитать исходные данные",
    "причина: PVE не смог создать снимок OpenBao Raft": "PVE: снимок OpenBao Raft не создан",
    "причина: PVE не смог собрать TAR-архив": "PVE: не удалось собрать TAR-архив",
    "причина: PVE не смог завершить снимок VM 100": "PVE: снимок VM 100 не завершён",
    "причина: SSH-соединение с PVE прервано": "Оборвалось SSH-соединение при экспорте PVE",
    "причина: PVE не подтвердил полный поток снимка": "PVE не завершил передачу архива",
}


CHECK_FAILURES = {
    "PVE: не подготовлен unseal OpenBao":
        "На PVE отсутствует обязательный unseal OpenBao",
    "PVE: не подготовлен аварийный GitHub-ключ":
        "На PVE отсутствует аварийный GitHub-ключ для восстановления",
    "PVE: не подготовлен AppRole снимка OpenBao":
        "На PVE не подготовлен AppRole для снимка OpenBao",
    "PVE: не подготовлен помощник Raft-снимка":
        "На PVE отсутствует помощник снимка OpenBao Raft",
    "PVE: не подтверждена идентичность гостя 910":
        "На PVE отсутствует описание управляющего гостя 910",
    "PVE: не хватает обязательного PVE-only компонента резервирования":
        "Не хватает обязательного PVE-only компонента резервирования",
    "PVE отверг проверку источников или носителей резервирования":
        "PVE отверг проверку носителя или обязательных данных",
    "PVE не подтвердил действие резервирования: check":
        "PVE отверг проверку носителя или обязательных PVE-only данных",
    "Проверка или запись restic в fast-ssd завершилась с ошибкой":
        "На SSD произошла ошибка restic; этап в старом журнале не уточнён",
    "restic: создание репозитория на fast-ssd завершилась ошибкой":
        "restic не создал репозиторий на SSD",
    "restic: проверка доступа к репозиторию на fast-ssd завершилась ошибкой":
        "restic не смог открыть репозиторий на SSD",
    "restic: запись архива на fast-ssd завершилась ошибкой":
        "restic не смог записать архив на SSD",
    "restic: проверка целостности архива на fast-ssd завершилась ошибкой":
        "restic не смог завершить проверку архива на SSD",
    "restic: создание репозитория на nas-backup завершилась ошибкой":
        "restic не создал репозиторий на NAS",
    "restic: проверка доступа к репозиторию на nas-backup завершилась ошибкой":
        "restic не смог открыть репозиторий на NAS",
    "restic: запись архива на nas-backup завершилась ошибкой":
        "restic не смог записать архив на NAS",
    "restic: проверка целостности архива на nas-backup завершилась ошибкой":
        "restic не смог завершить проверку архива на NAS",
}


def _backup_failure_hint() -> str | None:
    """Только закрытая классификация последних записей собственного systemd unit."""
    try:
        command = run(
            ["timeout", "3s", "journalctl", "-u", "infra-manager-backup.service",
             "--no-pager", "--output=cat", "--lines=30"],
            capture_output=True, check=False,
        )
        if command.returncode or len(command.stdout) > 16000:
            return None
        lines = command.stdout.splitlines()
        for entry in reversed(lines):
            if not entry.startswith("[ОЖИДАНИЕ] Резервирование: "):
                continue
            for needle, message in BACKUP_CAUSES.items():
                if needle in entry:
                    return message
            for needle, message in CHECK_FAILURES.items():
                if needle in entry:
                    return message
            if "Нет доступного и проверенного места резервирования" in entry:
                return "Резервирование не выполнено: PVE не подтвердил носитель или источники"
            return "Служба отклонила резервирование; откройте журнал для уточнения"
    except (OSError, ValueError, TypeError):
        pass
    return None


def _timer() -> dict:
    """Состояние реального systemd timer/service, а не одна декларация YAML."""
    result = {"state": "unknown", "service": "unknown", "next": None, "last": None}
    try:
        command = run(
            ["timeout", "3s", "systemctl", "show", "infra-manager-backup.timer",
             "--property=ActiveState,NextElapseUSecRealtime,LastTriggerUSec"],
            capture_output=True, check=False,
        )
        if command.returncode == 0:
            fields = dict(
                line.split("=", 1) for line in command.stdout.splitlines()
                if "=" in line and len(line) <= 110
            )
            if fields.get("ActiveState") in {"active", "inactive", "failed"}:
                result["state"] = fields["ActiveState"]
            for source, target in (
                ("NextElapseUSecRealtime", "next"), ("LastTriggerUSec", "last"),
            ):
                value = fields.get(source, "")
                if value and value != "n/a" and len(value) <= 90:
                    result[target] = value
        active = run(
            ["timeout", "3s", "systemctl", "show", "infra-manager-backup.service",
             "--property=ActiveState,Result"],
            capture_output=True, check=False,
        )
        if active.returncode == 0:
            fields = dict(
                line.split("=", 1) for line in active.stdout.splitlines()
                if "=" in line and len(line) <= 80
            )
            if fields.get("ActiveState") in {"activating", "active", "inactive", "failed"}:
                result["service"] = fields["ActiveState"]
            if fields.get("Result") in {"success", "exit-code", "timeout", "signal"}:
                result["last_result"] = fields["Result"]
                if fields["Result"] in {"exit-code", "timeout", "signal"}:
                    hint = _backup_failure_hint()
                    if hint:
                        result["failure_hint"] = hint
    except OSError:
        pass
    return result



def _volume_record(store: OperationStore, name: str, now: datetime) -> dict:
    """Показать историю по назначению; не выдавать вчерашнюю копию за свежую."""
    from .operation_store import _read
    empty = {
        "state": "missing", "created_utc": None, "destinations": [],
        "check": "not_confirmed", "restore_tested": False,
        "destination_status": {name: "missing" for name in DESTINATIONS},
        "destination_dates": {},
    }
    file = store.settings / ("backup-volume-" + name + ".json")
    if not file.exists() and not file.is_symlink():
        return empty
    try:
        record = _read(file)
        if record.get("schema_version") != 1 or record.get("volume_id") != name:
            raise ValueError("Неверная запись тома")
        dates = record["destination_dates"]
        if (not isinstance(dates, dict)
                or set(dates) - set(DESTINATIONS)
                or not all(isinstance(x, str) for x in dates.values())):
            raise ValueError("Неизвестные назначения тома")
        today = now.date()
        status = {}
        for target in DESTINATIONS:
            if target not in dates:
                status[target] = "missing"
            else:
                old = date.fromisoformat(dates[target])
                age = (today - old).days
                if not 0 <= age <= 36500:
                    raise ValueError("Некорректная дата")
                status[target] = "verified" if age <= 1 else "stale"
        good = [x for x in dates if status[x] == "verified"]
        state = ("verified" if len(good) == len(DESTINATIONS) else
                 "partial" if good else
                 "stale" if dates else "missing")
        return {
            "state": state, "created_utc": record.get("created_utc"),
            "destinations": sorted(good), "destination_status": status,
            "destination_dates": dates,
            "check": "restic_subset_1_percent" if good else "not_confirmed",
            "restore_tested": record.get("restore_tested") is True,
        }
    except (InfraManagerError, OSError, ValueError, TypeError, KeyError):
        return {**empty, "state": "invalid"}


def snapshot() -> dict:
    """Один ограниченный срез для уже авторизованной веб-панели."""
    repo = PATHS.repo_root
    storage = load_physical_storage(repo, "pve")
    nas = load_physical_storage(repo, "nas")
    backup_path = repo / "infrastructure/hosts/pve/backup.yaml"
    if backup_path.is_symlink():
        raise InfraManagerError("Недоверенный backup.yaml")
    policy = load_manifest(backup_path)
    if (policy.get("schema_version") != 1 or policy.get("host") != "pve"
            or policy.get("job") != "infra-core"
            or not isinstance(policy.get("schedule"), str)
            or len(policy["schedule"]) > 40):
        raise InfraManagerError("Некорректное описание резервирования")

    declared = policy.get("destinations")
    if (not isinstance(declared, list) or len(declared) != 2
            or any(not isinstance(x, dict) or x.get("id") not in DESTINATIONS
                   for x in declared)
            or {x["id"] for x in declared} != set(DESTINATIONS)):
        raise InfraManagerError("Неоднозначные места для резервирования")

    from .pve_backup_ssd import _read_state, _state_path, _read_job
    try:
        saved = _read_state(_state_path())
        job = _read_job()
    except InfraManagerError:
        saved = None
        job = None
        ssd_error = True
    else:
        ssd_error = False

    # Только read-only проверка Proxmox. PVE active не доказывает, что
    # сейчас смонтирован именно подтверждённый UUID/экспорт restic.
    pve_storages: dict[str, dict] = {}
    pve_api_available = False
    try:
        from .operator import _pve_node
        from .pve import PveClient
        node = _pve_node()
        response = PveClient().data(f"/nodes/{node}/storage", timeout=8)
        if not isinstance(response, list):
            raise InfraManagerError("PVE вернул некорректные storage")
        for item in response:
            if not isinstance(item, dict):
                continue
            identity = item.get("storage")
            if isinstance(identity, str) and identity in PVE_STORAGE_IDS:
                destination = PVE_STORAGE_IDS[identity]
                if destination in pve_storages:
                    raise InfraManagerError("PVE вернул дубликат хранилища")
                pve_storages[destination] = item
        pve_api_available = True
    except (InfraManagerError, OSError, ValueError, TypeError):
        pve_storages = {}

    targets: list[dict] = []
    for entry in declared:
        name = entry["id"]
        mount, fs, repository = DESTINATIONS[name]
        if (entry.get("mountpoint"), entry.get("filesystem"),
                entry.get("repository")) != (mount, fs, repository):
            raise InfraManagerError("Назначение резервирования не соответствует договору")
        configured = bool(
            entry.get("uuid") or entry.get("source")
            or (name == "fast-ssd" and saved)
        )
        observed = pve_storages.get(name)
        pve_state = (
            "not_checked" if not pve_api_available else
            "not_registered" if observed is None else
            "reported_active" if observed.get("active") in (1, True) else
            "reported_inactive" if observed.get("active") in (0, False) else
            "unknown"
        )
        targets.append({
            "id": name, "mountpoint": mount, "filesystem": fs,
            "configured": configured,
            "pve_state": pve_state,
            # PVE active не равнозначно проверенному физическому mount/UUID.
            "mount_check": "not_checked",
        })

    volumes = []
    paths = {x["volume"]: x["path"] for x in storage.mounts}
    for vol in storage.volumes:
        volumes.append({
            "id": vol["id"], "kind": vol["kind"], "size_gb": vol.get("size_gb"),
            "mountpoint": paths[vol["id"]], "mount_check": "not_checked",
        })

    nas_volumes = []
    nas_paths = {x["volume"]: x["path"] for x in nas.mounts}
    for vol in nas.volumes:
        nas_volumes.append({
            "id": vol["id"], "kind": vol["kind"],
            "mountpoint": nas_paths[vol["id"]],
            "configured": nas_paths[vol["id"]] is not None
                and (vol.get("filesystem_uuid") or vol.get("pool_guid")) is not None,
            "mount_check": "not_checked",
        })

    store = OperationStore()
    now = datetime.now(UTC)
    backups = {
        name: _backup_record(name, *_record(store, name), now)
        for name in RECORD_NAMES
    }
    volume_jobs = load_jobs(repo, policy)
    for job in volume_jobs:
        backups["volume_" + job["id"]] = _volume_record(store, job["id"], now)
    core = backups["core"]
    copies = len(core["destinations"])
    if core["state"] in {"missing", "invalid"}:
        overall = "no_confirmed_backup"
    elif core["state"] == "stale":
        overall = "stale"
    elif copies == 1:
        overall = "single_copy"
    else:
        overall = "two_recorded_copies"

    return {
        "storage": {"host": "pve", "volumes": volumes},
        "nas": {"host": "nas", "volumes": nas_volumes, "observed": False},
        "destinations": targets,
        "pve_api_available": pve_api_available,
        "ssd": {
            "state": "unknown" if ssd_error else
                "running" if job and job.get("state") in {"queued", "running"} else
                "failed" if job and job.get("state") == "failed" else
                "configured" if saved else "not_configured",
        },
        "backup": {
            "schedule_declared": policy["schedule"],
            "timer": _timer(),
            "volume_jobs": [
                {"id": item["id"], "size_gb": storage.volume(item["id"])["size_gb"],
                 "record_key": "volume_" + item["id"], "schedule": "daily"}
                for item in volume_jobs
            ],
            "records": backups,
            "overall": overall,
            "independent_recorded_copies": copies,
            "restore_tested": core["restore_tested"]
                and backups["full"]["restore_tested"],
            "live_media_verified": False,
        },
    }
