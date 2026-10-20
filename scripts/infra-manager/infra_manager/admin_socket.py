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
from pathlib import Path

import yaml

from .common import InfraManagerError, run
from .guest_catalog import find_guest_by_role
from .guest_operations import operation_guests
from .operation_store import OperationStore
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


def dispatch(request: dict) -> dict:
    """Запустить только фиксированную системную задачу с неизменяемым UUID."""
    if isinstance(request, dict) and request.get("kind") in (
        "storage-plan", "storage-confirm",
    ):
        return _dispatch_storage(request)
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
