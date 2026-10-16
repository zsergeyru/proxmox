"""Отдельный HTTPS-интерфейс Homarr с машинными ответами и заданиями.

Источник истины: каталог проекта и штатная команда infra-manager.
Homepage продолжает обслуживаться прежним portal_gateway, его не меняем.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import secrets
import ssl
import threading
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .common import CommandRunner, InfraManagerError
from .guest_catalog import guest_identity, guest_management_address, guests_by_provision_section
from .guest_operations import operation_guests
from .homarr_inventory import read_guest_inventory
from .settings import PATHS

API_DIR = Path("/mnt/persistent-state/homarr-api")
OPERATOR = Path("/usr/local/sbin/infra-manager")
READ_KEY = API_DIR / "read-token"
WRITE_KEY = API_DIR / "action-token"
JOBS_DIR = API_DIR / "jobs"
MAX_BODY = 2048
MAX_LOG_BYTES = 8192
ALLOWED_OPERATIONS = frozenset({"deploy", "status", "sync", "repair"})
JOB_ID = re.compile(r"^[a-f0-9]{32}$")
UNIT_PREFIX = "infra-homarr-"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_keys(directory: Path = API_DIR) -> None:
    """Создать ключи однократно; никогда не перезаписывать существующие."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    (directory / "jobs").mkdir(exist_ok=True)
    (directory / "jobs").chmod(0o700)
    for name in ("read-token", "action-token"):
        path = directory / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if not path.is_file() or path.is_symlink() or not path.read_text().strip():
                raise InfraManagerError(f"Пустой или неподходящий ключ API: {path}")
            continue
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(secrets.token_hex(32) + "\n")
    # Сохраняемые секреты не должны попадать в вывод проверки состояния.


def _key_matches(authorization: str, path: Path) -> bool:
    if not authorization.startswith("Bearer "):
        return False
    try:
        expected = path.read_text(encoding="ascii").strip()
    except OSError:
        return False
    received = authorization.removeprefix("Bearer ").strip()
    return bool(expected) and hmac.compare_digest(expected, received)


def allowed_client(address: str, repo_root: Path) -> bool:
    """Разрешить только 140 и локальный диагностический доступ."""
    try:
        remote = ipaddress.ip_address(address)
        if remote.is_loopback:
            return True
        portals = guests_by_provision_section(repo_root, "web_gateway")
        if len(portals) != 1:
            return False
        expected = guest_management_address(repo_root, portals[0])
        return bool(expected) and remote == ipaddress.ip_address(expected)
    except (ValueError, InfraManagerError):
        return False


def _write_json(path: Path, data: dict) -> None:
    """Не публиковать половину записи при аварийном завершении."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read_job(job_id: str) -> dict:
    if not JOB_ID.fullmatch(job_id):
        raise InfraManagerError("Некорректный номер задания")
    path = JOBS_DIR / f"{job_id}.json"
    if not path.is_file():
        raise FileNotFoundError(job_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("id") != job_id:
        raise InfraManagerError("Повреждена запись задания")
    return payload


def _unit_state(unit_name: str) -> dict[str, str]:
    result = CommandRunner().run(
        ["systemctl", "show", unit_name,
         "--property=ActiveState,Result,ExecMainStatus", "--no-pager"],
        capture=True, check=False,
    )
    if result.returncode:
        return {"status": "unknown", "result": "unknown"}
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    state = values.get("ActiveState", "unknown")
    result_name = values.get("Result", "unknown")
    if state == "active":
        status = "running"
    elif state in {"inactive", "failed"}:
        status = "success" if result_name == "success" and values.get("ExecMainStatus") == "0" else "failed"
    else:
        status = "unknown"
    return {"status": status, "result": result_name}


def job_details(job_id: str) -> dict:
    """Вернуть ограниченный журнал; пароль администратора никогда не запрашивается."""
    item = _read_job(job_id)
    state = _unit_state(item["unit"])
    log_path = JOBS_DIR / f"{job_id}.log"
    content = ""
    if log_path.is_file():
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - MAX_LOG_BYTES))
            content = stream.read(MAX_LOG_BYTES).decode("utf-8", "replace")
    # Стандартные команды уже запрещают доверенный вывод; дополнительная
    # маскировка защищает от случайного печатания типичных полей.
    content = re.sub(
        r"(?im)(password|secret|token|private.key)\s*[:=]\s*\S+",
        r"\1=[СКРЫТО]", content,
    )
    return {**item, **state, "log_tail": content}


def start_job(vmid: int, operation: str, confirmation: str) -> dict:
    """Запустить отдельное systemd-задание, не зависящее от браузера и 140."""
    if operation not in ALLOWED_OPERATIONS:
        raise InfraManagerError("Операция запрещена")
    identity = guest_identity(PATHS.repo_root, vmid)
    allowed = {x.vmid for x in operation_guests(PATHS.repo_root, operation)}
    if vmid not in allowed:
        raise InfraManagerError("Операция не поддерживается для гостя")
    if confirmation != f"{vmid} {identity.name}":
        raise InfraManagerError("Не подтверждены VMID и имя гостя")
    if not OPERATOR.is_file():
        raise InfraManagerError("Не установлена операторская команда")
    # Один активный запуск на гостя: не допускать повторного POST параллельно.
    for item in JOBS_DIR.glob("*.json"):
        try:
            previous = json.loads(item.read_text(encoding="utf-8"))
            if previous.get("vmid") == vmid and _unit_state(previous["unit"])["status"] in {"running", "unknown"}:
                raise InfraManagerError("Для гостя уже выполняется либо требует проверки другое задание")
        except (OSError, ValueError, KeyError, TypeError):
            raise InfraManagerError("Не удалось проверить ранее запущенные задания")
    job_id = uuid.uuid4().hex
    unit_name = f"{UNIT_PREFIX}{job_id}"
    log_path = JOBS_DIR / f"{job_id}.log"
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    record = {
        "id": job_id, "unit": unit_name, "vmid": vmid, "name": identity.name,
        "operation": operation, "created_at": _utc_now(),
    }
    _write_json(JOBS_DIR / f"{job_id}.json", record)
    # systemd-run создаёт независимую службу: закрытие страницы не прерывает задачу.
    try:
        result = CommandRunner().run(
            ["systemd-run", "--quiet", f"--unit={unit_name}",
             "--property=Type=exec",
             f"--property=StandardOutput=append:{log_path}",
             f"--property=StandardError=append:{log_path}",
             "--", str(OPERATOR), operation, str(vmid)],
            capture=True, check=False,
        )
        if result.returncode:
            raise InfraManagerError("Systemd отказал в запуске задания")
    except (OSError, InfraManagerError) as exc:
        record["start_error"] = "Не удалось назначить задание systemd"
        _write_json(JOBS_DIR / f"{job_id}.json", record)
        raise InfraManagerError(record["start_error"]) from exc
    return {"id": job_id, "status": "running", "vmid": vmid, "operation": operation}


class HomarrApiHandler(BaseHTTPRequestHandler):
    """Изолированный интерфейс только для 140 или локального администратора."""

    server_version = "InfraHomarr/1"

    def _reply(self, status: int, value: dict) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _check(self, *, action: bool = False) -> bool:
        if not allowed_client(self.client_address[0], PATHS.repo_root):
            self._reply(HTTPStatus.FORBIDDEN, {"error": "Клиент не разрешён"})
            return False
        if action and os.environ.get("INFRA_HOMARR_ACTIONS_ENABLED") != "1":
            self._reply(HTTPStatus.FORBIDDEN, {"error": "Изменяющие команды пока отключены"})
            return False
        token = WRITE_KEY if action else READ_KEY
        if not _key_matches(self.headers.get("Authorization", ""), token):
            self._reply(HTTPStatus.UNAUTHORIZED, {"error": "Нет доступа"})
            return False
        return True

    def do_GET(self) -> None:
        if not self._check():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/v1/guests":
                inventory = read_guest_inventory(PATHS.repo_root)
                inventory["actions_enabled"] = os.environ.get("INFRA_HOMARR_ACTIONS_ENABLED") == "1"
                self._reply(HTTPStatus.OK, inventory)
            elif path.startswith("/v1/jobs/"):
                self._reply(HTTPStatus.OK, job_details(path.rsplit("/", 1)[-1]))
            else:
                self._reply(HTTPStatus.NOT_FOUND, {"error": "Адрес не найден"})
        except (InfraManagerError, ValueError, OSError, KeyError) as exc:
            self._reply(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

    def do_POST(self) -> None:
        if not self._check(action=True):
            return
        path = urlsplit(self.path).path
        match = re.fullmatch(r"/v1/guests/([0-9]+)/actions", path)
        if not match:
            self._reply(HTTPStatus.NOT_FOUND, {"error": "Адрес не найден"})
            return
        try:
            count = int(self.headers.get("Content-Length", "0"))
            if not 0 < count <= MAX_BODY:
                raise InfraManagerError("Неверный размер запроса")
            body = json.loads(self.rfile.read(count))
            if not isinstance(body, dict) or set(body) != {"operation", "confirm"}:
                raise InfraManagerError("Допустимы только operation и confirm")
            operation, confirmation = body["operation"], body["confirm"]
            if not isinstance(operation, str) or not isinstance(confirmation, str):
                raise InfraManagerError("Неверный формат операции")
            with self.server.action_lock:
                job = start_job(int(match.group(1)), operation, confirmation)
            self._reply(HTTPStatus.ACCEPTED, job)
        except (InfraManagerError, ValueError, OSError, json.JSONDecodeError) as exc:
            self._reply(HTTPStatus.BAD_REQUEST, {"error": str(exc)})


class HomarrApiServer(ThreadingHTTPServer):
    """HTTP-сервер с общей блокировкой постановки заданий."""

    def __init__(self, address: tuple[str, int]):
        super().__init__(address, HomarrApiHandler)
        self.action_lock = threading.Lock()


def run_homarr_api(bind: str, port: int, cert: Path, key: Path) -> int:
    """Для административных ключей обязательно использовать проверяемый TLS."""
    if not cert.is_file() or not key.is_file():
        raise InfraManagerError("Сертификат HTTPS управляющего гостя отсутствует")
    initialize_keys()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(cert), str(key))
    server = HomarrApiServer((bind, port))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0
