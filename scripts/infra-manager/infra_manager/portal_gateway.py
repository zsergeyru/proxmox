"""Защищённый переход от локального Homepage к операциям Semaphore."""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .common import InfraManagerError
from .guest_catalog import guest_identity
from .guest_operations import operation_guests
from .semaphore import (
    PROJECT_NAME,
    SemaphoreClient,
    find_unique_by_name,
    require_unique_by_name,
)
from .settings import PATHS, SETTINGS


ACTION_TEMPLATES = {
    "status": ("Проверить", "Status Guest"),
    "sync": ("Синхронизировать", "Sync Guest"),
    "repair": ("Исправить", "Repair Guest"),
}


def _semaphore_objects(operation: str) -> tuple[int, int]:
    client = SemaphoreClient()
    if not client.token_valid():
        raise InfraManagerError(
            "Рабочий API token Semaphore отсутствует или недействителен"
        )
    client.auth_mode = "token"

    project = require_unique_by_name(
        client.get("/projects"),
        PROJECT_NAME,
        "project",
    )
    project_id = project.get("id")
    if not isinstance(project_id, int):
        raise InfraManagerError("Проект Semaphore имеет некорректный id")

    _, template_name = ACTION_TEMPLATES[operation]
    templates = client.get(
        f"/project/{project_id}/templates?sort=name&order=asc"
    )
    template = find_unique_by_name(templates, template_name, "template")
    if template is None:
        raise InfraManagerError(
            f"В Semaphore отсутствует шаблон '{template_name}'"
        )
    template_id = template.get("id")
    if not isinstance(template_id, int):
        raise InfraManagerError(
            f"Semaphore template '{template_name}' имеет некорректный id"
        )
    return project_id, template_id


def _semaphore_request(
    method: str,
    path: str,
    *,
    cookie: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, bytes]:
    body = None
    headers = {"Accept": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    request = urllib.request.Request(
        f"{SETTINGS.semaphore_url}/api{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError) as exc:
        raise InfraManagerError(
            f"Semaphore API недоступен: {exc}"
        ) from exc


def _session_valid(cookie: str) -> bool:
    if not cookie:
        return False
    status, _ = _semaphore_request("GET", "/user", cookie=cookie)
    return status == HTTPStatus.OK


def _validate_action(operation: str, vmid: int):
    if operation not in ACTION_TEMPLATES:
        raise InfraManagerError(f"Неизвестное действие портала: {operation}")
    identity = guest_identity(PATHS.repo_root, vmid)
    allowed = {
        guest.vmid
        for guest in operation_guests(PATHS.repo_root, operation)
    }
    if vmid not in allowed:
        raise InfraManagerError(
            f"Операция {operation} не поддерживается для гостя {vmid}"
        )
    return identity


def queue_action(operation: str, vmid: int, cookie: str) -> tuple[int, int]:
    """Создать штатное задание Semaphore от имени текущего пользователя."""

    _validate_action(operation, vmid)
    project_id, template_id = _semaphore_objects(operation)
    status, raw = _semaphore_request(
        "POST",
        f"/project/{project_id}/tasks",
        cookie=cookie,
        payload={
            "template_id": template_id,
            "environment": json.dumps(
                {"GUEST_VMID": str(vmid)},
                separators=(",", ":"),
            ),
            "secret": "{}",
            "params": {},
            "message": f"Homepage: guest {vmid}",
        },
    )
    if status != HTTPStatus.CREATED:
        detail = raw.decode("utf-8", errors="replace").strip()
        raise InfraManagerError(
            f"Semaphore отклонил задание: HTTP {status}: "
            f"{detail or '(пустой ответ)'}"
        )

    try:
        task = json.loads(raw.decode("utf-8"))
        task_id = int(task["id"])
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as exc:
        raise InfraManagerError(
            "Semaphore создал задание, но не вернул его номер"
        ) from exc
    return project_id, task_id


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ font-family: sans-serif; max-width: 640px; margin: 4rem auto; padding: 0 1rem; }}
    .card {{ border: 1px solid #ccc; border-radius: 12px; padding: 1.5rem; }}
    .actions {{ display: flex; gap: .75rem; margin-top: 1.5rem; flex-wrap: wrap; }}
    button, a.button {{ padding: .7rem 1rem; border-radius: 8px; border: 1px solid #888;
      background: #fff; color: inherit; text-decoration: none; cursor: pointer; }}
    button.primary {{ font-weight: 600; }}
  </style>
</head>
<body><div class="card">{body}</div></body>
</html>
""".encode("utf-8")


class PortalActionHandler(BaseHTTPRequestHandler):
    server_version = "InfraPortal/1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Журнал systemd уже содержит адрес клиента и строку запроса.
        super().log_message(fmt, *args)

    def _send_html(
        self,
        status: int,
        title: str,
        body: str,
    ) -> None:
        content = _page(title, body)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(content)

    def _parse_action(self) -> tuple[str, int]:
        parsed = urllib.parse.urlparse(self.path)
        prefix = "/action/"
        if not parsed.path.startswith(prefix):
            raise InfraManagerError("Неизвестный адрес")
        operation = parsed.path[len(prefix):].strip("/")
        values = urllib.parse.parse_qs(parsed.query)
        raw_vmid = values.get("vmid", [""])[0]
        if not raw_vmid.isdigit() or int(raw_vmid) <= 0:
            raise InfraManagerError("Некорректный VMID")
        return operation, int(raw_vmid)

    def _cookie(self) -> str:
        return self.headers.get("Cookie", "")

    def _require_session(self) -> bool:
        try:
            valid = _session_valid(self._cookie())
        except InfraManagerError as exc:
            self._send_html(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Semaphore недоступен",
                f"<h1>Semaphore недоступен</h1><p>{html.escape(str(exc))}</p>",
            )
            return False
        if valid:
            return True

        self._send_html(
            HTTPStatus.UNAUTHORIZED,
            "Требуется вход",
            (
                "<h1>Требуется вход в Semaphore</h1>"
                "<p>Откройте Semaphore, войдите в систему и повторите действие.</p>"
                '<div class="actions"><a class="button" '
                f'href="http://{html.escape(self.server.manager_address)}:3000/auth/login">'
                "Открыть Semaphore</a></div>"
            ),
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            body = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        try:
            operation, vmid = self._parse_action()
            identity = _validate_action(operation, vmid)
        except InfraManagerError as exc:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                "Некорректное действие",
                f"<h1>Действие недоступно</h1><p>{html.escape(str(exc))}</p>",
            )
            return

        if not self._require_session():
            return

        label, _ = ACTION_TEMPLATES[operation]
        action = html.escape(self.path, quote=True)
        self._send_html(
            HTTPStatus.OK,
            label,
            (
                f"<h1>{html.escape(label)}</h1>"
                f"<p>Гость: <strong>{identity.vmid} — {html.escape(identity.name)}</strong></p>"
                "<p>После подтверждения будет создано штатное задание Semaphore.</p>"
                f'<form method="post" action="{action}">'
                '<div class="actions"><button class="primary" type="submit">'
                f"{html.escape(label)}</button>"
                '<a class="button" href="javascript:history.back()">Отмена</a></div>'
                "</form>"
            ),
        )

    def do_POST(self) -> None:  # noqa: N802
        try:
            operation, vmid = self._parse_action()
            _validate_action(operation, vmid)
        except InfraManagerError as exc:
            self._send_html(
                HTTPStatus.BAD_REQUEST,
                "Некорректное действие",
                f"<h1>Действие недоступно</h1><p>{html.escape(str(exc))}</p>",
            )
            return

        if not self._require_session():
            return

        origin = self.headers.get("Origin")
        if origin:
            expected = f"http://{self.headers.get('Host', '')}"
            if origin.rstrip("/") != expected.rstrip("/"):
                self._send_html(
                    HTTPStatus.FORBIDDEN,
                    "Запрос отклонён",
                    "<h1>Запрос отклонён</h1><p>Источник запроса не совпадает с порталом.</p>",
                )
                return

        try:
            project_id, task_id = queue_action(
                operation,
                vmid,
                self._cookie(),
            )
        except InfraManagerError as exc:
            self._send_html(
                HTTPStatus.BAD_GATEWAY,
                "Не удалось создать задание",
                (
                    "<h1>Не удалось создать задание</h1>"
                    f"<p>{html.escape(str(exc))}</p>"
                ),
            )
            return

        location = (
            f"http://{self.server.manager_address}:3000/project/"
            f"{project_id}/history?t={task_id}"
        )
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class PortalActionServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        manager_address: str,
    ) -> None:
        self.manager_address = manager_address
        super().__init__(server_address, PortalActionHandler)


def run_portal_gateway(bind: str, port: int) -> int:
    manager = guest_identity(
        PATHS.repo_root,
        find_infra_manager_vmid(),
    )
    from .guest_catalog import guest_management_address

    address = guest_management_address(PATHS.repo_root, manager)
    if not address:
        raise InfraManagerError(
            "Не удалось определить административный адрес infra-manager"
        )

    server = PortalActionServer((bind, port), address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def find_infra_manager_vmid() -> int:
    from .guest_catalog import find_guest_by_role

    return find_guest_by_role(
        PATHS.repo_root,
        SETTINGS.infra_manager_role,
    ).vmid
