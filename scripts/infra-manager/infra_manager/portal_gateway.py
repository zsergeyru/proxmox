"""Защищённый прямой запуск стандартных действий из локального Homepage."""

from __future__ import annotations

import html
import os
import subprocess
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import InfraManagerError
from .guest_catalog import guest_identity
from .guest_operations import operation_guests
from .settings import PATHS, SETTINGS


ACTION_LABELS = {
    "status": "Проверить",
    "sync": "Синхронизировать",
    "repair": "Исправить",
}
OPERATOR_COMMAND = Path("/usr/local/sbin/infra-manager")


def _validate_action(operation: str, vmid: int):
    if operation not in ACTION_LABELS:
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


def _homepage_url(identity) -> str:
    from .portal import local_portal_definition

    portal = local_portal_definition(PATHS.repo_root, identity)
    if not isinstance(portal, dict):
        raise InfraManagerError(
            f"Для гостя {identity.vmid} не настроен локальный Homepage"
        )
    url = portal.get("url")
    if not isinstance(url, str) or not url.startswith("http://"):
        raise InfraManagerError(
            f"Для гостя {identity.vmid} не определён адрес локального Homepage"
        )
    return url


def start_action(operation: str, vmid: int) -> subprocess.Popen[str]:
    """Запустить разрешённую операторскую команду без вывода секретов."""

    _validate_action(operation, vmid)
    if not OPERATOR_COMMAND.is_file():
        raise InfraManagerError(
            f"Не найдена операторская команда: {OPERATOR_COMMAND}"
        )

    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    return subprocess.Popen(
        [str(OPERATOR_COMMAND), operation, str(vmid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )


def _page(title: str, body: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 3rem auto;
      padding: 0 1rem; background: #0f172a; color: #e2e8f0; }}
    .card {{ border: 1px solid #334155; border-radius: 14px; padding: 1.5rem;
      background: #111827; box-shadow: 0 18px 50px rgba(0,0,0,.25); }}
    .actions {{ display: flex; gap: .75rem; margin-top: 1.5rem; flex-wrap: wrap; }}
    button, a.button {{ padding: .7rem 1rem; border-radius: 8px; border: 1px solid #475569;
      background: #1e293b; color: #e2e8f0; text-decoration: none; cursor: pointer; }}
    button.primary {{ font-weight: 700; border-color: #38bdf8; }}
    pre.log {{ white-space: pre-wrap; overflow-wrap: anywhere; min-height: 12rem;
      max-height: 65vh; overflow: auto; padding: 1rem; border-radius: 10px;
      background: #020617; border: 1px solid #1e293b; color: #cbd5e1; }}
    .ok {{ color: #34d399; font-weight: 700; }}
    .error {{ color: #fb7185; font-weight: 700; }}
    .muted {{ color: #94a3b8; }}
  </style>
</head>
<body><div class="card">{body}</div></body>
</html>
""".encode("utf-8")


def _stream_page_start(title: str, vmid: int, name: str) -> bytes:
    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; }}
    body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 3rem auto;
      padding: 0 1rem; background: #0f172a; color: #e2e8f0; }}
    .card {{ border: 1px solid #334155; border-radius: 14px; padding: 1.5rem;
      background: #111827; box-shadow: 0 18px 50px rgba(0,0,0,.25); }}
    .actions {{ display: flex; gap: .75rem; margin-top: 1.5rem; flex-wrap: wrap; }}
    a.button {{ padding: .7rem 1rem; border-radius: 8px; border: 1px solid #475569;
      background: #1e293b; color: #e2e8f0; text-decoration: none; }}
    pre.log {{ white-space: pre-wrap; overflow-wrap: anywhere; min-height: 12rem;
      max-height: 65vh; overflow: auto; padding: 1rem; border-radius: 10px;
      background: #020617; border: 1px solid #1e293b; color: #cbd5e1; }}
    .ok {{ color: #34d399; font-weight: 700; }}
    .error {{ color: #fb7185; font-weight: 700; }}
    .muted {{ color: #94a3b8; }}
  </style>
</head>
<body><div class="card">
<h1>{html.escape(title)}</h1>
<p>Гость: <strong>{vmid} — {html.escape(name)}</strong></p>
<p class="muted">Операция выполняется напрямую через общий исполнитель infra-manager.</p>
<pre class="log">""".encode("utf-8")


class PortalActionHandler(BaseHTTPRequestHandler):
    server_version = "InfraPortal/2"

    def log_message(self, fmt: str, *args: object) -> None:
        super().log_message(fmt, *args)

    def _security_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'",
        )

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
        self._security_headers()
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

    def _require_same_origin(self) -> bool:
        host = self.headers.get("Host", "").strip()
        origin = self.headers.get("Origin", "").strip()
        expected = f"http://{host}" if host else ""
        if not expected or origin.rstrip("/") != expected.rstrip("/"):
            self._send_html(
                HTTPStatus.FORBIDDEN,
                "Запрос отклонён",
                (
                    "<h1>Запрос отклонён</h1>"
                    "<p>Подтверждение должно быть отправлено со страницы посредника.</p>"
                ),
            )
            return False
        return True

    def do_GET(self) -> None:
        if self.path == "/health":
            body = b"ok\n"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._security_headers()
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

        label = ACTION_LABELS[operation]
        action = html.escape(self.path, quote=True)
        homepage_url = html.escape(_homepage_url(identity), quote=True)
        self._send_html(
            HTTPStatus.OK,
            label,
            (
                f"<h1>{html.escape(label)}</h1>"
                f"<p>Гость: <strong>{identity.vmid} — {html.escape(identity.name)}</strong></p>"
                "<p>После подтверждения операция будет выполнена напрямую. "
                "Журнал появится на этой странице.</p>"
                f'<form method="post" action="{action}">'
                '<div class="actions"><button class="primary" type="submit">'
                f"{html.escape(label)}</button>"
                f'<a class="button" href="{homepage_url}">Отмена</a></div>'
                "</form>"
            ),
        )

    def do_POST(self) -> None:
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

        if not self._require_same_origin():
            return

        try:
            process = start_action(operation, vmid)
        except (InfraManagerError, OSError) as exc:
            self._send_html(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Не удалось запустить действие",
                (
                    "<h1>Не удалось запустить действие</h1>"
                    f"<p>{html.escape(str(exc))}</p>"
                ),
            )
            return

        label = ACTION_LABELS[operation]
        homepage_url = html.escape(_homepage_url(identity), quote=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._security_headers()
        self.end_headers()

        connected = True
        try:
            self.wfile.write(
                _stream_page_start(
                    label,
                    identity.vmid,
                    identity.name,
                )
            )
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            connected = False

        assert process.stdout is not None
        for line in process.stdout:
            if not connected:
                continue
            try:
                self.wfile.write(html.escape(line).encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                connected = False

        returncode = process.wait()
        if not connected:
            return

        result_class = "ok" if returncode == 0 else "error"
        result_text = (
            "Операция завершена успешно"
            if returncode == 0
            else f"Операция завершилась с кодом {returncode}"
        )
        tail = (
            "</pre>"
            f'<p class="{result_class}">{html.escape(result_text)}</p>'
            '<div class="actions">'
            f'<a class="button" href="{homepage_url}">Назад в Homepage</a>'
            f'<a class="button" href="{html.escape(self.path, quote=True)}">Повторить</a>'
            "</div></div></body></html>"
        ).encode("utf-8")
        try:
            self.wfile.write(tail)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return


class PortalActionServer(ThreadingHTTPServer):
    pass


def run_portal_gateway(bind: str, port: int) -> int:
    server = PortalActionServer((bind, port), PortalActionHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
