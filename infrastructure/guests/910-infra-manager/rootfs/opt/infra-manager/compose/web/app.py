"""Локальный предварительный просмотр операций без доступа к исполнению.

Работает только на loopback управляющего LXC. До появления независимой
авторизации и HTTPS не предназначен для публикации в сеть.
"""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

STATE = Path(os.environ.get("INFRA_WEB_OPERATIONS", "/srv/operations"))
ID = re.compile(r"^[0-9a-f]{32}$")
MAX_RECORD = 65536
LIMIT = 40
PHASES = {
    "queued": "Ожидает",
    "running": "Выполняется",
    "activating": "Активация",
    "verifying": "Проверка",
    "completed": "Успешно",
    "failed": "Ошибка",
    "rolled-back": "Откат",
    "needs-recovery": "Нужна проверка",
}


def _read_record(path: Path) -> dict | None:
    """Читать только обычный небольшой JSON без символьных ссылок."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as stream:
            if stream.readable():
                data = stream.read(MAX_RECORD + 1)
            else:
                return None
        if len(data) > MAX_RECORD:
            return None
        result = json.loads(data)
        return result if isinstance(result, dict) else None
    except (OSError, ValueError, UnicodeError):
        return None


def _history(limit: int = LIMIT) -> list[dict]:
    directory = STATE / "history"
    if directory.is_symlink() or not directory.is_dir():
        return []
    records: list[dict] = []
    for file in directory.iterdir():
        if not file.name.endswith(".json") or not ID.fullmatch(file.stem):
            continue
        record = _read_record(file)
        if record and record.get("id") == file.stem:
            records.append(record)
    return sorted(
        records,
        key=lambda record: (str(record.get("created_at", "")), record["id"]),
        reverse=True,
    )[:limit]


def _page(records: list[dict]) -> bytes:
    """Отображать только экранированные поля проверенных JSON-записей."""
    rows = []
    for record in records:
        operation_id = str(record.get("id", ""))
        if not ID.fullmatch(operation_id):
            continue
        action = html.escape(str(record.get("kind", "—")))
        target = html.escape(str(record.get("target", "—")))
        phase = html.escape(PHASES.get(record.get("phase"), "Неизвестно"))
        started = html.escape(str(record.get("created_at", "—")))
        rows.append(
            f'<tr><td><code>{operation_id[:8]}</code></td><td>{target}</td>'
            f'<td>{action}</td><td>{phase}</td><td>{started}</td></tr>'
        )
    table_rows = "\n".join(rows) if rows else (
        '<tr><td colspan="5">Сохранённых операций пока нет.</td></tr>'
    )
    doc = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>infra-manager — операции (предпросмотр)</title>
<style>
:root{color-scheme:dark}body{font-family:system-ui,sans-serif;margin:3rem auto;
max-width:1050px;padding:0 1rem;background:#0c1525;color:#e5edf9}
h1{font-size:1.65rem}p{color:#aebfd1}
section{background:#142237;border:1px solid #30435b;border-radius:14px;
padding:1.25rem;overflow:auto}table{width:100%;border-collapse:collapse}
th,td{padding:.75rem;text-align:left;border-bottom:1px solid #30435b}
th{color:#aac4dc}code{color:#9bdfd0}
</style></head><body>
<h1>infra-manager · задания</h1>
<p>Локальный предварительный просмотр. Доступен только внутри управляющего
гостя; команды и секреты недоступны. Страница обновляется каждые 15 секунд.</p>
<section><table><thead><tr><th>Задание</th><th>Гость</th><th>Действие</th>
<th>Состояние</th><th>Начало (UTC)</th></tr></thead><tbody>
""" + table_rows + """
</tbody></table></section></body></html>"""
    return doc.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    """Только GET для закрытой истории; произвольные пути не обслуживаются."""

    def _send(self, code: HTTPStatus, body: bytes, mime: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
        elif path == "/":
            self._send(HTTPStatus.OK, _page(_history()), "text/html; charset=utf-8")
        elif path == "/api/operations":
            body = json.dumps(_history(), ensure_ascii=False).encode()
            self._send(HTTPStatus.OK, body, "application/json; charset=utf-8")
        else:
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read only\n", "text/plain; charset=utf-8")


def main() -> None:
    # Дополнительная защита: слушать только loopback, вне зависимости от ENV.
    with ThreadingHTTPServer(("127.0.0.1", 3104), Handler) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
