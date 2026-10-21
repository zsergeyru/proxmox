"""Закрытый канал разрешённых операций для веб-панели.

Вход через существующий системный посредник; произвольные команды,
пути и параметры от веб-контейнера не принимаются.
"""

from __future__ import annotations

import json
import os
import re
import socketserver
import stat
from datetime import datetime
from pathlib import Path

import yaml

from .common import InfraManagerError, run
from .guest_catalog import (
    deployable_guests, find_guest_by_role, guest_management_address, load_manifest,
)
from .guest_operations import operation_guests
from .operation_store import OperationStore
from .pve import PveClient
from .settings import PATHS, SETTINGS

SOCKET_PATH = Path("/run/infra-web/operations.sock")
REQUEST_ID = re.compile(r"^[0-9a-f]{32}$")
ACTIONS = frozenset({"deploy", "repair", "sync", "status"})
NEEDED_PERMISSION = {
    "status": "read", "sync": "configure",
    "repair": "configure", "deploy": "configure",
}


def authorize_action(operation: str, vmid: int) -> None:
    """Проверить известное действие, VMID и права системного исполнителя."""
    if operation not in ACTIONS or not isinstance(vmid, int) or isinstance(vmid, bool):
        raise InfraManagerError("Недопустимые параметры задания")
    guests = {guest.vmid: guest for guest in operation_guests(PATHS.repo_root, operation)}
    if vmid not in guests:
        raise InfraManagerError("Операция не разрешена для выбранного гостя")
    if operation == "deploy" and guests[vmid].role == SETTINGS.infra_manager_role:
        raise InfraManagerError(
            "Самообновление управляющего гостя выполняется через PVE CLI"
        )
    owner = find_guest_by_role(PATHS.repo_root, SETTINGS.infra_manager_role)
    descriptor = PATHS.repo_root / "infrastructure/security/access.yaml"
    if descriptor.is_symlink() or not descriptor.is_file():
        raise InfraManagerError("Нет описания доступа исполнителя")
    data = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise InfraManagerError("Неверная версия описания доступа")
    permitted = any(
        isinstance(rule, dict)
        and rule.get("subject") == f"guest:{owner.vmid}"
        and rule.get("service") == "pve-api"
        and rule.get("resource") == "guests"
        and NEEDED_PERMISSION[operation] in rule.get("access", [])
        for rule in data.get("rules", [])
    )
    if not permitted:
        raise InfraManagerError("Нет разрешённого доступа к управлению гостями")


def _dispatch_storage(request: dict) -> dict:
    """Только чтение плана или запись одноразового согласия; никаких mkfs из веба."""
    if not isinstance(request, dict):
        raise InfraManagerError("Неверный запрос на подготовку SSD")
    kind = request.get("kind")
    if kind not in {"storage-plan", "storage-confirm"}:
        raise InfraManagerError("Неизвестный запрос SSD")
    required = (
        {"kind", "vmid", "request_id"}
        if kind == "storage-plan"
        else {"kind", "vmid", "request_id", "plan_id", "serial"}
    )
    from .operator import _pve_node
    from .pve import PveClient
    from .pve_backup_ssd import storage_status, approve_backup_ssd
    if set(request) != required:
        raise InfraManagerError("Недопустимые поля запроса SSD")
    owner = find_guest_by_role(PATHS.repo_root, SETTINGS.infra_manager_role)
    if (type(request["vmid"]) is not int or request["vmid"] not in (0, owner.vmid)
            or not isinstance(request["request_id"], str)
            or not REQUEST_ID.fullmatch(request["request_id"])):
        raise InfraManagerError("SSD может управлять только подтверждённый 910")
    authorize_action("status" if kind == "storage-plan" else "repair", owner.vmid)
    client = PveClient()
    node = _pve_node()
    if kind == "storage-plan":
        return {"accepted": True, "storage": storage_status(client, node)}
    job_id = approve_backup_ssd(
        client, node,
        plan_id=request["plan_id"], serial=request["serial"],
    )
    return {
        "accepted": True,
        "job_id": job_id,
        "message": "Подготовка SSD запущена; результат будет показан в infra-web",
    }



def _dispatch_history(request: dict) -> dict:
    """Отдавать через закрытый сокет только публичные поля истории заданий."""
    if (
        not isinstance(request, dict)
        or set(request) != {"kind", "vmid", "request_id"}
        or request["kind"] != "history"
        or type(request["vmid"]) is not int
        or request["vmid"] != 0
        or not isinstance(request["request_id"], str)
        or not REQUEST_ID.fullmatch(request["request_id"])
    ):
        raise InfraManagerError("Недопустимый запрос истории")
    operations = []
    for record in OperationStore().list_recent(limit=40):
        identifier = record.get("id", "")
        if not isinstance(identifier, str) or not REQUEST_ID.fullmatch(identifier):
            continue
        operations.append({
            "id": identifier,
            "kind": str(record.get("kind", ""))[:32],
            "target": record["target"] if type(record.get("target")) is int else 0,
            "phase": str(record.get("phase", ""))[:32],
            "created_at": str(record.get("created_at", ""))[:80],
        })
    return {"accepted": True, "operations": operations}



# Показываем оператору только строки стандартного формата, а не сырые журналы.
# Даже строки известных уровней отбрасываются, если похожи на секреты.
SAFE_LOG = re.compile(r"^(?:\[(?:ИНФО|ОК|ОШИБКА|ВРЕМЯ|ПРЕРВАНО|ПРЕДУПРЕЖДЕНИЕ)\]|ОШИБКА:)\s*")
PRIVATE_LOG = re.compile(
    r"(?i)(?:password|passwd|passphrase|token|secret|credential|authorization|cookie|"
    r"private.?key|api.?key|парол|токен|секрет|ключ|bearer|-----BEGIN|"
    r"[A-Za-z0-9_+/=-]{72,})"
)
ACTIVATION_LOG = Path("/var/log/infra-manager/runtime-activation.log")
MAX_LOG_FILE = 131072
MAX_LOG_ENTRIES = 50


def _read_log_tail(path: Path, limit: int = MAX_LOG_FILE) -> str:
    """Прочитать только конец обычного файла; symlink и специальные файлы запрещены."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode):
            raise InfraManagerError("Журнал не является обычным файлом")
        os.lseek(fd, max(0, meta.st_size - limit), os.SEEK_SET)
        return os.read(fd, limit).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


def _filter_log(lines: list[str]) -> list[str]:
    """Не публиковать неизвестные строки, управляющие символы и вероятные секреты."""
    result = []
    for line in lines:
        line = line.strip()
        if not SAFE_LOG.match(line) or PRIVATE_LOG.search(line):
            continue
        if any(ord(c) < 32 or ord(c) == 127 for c in line):
            continue
        result.append(line[:240])
    return result[-MAX_LOG_ENTRIES:]


def _operation_journal(operation_id: str) -> list[str]:
    """Только заранее известный systemd unit конкретного UUID, без shell."""
    try:
        outcome = run(
            ["timeout", "3s", "journalctl", "--no-pager", "--quiet",
             "--output=cat", "--lines=50",
             "--unit", f"infra-operation-{operation_id}.service"],
            capture_output=True, check=False,
        )
        if outcome.returncode:
            return []
        return _filter_log(outcome.stdout[:MAX_LOG_FILE].splitlines())
    except OSError:
        return []


def _activation_journal(operation_id: str) -> list[str]:
    """Не смешивать соседние обновления 910 в общем журнале активации."""
    try:
        content = _read_log_tail(ACTIVATION_LOG)
    except OSError:
        return []
    marker = f"[ИНФО] Операция активации: {operation_id}"
    position = content.rfind(marker)
    if position < 0:
        return []
    lines = content[position:].splitlines()
    # Следующий запуск открывает новую секцию; чужие операции не читаем.
    stop = next(
        (i for i, line in enumerate(lines[1:], 1)
         if line.startswith("[ИНФО] Операция активации: ")
         or ("Активация infra-runtime, ветка" in line)),
        len(lines),
    )
    return _filter_log(lines[:stop])


def _operation_detail(request: dict) -> dict:
    """Отдать ограниченную временную шкалу и безопасный диагностический вывод."""
    if (not isinstance(request, dict)
            or set(request) != {"kind", "vmid", "request_id", "operation_id"}
            or request.get("kind") != "operation-detail"
            or type(request.get("vmid")) is not int or request["vmid"] != 0
            or not isinstance(request.get("request_id"), str)
            or not REQUEST_ID.fullmatch(request["request_id"])
            or not isinstance(request.get("operation_id"), str)
            or not REQUEST_ID.fullmatch(request["operation_id"])):
        raise InfraManagerError("Недопустимый запрос диагностики")
    operation_id = request["operation_id"]
    store = OperationStore()
    record = store.get(operation_id)
    if record.get("id") != operation_id:
        raise InfraManagerError("История не соответствует идентификатору")
    fields = ("id", "kind", "target", "phase", "created_at", "started_at",
              "finished_at", "result", "exit_code", "activation_required")
    safe_record = {key: record.get(key) for key in fields}
    safe_record["id"] = operation_id
    if safe_record["kind"] not in ACTIONS:
        safe_record["kind"] = "неизвестно"
    if safe_record["phase"] not in {
        "queued", "running", "activating", "verifying", "completed",
        "failed", "rolled-back", "needs-recovery",
    }:
        safe_record["phase"] = "неизвестно"
    if type(safe_record["target"]) is not int or not 0 < safe_record["target"] < 1000000:
        safe_record["target"] = 0
    if safe_record["result"] not in {None, "ok", "failed", "rolled-back"}:
        safe_record["result"] = None
    if type(safe_record["exit_code"]) is not int or not 0 <= safe_record["exit_code"] <= 255:
        safe_record["exit_code"] = None
    for key in ("created_at", "started_at", "finished_at"):
        value = safe_record[key]
        if not isinstance(value, str) or len(value) > 40:
            safe_record[key] = None
        else:
            try:
                datetime.fromisoformat(value)
            except ValueError:
                safe_record[key] = None
    safe_record["activation_required"] = safe_record["activation_required"] is True
    events = []
    try:
        lines = _read_log_tail(store.logs / f"{operation_id}.jsonl", 65536).splitlines()
    except OSError:
        lines = []
    for line in lines[-64:]:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if (not isinstance(event, dict) or event.get("id") != operation_id
                or not isinstance(event.get("at"), str)
                or not isinstance(event.get("phase"), str)):
            continue
        try:
            datetime.fromisoformat(event["at"])
        except ValueError:
            continue
        if event["phase"] not in {
            "queued", "running", "activating", "verifying",
            "completed", "failed", "rolled-back", "needs-recovery",
        }:
            continue
        events.append({"at": event["at"][:40], "phase": event["phase"]})
    logs = _operation_journal(operation_id)
    if safe_record["target"] == 910 and safe_record["activation_required"] is True:
        logs.extend(_activation_journal(operation_id))
    last_error = next(
        (message for message in reversed(logs)
         if message.startswith(("[ОШИБКА]", "ОШИБКА:", "[ПРЕРВАНО]"))),
        None,
    )
    if safe_record["phase"] in {"failed", "needs-recovery", "rolled-back"}:
        diagnostic = last_error or (
            "Задание завершилось без подтверждения готовности. "
            "Полный закрытый журнал доступен администратору в 910."
        )
    elif safe_record["phase"] in {"queued", "running", "activating", "verifying"}:
        diagnostic = "Выполнение ещё не завершено."
    else:
        diagnostic = "Выполнение подтверждено." if safe_record["phase"] == "completed" else ""
    return {
        "accepted": True,
        "operation": safe_record,
        "events": events[-20:],
        "logs": logs[-MAX_LOG_ENTRIES:],
        "diagnostic": diagnostic,
    }




# Гостевые страницы не хранят свой каталог. Данные берутся из существующих
# guest.yaml/defaults.yaml, разрешения — из общего access.yaml.
MAX_GUESTS = 100
GUEST_ACTIONS = ("status", "sync", "repair", "deploy")


def _guests_catalog() -> tuple[list[dict], bool]:
    """Снимок каталога и ONE PVE API request на все VM/LXC, без N+1 запросов."""
    identities = sorted(deployable_guests(PATHS.repo_root), key=lambda guest: guest.vmid)
    if len(identities) > MAX_GUESTS:
        raise InfraManagerError("Количество гостей превышает безопасный предел")
    defaults = load_manifest(PATHS.repo_root / "infrastructure/guests/defaults.yaml")
    profiles = defaults.get("profiles", {})
    allowed = {
        kind: {item.vmid for item in operation_guests(PATHS.repo_root, kind)}
        for kind in GUEST_ACTIONS
    }
    # Проверки access.yaml делают те же функции, которые ограничивают действия.
    # Если отсутствует доступ на чтение, не публикуем список.
    if identities:
        authorize_action("status", identities[0].vmid)
    for kind in ("sync", "repair", "deploy"):
        candidates = [
            guest.vmid for guest in identities
            if guest.vmid in allowed[kind]
            and not (kind == "deploy" and guest.role == SETTINGS.infra_manager_role)
        ]
        if not candidates:
            allowed[kind] = set()
            continue
        try:
            authorize_action(kind, candidates[0])
        except InfraManagerError:
            # Отсутствующие configure-права не должны скрывать список гостей.
            allowed[kind] = set()
    # PVE недоступен -> состояние "не проверено", не ложное "выключен".
    resources: dict[int, dict] = {}
    pve_available = False
    try:
        response = PveClient().data(
            "/cluster/resources", query={"type": "vm"}, timeout=8,
        )
        if not isinstance(response, list):
            raise InfraManagerError("PVE вернул неверный список объектов")
        for item in response:
            if not isinstance(item, dict) or type(item.get("vmid")) is not int:
                continue
            vmid = item["vmid"]
            if vmid in resources:
                raise InfraManagerError("PVE вернул дубликат VMID")
            resources[vmid] = item
        pve_available = True
    except (InfraManagerError, OSError, TypeError, ValueError):
        resources = {}
    guests = []
    for identity in identities:
        profile = identity.source.get("profile")
        profile_data = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
        guest_type = profile_data.get("type") if isinstance(profile_data, dict) else None
        if guest_type not in {"lxc", "vm"}:
            guest_type = "unknown"
        try:
            address = guest_management_address(PATHS.repo_root, identity)
        except InfraManagerError:
            address = None
        if not isinstance(address, str) or len(address) > 45:
            address = None
        resource = resources.get(identity.vmid)
        if not pve_available:
            state = "unavailable"
        elif resource is None:
            state = "absent"
        elif guest_type == "unknown":
            state = "unknown"
        elif (resource.get("name") != identity.name
              or resource.get("type") != ("qemu" if guest_type == "vm" else guest_type)):
            state = "conflict"
        elif resource.get("status") in {"running", "stopped", "paused"}:
            state = resource["status"]
        else:
            state = "unknown"
        actions = [
            kind for kind in GUEST_ACTIONS
            if identity.vmid in allowed[kind]
            and not (kind == "deploy" and identity.role == SETTINGS.infra_manager_role)
        ]
        guests.append({
            "vmid": identity.vmid,
            "name": identity.name[:80],
            "role": (identity.role or "")[:80],
            "type": guest_type,
            "address": address,
            "state": state,
            "actions": actions,
        })
    return guests, pve_available


def _dispatch_guests(request: dict) -> dict:
    """Обзор либо карточка только известного VMID."""
    if (not isinstance(request, dict)
            or set(request) != {"kind", "vmid", "request_id"}
            or request.get("kind") not in {"guests", "guest-detail"}
            or type(request.get("vmid")) is not int
            or not isinstance(request.get("request_id"), str)
            or not REQUEST_ID.fullmatch(request["request_id"])):
        raise InfraManagerError("Некорректный запрос каталога гостей")
    if request["kind"] == "guests":
        if request["vmid"] != 0:
            raise InfraManagerError("Список гостей не принимает VMID")
    elif not 1 <= request["vmid"] <= 999999:
        raise InfraManagerError("Некорректный VMID гостя")
    guests, pve_available = _guests_catalog()
    if request["kind"] == "guests":
        return {"accepted": True, "guests": guests, "pve_available": pve_available}
    guest = next((row for row in guests if row["vmid"] == request["vmid"]), None)
    if guest is None:
        raise InfraManagerError("Гость отсутствует в каталоге проекта")
    try:
        records = OperationStore().list_recent(limit=100)
        history_available = True
    except (InfraManagerError, OSError):
        records = []
        history_available = False
    operations = []
    for record in records:
        if type(record.get("target")) is not int or record["target"] != guest["vmid"]:
            continue
        operation_id = record.get("id", "")
        if not isinstance(operation_id, str) or not REQUEST_ID.fullmatch(operation_id):
            continue
        operations.append({
            "id": operation_id,
            "kind": str(record.get("kind", ""))[:32],
            "phase": str(record.get("phase", ""))[:32],
            "created_at": str(record.get("created_at", ""))[:40],
        })
        if len(operations) >= 20:
            break
    return {
        "accepted": True, "guest": guest, "operations": operations,
        "history_available": history_available, "pve_available": pve_available,
    }




BACKUP_REFUSALS = {
    "permission_denied": "Нет разрешения на запуск резервирования для гостя 910",
    "media_not_ready": "SSD или NAS не подтверждены для резервирования. Проверьте подготовку SSD и настройки NAS",
    "already_running": "Резервирование уже выполняется. Дождитесь завершения текущего задания",
    "systemd_unavailable": "Не удалось проверить системную службу резервирования",
    "service_missing": "Служба infra-manager-backup.service отсутствует или недоступна. Требуется проверка deploy 910",
    "start_failed": "systemd отказал в запуске резервирования. Проверьте журнал службы на госте 910",
}


def _backup_refusal(code: str) -> dict:
    """Только известные безопасные причины, без stderr и секретов из PVE."""
    return {"accepted": False, "code": code, "message": BACKUP_REFUSALS[code]}


def _dispatch_backup(request: dict) -> dict:
    """Чтение статуса и явный запуск утверждённого systemd unit."""
    if (not isinstance(request, dict)
            or set(request) != {"kind", "vmid", "request_id"}
            or request.get("kind") not in {"backup-overview", "backup-run"}
            or type(request.get("vmid")) is not int or request["vmid"] != 0
            or not isinstance(request.get("request_id"), str)
            or not REQUEST_ID.fullmatch(request["request_id"])):
        raise InfraManagerError("Некорректный запрос резервирования")
    owner = find_guest_by_role(PATHS.repo_root, SETTINGS.infra_manager_role)
    kind = request["kind"]
    if kind == "backup-overview":
        authorize_action("status", owner.vmid)
        from .backup_overview import snapshot
        return {"accepted": True, "overview": snapshot()}
    try:
        authorize_action("repair", owner.vmid)
    except InfraManagerError:
        return _backup_refusal("permission_denied")
    from .backup_controller import policy
    try:
        _description, confirmed = policy()
    except (InfraManagerError, OSError, ValueError):
        return _backup_refusal("media_not_ready")
    if not confirmed:
        return _backup_refusal("media_not_ready")
    try:
        busy = run(
            ["timeout", "3s", "systemctl", "is-active", "--quiet",
             "infra-manager-backup.service"],
            capture_output=True, check=False,
        )
    except OSError:
        return _backup_refusal("systemd_unavailable")
    if busy.returncode == 0:
        return _backup_refusal("already_running")
    if busy.returncode not in {3, 4}:
        return _backup_refusal("systemd_unavailable")
    # systemctl is-active 3 также возвращает для отсутствующего unit.
    # Проверяем unit до запуска, чтобы избежать ложного "запрос принят".
    try:
        loaded = run(
            ["timeout", "3s", "systemctl", "show",
             "infra-manager-backup.service", "--property=LoadState"],
            capture_output=True, check=False,
        )
    except OSError:
        return _backup_refusal("systemd_unavailable")
    if loaded.returncode != 0:
        return _backup_refusal("systemd_unavailable")
    state = dict(
        item.split("=", 1) for item in loaded.stdout.splitlines()
        if item.startswith("LoadState=") and len(item) <= 80
    ).get("LoadState")
    if state != "loaded":
        return _backup_refusal("service_missing")
    try:
        started = run(
            ["timeout", "5s", "systemctl", "start", "--no-block",
             "infra-manager-backup.service"],
            capture_output=True, check=False,
        )
    except OSError:
        return _backup_refusal("systemd_unavailable")
    if started.returncode:
        return _backup_refusal("start_failed")
    return {
        "accepted": True,
        "message": "systemd принял запуск. Успех копирования пока не подтверждён",
    }



def dispatch(request: dict) -> dict:
    """Запустить только фиксированную системную задачу с неизменяемым UUID."""
    if isinstance(request, dict) and request.get("kind") in (
        "storage-plan", "storage-confirm",
    ):
        return _dispatch_storage(request)
    if isinstance(request, dict) and request.get("kind") == "history":
        return _dispatch_history(request)
    if isinstance(request, dict) and request.get("kind") == "operation-detail":
        return _operation_detail(request)
    if isinstance(request, dict) and request.get("kind") in {"guests", "guest-detail"}:
        return _dispatch_guests(request)
    if isinstance(request, dict) and request.get("kind") in {"backup-overview", "backup-run"}:
        return _dispatch_backup(request)
    if not isinstance(request, dict) or set(request) != {"kind", "vmid", "request_id"}:
        raise InfraManagerError("Неверный формат запроса")
    kind, vmid, operation_id = (
        request["kind"], request["vmid"], request["request_id"]
    )
    if (kind not in ACTIONS or not isinstance(vmid, int)
            or isinstance(vmid, bool) or vmid < 1):
        raise InfraManagerError("Недопустимое действие или VMID")
    if not isinstance(operation_id, str) or not REQUEST_ID.fullmatch(operation_id):
        raise InfraManagerError("Некорректный идентификатор запроса")
    authorize_action(kind, vmid)
    store = OperationStore()
    existing_path = store.history / f"{operation_id}.json"
    if existing_path.exists() or existing_path.is_symlink():
        old = store.get(operation_id)
        if old["kind"] != kind or old["target"] != vmid:
            raise InfraManagerError("Идентификатор уже используется другим заданием")
        return {"id": operation_id, "state": old["phase"], "accepted": True}
    # Никакой оболочки, пользовательского argv, произвольного окружения.
    command = [
        "systemd-run", "--collect",
        "--unit=infra-operation-" + operation_id,
        "--setenv=PYTHONPATH=/usr/local/lib/infra-manager",
        "/usr/bin/python3", "-m", "infra_manager.operation_runner",
        "run", kind, str(vmid), "--request-id", operation_id,
    ]
    proc = run(command, check=False, capture_output=True)
    if proc.returncode:
        raise InfraManagerError(
            "Не удалось создать задание systemd; проверьте состояние и идентификатор"
        )
    return {"id": operation_id, "state": "queued", "accepted": True}


class SocketHandler(socketserver.StreamRequestHandler):
    """Построчный JSON с предельной длиной и безопасным ответом."""

    def handle(self) -> None:
        raw = self.rfile.readline(2049)
        try:
            if len(raw) > 2048 or not raw.endswith(b"\n"):
                raise InfraManagerError("Запрос превышает разрешённый размер")
            parsed = json.loads(raw)
            result = dispatch(parsed)
        except (InfraManagerError, ValueError, TypeError):
            result = {"accepted": False, "error": "Действие отклонено"}
        self.wfile.write((json.dumps(result, ensure_ascii=False) + "\n").encode())


class AdminSocketServer(socketserver.ThreadingUnixStreamServer):
    """Исполнитель работает внутри существующего gateway, а не в веб-контейнере."""

    daemon_threads = True

    def __init__(self, path: Path = SOCKET_PATH) -> None:
        parent = path.parent
        if parent.is_symlink() or not parent.is_dir():
            raise InfraManagerError("Отсутствует закрытый каталог сокета")
        if path.exists() or path.is_symlink():
            if path.is_symlink() or not stat.S_ISSOCK(path.lstat().st_mode):
                raise InfraManagerError("Небезопасный путь сокета")
            path.unlink()
        super().__init__(str(path), SocketHandler)
        os.chmod(path, 0o660)

    def server_close(self) -> None:
        path = Path(self.server_address)
        super().server_close()
        if path.is_socket() and not path.is_symlink():
            path.unlink()
