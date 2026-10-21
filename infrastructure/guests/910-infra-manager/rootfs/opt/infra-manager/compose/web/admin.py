"""Административная панель infra-manager: HTTPS, OpenBao, закрытая сессия.

Пароли проверяются OpenBao userpass и не хранятся в контейнере.
Команды передаются в проверяемый системный посредник по Unix-сокету.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import ssl
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from app import PHASES

WEB_HOST = os.environ.get("INFRA_WEB_PUBLIC_HOST", "192.168.9.10:8443")
CERT = os.environ.get("INFRA_WEB_CERT", "/etc/infra-manager/web/tls/server.crt")
KEY = os.environ.get("INFRA_WEB_KEY", "/etc/infra-manager/web/tls/server.key")
SOCKET = os.environ.get("INFRA_WEB_SOCKET", "/run/infra-web/operations.sock")
OPENBAO = "http://127.0.0.1:8200"
COOKIE = "infra_session"
SESSION_TTL = 3600
REMEMBER_TTL = 30 * 24 * 3600
SESSION_DIR = Path("/var/lib/infra-web/sessions")
SESSION_LIMIT = 60
MAX_BODY = 8192
REQ_ID = re.compile(r"^[0-9a-f]{32}$")
sessions: dict[str, dict] = {}
attempts: dict[str, list[float]] = {}
guard = threading.RLock()


def _authenticate(username: str, password: str) -> bool:
    """Проверять пароль человека через OpenBao, не доверяя присланному имени."""
    if username != "operator" or not password or len(password) > 2048:
        return False
    payload = json.dumps({"password": password}).encode()
    request = urllib.request.Request(
        OPENBAO + "/v1/auth/userpass/login/operator",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as reply:
            result = json.load(reply)
        token = result.get("auth", {}).get("client_token")
        if not isinstance(token, str) or not token:
            return False
        # Не хранить токен в сессии. Отозвать кратковременный входной токен.
        revoke = urllib.request.Request(
            OPENBAO + "/v1/auth/token/revoke-self",
            data=b"{}", headers={"X-Vault-Token": token,
                                  "Content-Type": "application/json"}, method="POST",
        )
        try:
            urllib.request.urlopen(revoke, timeout=5).close()
        except (OSError, urllib.error.HTTPError):
            # Отказ отзыва не превращает OpenBao token в полномочия веб-панели.
            pass
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _session_directory() -> Path:
    """Отдельный приватный том: никаких сессий в cookie, Git или Docker layer."""
    folder = SESSION_DIR
    info = folder.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("Постоянное хранилище сеансов недоверенное")
    return folder


def _session_path(token: str) -> Path:
    """Имя файла — только SHA-256 случайного cookie, не сам bearer token."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ValueError("Недопустимый идентификатор сессии")
    return _session_directory() / (hashlib.sha256(token.encode("ascii")).hexdigest() + ".json")


def _stored_session(token: str) -> dict | None:
    try:
        path = _session_path(token)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 512):
                return None
            raw = os.read(fd, 513)
        finally:
            os.close(fd)
        if len(raw) > 512:
            return None
        data = json.loads(raw)
        if (not isinstance(data, dict) or set(data) != {"version", "csrf", "expires"}
                or data["version"] != 1
                or not isinstance(data["csrf"], str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", data["csrf"])
                or type(data["expires"]) is not int):
            return None
        if data["expires"] <= int(time.time()):
            path.unlink(missing_ok=True)
            return None
        # Сервер хранит только CSRF/expiry; OpenBao-token не сохраняется.
        return {"csrf": data["csrf"], "expires": data["expires"], "remember": True}
    except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError):
        return None


def _write_persistent_session(token: str, session: dict) -> None:
    path = _session_path(token)
    folder = path.parent
    # Ограничение числа долгих сеансов и уборка протухших файлов.
    records = list(folder.iterdir())
    if len(records) >= SESSION_LIMIT:
        for entry in records:
            if re.fullmatch(r"[0-9a-f]{64}\.json", entry.name):
                try:
                    fd = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW)
                    try:
                        info = os.fstat(fd)
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                                or info.st_size > 512):
                            continue
                        record = json.loads(os.read(fd, 513))
                    finally:
                        os.close(fd)
                    if type(record.get("expires")) is int and record["expires"] <= int(time.time()):
                        entry.unlink(missing_ok=True)
                except (OSError, ValueError, TypeError):
                    continue
        if len(list(folder.iterdir())) >= SESSION_LIMIT:
            raise ValueError("Превышен предел запомненных устройств")
    record = {"version": 1, "csrf": session["csrf"], "expires": session["expires"]}
    payload = json.dumps(record, separators=(",", ":")).encode()
    temporary = folder / ("." + secrets.token_hex(16) + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _revoke_session(token: str, session: dict) -> None:
    with guard:
        sessions.pop(token, None)
    if session.get("remember") is True:
        try:
            _session_path(token).unlink(missing_ok=True)
        except (OSError, ValueError):
            # Сбой отзыва нельзя замаскировать успешным выходом.
            raise ValueError("Не удалось отозвать долгую сессию")


def _session(cookie: str | None) -> tuple[str, dict] | None:
    if not cookie or len(cookie) > 1024:
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(cookie)
    except Exception:
        return None
    if COOKIE not in parsed:
        return None
    token = parsed[COOKIE].value
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        return None
    with guard:
        session = sessions.get(token)
        if session and session["expires"] > time.monotonic():
            return token, session
        sessions.pop(token, None)
    saved = _stored_session(token)
    return (token, saved) if saved else None


def _new_session(*, remember: bool = False) -> tuple[str, dict]:
    token = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(32)}
    if remember:
        session.update(expires=int(time.time()) + REMEMBER_TTL, remember=True)
        try:
            _write_persistent_session(token, session)
        except (OSError, ValueError):
            raise ValueError("Не удалось сохранить длительный вход") from None
    else:
        session.update(expires=time.monotonic() + SESSION_TTL, remember=False)
        with guard:
            now = time.monotonic()
            for key, value in tuple(sessions.items()):
                if value["expires"] <= now:
                    sessions.pop(key, None)
            if len(sessions) >= SESSION_LIMIT:
                raise ValueError("Превышен предел числа активных входов")
            sessions[token] = session
    return token, session


def _allow_attempt(ip: str) -> bool:
    with guard:
        now = time.monotonic()
        timestamps = [x for x in attempts.get(ip, []) if now - x < 600]
        if len(timestamps) >= 5:
            attempts[ip] = timestamps
            return False
        timestamps.append(now)
        attempts[ip] = timestamps
        return True



def _operation_history() -> list[dict]:
    """Получить разрешённые поля истории от системного посредника, не читая state."""
    request = {
        "kind": "history", "vmid": 0, "request_id": secrets.token_hex(16),
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(SOCKET)
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            raw = stream.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("Посредник не вернул ограниченную историю")
    response = json.loads(raw)
    if (
        not isinstance(response, dict)
        or response.get("accepted") is not True
        or not isinstance(response.get("operations"), list)
        or len(response["operations"]) > 40
        or any(not isinstance(item, dict) for item in response["operations"])
    ):
        raise ValueError("Посредник отклонил запрос истории")
    return response["operations"]



def _operation_detail(operation_id: str) -> dict:
    """Запросить диагностику ровно одного UUID через закрытый системный посредник."""
    if not REQ_ID.fullmatch(operation_id):
        raise ValueError("Некорректный идентификатор задания")
    payload = json.dumps({
        "kind": "operation-detail", "vmid": 0,
        "request_id": secrets.token_hex(16), "operation_id": operation_id,
    }).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(SOCKET)
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            raw = stream.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("Посредник не вернул ограниченную диагностику")
    result = json.loads(raw)
    if (not isinstance(result, dict)
            or result.get("accepted") is not True
            or not isinstance(result.get("operation"), dict)
            or result["operation"].get("id") != operation_id
            or not isinstance(result.get("events"), list)
            or len(result["events"]) > 20
            or not isinstance(result.get("logs"), list)
            or len(result["logs"]) > 50):
        raise ValueError("Некорректный ответ диагностики")
    return result


def _operation_page(operation_id: str, csrf: str | None = None) -> bytes:
    """Страница задания с читаемым временем и простым журналом."""
    try:
        result = _operation_detail(operation_id)
    except (OSError, ValueError, TimeoutError):
        result = {
            "operation": {"phase": "unavailable"}, "events": [], "logs": [],
            "diagnostic": "Системный посредник недоступен. Повторяем проверку автоматически.",
        }
    record = result["operation"]
    events = result["events"]
    stages = "".join(
        '<li><div class="timeline-name">' +
        _text(PHASES.get(event.get("phase"), "Неизвестно")) +
        '</div><div class="timeline-detail">' + _time_html(event.get("at")) +
        '</div></li>' for event in events if isinstance(event, dict)
    ) or '<li>Этапы ещё не записаны</li>'
    logs = "\n".join(str(line) for line in result["logs"] if isinstance(line, str))
    phase = record.get("phase")
    running = phase in {"queued", "running", "activating", "verifying"}
    started = record.get("started_at") or record.get("created_at")
    finished = record.get("finished_at")
    duration = (
        '<strong id="op-duration" data-duration-start="' +
        _text(started) + '"' +
        (' data-duration-end="' + _text(finished) + '"' if finished else '') +
        '>—</strong>' if isinstance(started, str) and
        re.fullmatch(r"\d{4}-\d\d-\d\dT[^ ]+", started) else
        '<strong id="op-duration">—</strong>'
    )
    body = (
        '<div class="breadcrumbs"><a href="/#recent-operations">Задания</a> / '
        + operation_id[:8] + '</div>'
        '<section id="operation-view" data-operation-id="' + operation_id + '">'
        '<div class="panel"><div class="section-head"><div>'
        '<span class="item-label">Задание ' + operation_id[:8] + '</span>'
        '<h2>Диагностика задания</h2>'
        '</div><span id="op-phase">' + _chip(phase) + '</span></div>'
        '<p class="page-intro">Следи за выполнением или открой эту страницу позже: '
        'операция продолжается независимо от браузера.</p>'
        '<div class="grid cols-3">'
        '<div class="stat"><div class="stat-label">Действие</div><div class="stat-value" '
        'style="font-size:20px" id="op-kind">' +
        _text(OPERATION_NAMES.get(record.get("kind"), record.get("kind"))) + '</div>'
        '</div><div class="stat"><div class="stat-label">Гость</div><div class="stat-value" '
        'style="font-size:20px" id="op-target">' + _text(record.get("target")) +
        '</div></div><div class="stat"><div class="stat-label">Длительность</div>'
        '<div class="stat-value" style="font-size:20px">' + duration + '</div></div></div>'
        '<div class="details-row"><span>Начало: <strong id="op-start">' +
        _time_html(started) + '</strong></span><span>Завершение: '
        '<strong id="op-finish">' + _time_html(finished) + '</strong></span>'
        '<span>Код: <strong id="op-exit">' + _text(record.get("exit_code")) +
        '</strong></span></div>'
        '<div id="op-diagnostic" class="notice" role="status">' +
        _text(result.get("diagnostic", "")) + '</div>'
        '<p class="muted code">ID: ' + operation_id + '</p></div>'
        '<div class="grid cols-2">'
        '<section class="panel"><div class="section-head"><h3>Этапы выполнения</h3></div>'
        '<ol id="op-timeline" class="timeline">' + stages + '</ol></section>'
        '<section class="panel"><div class="section-head"><h3>Управление просмотром</h3>'
        '</div><p class="muted">Данные обновляются каждые 4 секунды. '
        'Здесь нельзя случайно перезапустить задание.</p>'
        '<div class="actions"><a class="btn secondary" href="/#recent-operations">'
        'Все задания</a><button class="secondary" type="button" id="op-copy">'
        'Скопировать ID</button></div>'
        '<p class="muted" id="op-refresh">' +
        ("Выполняется · автообновление включено" if running else
         "Задание завершено · журнал доступен") + '</p></section></div>'
        '<section class="panel"><div class="section-head"><h3>Журнал задания</h3>'
        '<button class="secondary" type="button" id="op-copy-log">Копировать</button></div>'
        '<p class="muted">Показаны безопасные служебные сообщения. '
        'Полный технический вывод остаётся внутри 910.</p>'
        '<label for="op-log-search">Поиск в видимых строках</label>'
        '<input id="op-log-search" type="search" placeholder="Поиск ошибки, шага…" '
        'aria-label="Поиск в журнале">'
        '<pre id="op-log" class="diagnostic-log">' + html.escape(logs) +
        '</pre></section></section>'
        '<script src="/diagnostics.js" defer></script>'
    )
    return _layout(body, page="operations", csrf=csrf)



GUEST_STATES = {
    "running": "Работает", "stopped": "Остановлен",
    "paused": "Приостановлен", "absent": "Не найден в PVE",
    "conflict": "Конфликт VMID", "unavailable": "PVE недоступен",
    "unknown": "Неизвестно",
}
GUEST_ACTION_LABELS = {
    "status": "Проверить", "sync": "Синхронизировать",
    "repair": "Исправить", "deploy": "Развернуть",
}


def _guest_catalog(kind: str, vmid: int = 0) -> dict:
    """Читать каталог и состояние PVE только через закрытый системный сокет."""
    if kind not in {"guests", "guest-detail"} or (
        kind == "guests" and vmid != 0
    ) or (kind == "guest-detail" and not 1 <= vmid <= 999999):
        raise ValueError("Некорректный запрос гостя")
    payload = json.dumps({
        "kind": kind, "vmid": vmid, "request_id": secrets.token_hex(16),
    }).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(12)
        connection.connect(SOCKET)
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            raw = stream.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("Нет ограниченного ответа каталога")
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("accepted") is not True:
        raise ValueError("Посредник не вернул каталог")
    if kind == "guests":
        guests = data.get("guests")
        if (not isinstance(guests, list) or len(guests) > 100
                or any(not isinstance(row, dict) for row in guests)):
            raise ValueError("Некорректный список гостей")
    else:
        guest = data.get("guest")
        operations = data.get("operations")
        if (not isinstance(guest, dict) or guest.get("vmid") != vmid
                or not isinstance(operations, list) or len(operations) > 20
                or any(not isinstance(row, dict) for row in operations)):
            raise ValueError("Некорректная карточка гостя")
    return data


def _guest_rows(records: list[dict]) -> str:
    rows = []
    for guest in records:
        vmid = guest.get("vmid")
        if type(vmid) is not int or not 1 <= vmid <= 999999:
            continue
        state = GUEST_STATES.get(guest.get("state"), "Неизвестно")
        cells = [
            '<a href="/guests?vmid=' + str(vmid) + '">' + str(vmid) + '</a>',
            str(guest.get("name") or "—"),
            str(guest.get("role") or "—"),
            "VM" if guest.get("type") == "vm" else
            "LXC" if guest.get("type") == "lxc" else "—",
            str(guest.get("address") or "—"), state,
        ]
        rows.append("<tr>" + "".join(
            "<td>" + (cell if i == 0 else html.escape(cell)) + "</td>"
            for i, cell in enumerate(cells)
        ) + "</tr>")
    return "".join(rows) or '<tr><td colspan="6">Гостей нет</td></tr>'


def _guest_card(guest: dict) -> str:
    vmid = guest.get("vmid")
    if type(vmid) is not int or not 1 <= vmid <= 999999:
        return ""
    name = str(guest.get("name") or f"Гость {vmid}")
    role = str(guest.get("role") or "Без роли")
    address = str(guest.get("address") or "Адрес не указан")
    kind = "VM" if guest.get("type") == "vm" else "LXC" if guest.get("type") == "lxc" else "Тип неизвестен"
    state = guest.get("state")
    group = "attention" if state in {"stopped", "paused", "absent", "conflict"} else "other"
    return (
        '<a class="card-link guest-card" data-filter-item data-state="' + _text(state) +
        '" data-group="' + group + '" data-search="' + html.escape(f"{vmid} {name} {role} {address} {kind}", quote=True) +
        '" href="/guests?vmid=' + str(vmid) + '">'
        '<div class="guest-card-top"><span class="vmid">#' + str(vmid) + " · " + kind +
        '</span>' + _chip(state, category="guest") + '</div>'
        '<div class="card-heading">' + _text(name) + '</div>'
        '<div class="muted">' + _text(role) + '</div>'
        '<div class="card-footer"><span class="address">' + _text(address) +
        '</span><strong>Открыть →</strong></div></a>'
    )


def _guests_page(csrf: str = "") -> bytes:
    try:
        data = _guest_catalog("guests")
        guests = data["guests"]
        available = data.get("pve_available") is True
    except (OSError, ValueError, TimeoutError):
        return _layout(
            '<div class="notice" data-tone="warning" role="status">'
            'Каталог гостей временно недоступен. Проверьте посредник и повторите запрос.'
            '</div><a class="btn secondary" href="/guests">Повторить</a>',
            page="guests", csrf=csrf,
        )
    visible = [g for g in guests if isinstance(g, dict) and type(g.get("vmid")) is int]
    online = sum(g.get("state") == "running" for g in visible)
    attention = sum(g.get("state") in {"conflict", "absent", "stopped", "paused"} for g in visible)
    items = "".join(_guest_card(guest) for guest in visible)
    note = (
        '<div class="notice" data-tone="warning" role="status">'
        'Нет связи с PVE: статусы не подтверждены. Каталог из Git доступен.</div>'
        if not available else ""
    )
    stats = "".join(
        '<div class="stat" data-tone="' + tone + '"><div class="stat-label">' + label +
        '</div><div class="stat-value">' + str(num) + '</div>'
        '<div class="stat-detail">' + hint + '</div></div>'
        for label, num, hint, tone in (
            ("В каталоге", len(visible), "VM и LXC из Git", ""),
            ("Работают", online if available else "—", "По данным PVE", "success"),
            ("Требуют внимания", attention if available else "—", "Остановлены или конфликт", "warning" if attention else ""),
        )
    )
    return _layout(
        '<p class="page-intro">Выбери гостя, чтобы проверить его состояние, '
        'посмотреть историю или выполнить разрешённое действие.</p>'
        + note + '<div class="grid cols-3">' + stats + '</div>'
        '<section class="panel"><div class="section-head"><h2>Каталог гостей</h2>'
        '<a class="btn secondary" href="/guests">↻ Обновить</a></div>'
        '<div class="toolbar"><input type="search" placeholder="Поиск: имя, IP, VMID…" '
        'aria-label="Поиск гостя" data-search-for="guests">'
        '<select aria-label="Фильтр состояния" data-state-for="guests">'
        '<option value="all">Все гости</option>'
        '<option value="running">Работают</option>'
        '<option value="attention">Требуют внимания</option>'
        '<option value="unavailable">Не проверены</option>'
        '</select><span class="muted" data-count-for="guests">'
        + str(len(visible)) + ' из ' + str(len(visible)) + '</span></div>'
        '<div class="guest-grid" data-filter-list="guests">' + items + '</div>'
        '<p class="empty" data-empty-for="guests" hidden>Нет подходящих гостей.</p>'
        '</section>',
        page="guests", csrf=csrf,
    )


def _guest_page(vmid: int, csrf: str) -> bytes:
    try:
        data = _guest_catalog("guest-detail", vmid)
    except (OSError, ValueError, TimeoutError):
        return _layout(
            '<div class="breadcrumbs"><a href="/guests">Гости</a> / ' + str(vmid) + '</div>'
            '<div class="notice" data-tone="warning">Карточка временно недоступна. '
            'Попробуй обновить страницу.</div>'
            '<a class="btn secondary" href="/guests?vmid=' + str(vmid) + '">Повторить</a>',
            page="guests", csrf=csrf,
        )
    guest = data["guest"]
    actions = guest.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    token = html.escape(csrf, quote=True)
    commands = []
    for kind, label in GUEST_ACTION_LABELS.items():
        if kind not in actions:
            continue
        if kind == "status":
            commands.append(
                '<form method="POST" action="/action">'
                f'<input type="hidden" name="csrf" value="{token}">'
                f'<input type="hidden" name="vmid" value="{vmid}">'
                '<input type="hidden" name="kind" value="status">'
                '<button type="submit">Проверить состояние</button></form>'
            )
        else:
            commands.append(
                '<form method="GET" action="/guests/confirm">'
                f'<input type="hidden" name="vmid" value="{vmid}">'
                f'<input type="hidden" name="kind" value="{kind}">'
                f'<button class="secondary" type="submit">{_text(label)}</button></form>'
            )
    history = []
    for item in data["operations"]:
        identifier = item.get("id")
        if not isinstance(identifier, str) or not REQ_ID.fullmatch(identifier):
            continue
        phase = item.get("phase")
        history.append(
            '<tr><td><a class="cell-title" href="/operations?id=' + identifier + '">'
            + _text(OPERATION_NAMES.get(item.get("kind"), item.get("kind", "Действие"))) +
            '</a><span class="cell-sub code">' + identifier[:8] + '</span></td>'
            '<td>' + _chip(phase) + '</td><td>' + _time_html(item.get("created_at")) +
            '</td></tr>'
        )
    state = guest.get("state")
    if data.get("pve_available") is False:
        state = "unavailable"
    detail = (
        ("Имя", guest.get("name") or "—"),
        ("Роль", guest.get("role") or "—"),
        ("Тип", "VM" if guest.get("type") == "vm" else "LXC"
         if guest.get("type") == "lxc" else "—"),
        ("Адрес управления", guest.get("address") or "—"),
        ("Состояние PVE", GUEST_STATES.get(state, "Неизвестно")),
    )
    details_html = "".join(
        "<dt>" + _text(title) + "</dt><dd>" + _text(value) + "</dd>"
        for title, value in detail
    )
    history_notice = (
        '<div class="notice" data-tone="warning">История операций пока недоступна.</div>'
        if data.get("history_available") is False else ""
    )
    warning = (
        '<div class="notice" data-tone="warning">Самообновление 910 доступно '
        'только через PVE CLI.</div>' if vmid == 910 else ""
    )
    return _layout(
        '<div class="breadcrumbs"><a href="/guests">Все гости</a> / ' + str(vmid) + '</div>'
        '<div class="panel"><div class="section-head"><div>'
        '<span class="item-label">Гость #' + str(vmid) + '</span>'
        '<h2>' + _text(guest.get("name") or f"Гость {vmid}") + '</h2>'
        '</div>' + _chip(state, category="guest") + '</div>'
        '<dl class="guest-meta">' + details_html + '</dl></div>'
        + warning +
        '<section class="panel"><h3>Действия</h3>'
        '<p class="muted">Сначала выбери действие. Перед изменением конфигурации '
        'появится подтверждение, после запуска откроется журнал.</p>'
        '<div class="guest-actions">' + "".join(commands) + '</div>'
        + ('' if commands else '<p>Разрешённых действий для этого гостя нет.</p>')
        + '</section>'
        '<section class="panel"><div class="section-head"><h3>Последние задания</h3>'
        '<a href="/guests?vmid=' + str(vmid) + '">↻ Обновить</a></div>'
        + history_notice +
        '<div class="table-wrap"><table><thead><tr><th>Операция</th>'
        '<th>Состояние</th><th>Начато</th></tr></thead><tbody>'
        + ("".join(history) or '<tr><td colspan="3" class="empty">Истории пока нет</td></tr>')
        + '</tbody></table></div></section>',
        page="guests", csrf=csrf,
    )


def _guest_confirmation(vmid: int, kind: str, csrf: str) -> bytes:
    if kind not in {"sync", "repair", "deploy"}:
        raise ValueError("Для этого действия подтверждение не требуется")
    data = _guest_catalog("guest-detail", vmid)
    guest = data["guest"]
    if kind not in guest.get("actions", []):
        raise ValueError("Операция запрещена для выбранного гостя")
    token = html.escape(csrf, quote=True)
    label = GUEST_ACTION_LABELS[kind]
    return _layout(
        '<div class="breadcrumbs"><a href="/guests?vmid=' + str(vmid) +
        '">Гость #' + str(vmid) + '</a> / Подтверждение</div>'
        '<section class="panel"><span class="item-label">Подтверждение операции</span>'
        '<h2>Подтвердите действие</h2>'
        '<p>Гость: <strong>' + _text(guest.get("name")) +
        '</strong> · VMID ' + str(vmid) + '</p>'
        '<div class="notice" data-tone="warning">'
        'Операция «' + _text(label) + '» изменит состояние инфраструктуры. '
        'После запуска откроется страница задания с ходом выполнения.</div>'
        '<div class="actions"><form method="POST" action="/action">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="vmid" value="{vmid}">'
        f'<input type="hidden" name="kind" value="{kind}">'
        '<button type="submit">Да, выполнить</button></form>'
        '<a class="btn secondary" href="/guests?vmid=' + str(vmid) +
        '">Отмена</a></div></section>',
        page="guests", csrf=csrf,
    )



BACKUP_STATE = {
    "verified": "Есть запись об успешной проверке", "stale": "Копия устарела",
    "missing": "Нет подтверждённой записи", "invalid": "Запись повреждена",
}
BACKUP_OVERALL = {
    "no_confirmed_backup": "Нет подтверждённой копии PVE/910",
    "stale": "Последняя копия PVE/910 устарела",
    "single_copy": "Вторая независимая копия отсутствует",
    "two_recorded_copies": "Записаны две копии; текущая доступность носителей не проверена",
}


BACKUP_REQUEST_ERRORS = {
    "permission_denied": "Нет прав для ручного запуска резервирования. Проверьте access.yaml.",
    "media_not_ready": "SSD и NAS ещё не подтверждены. Откройте «Подготовка SSD» или проверьте NAS.",
    "already_running": "Резервирование уже идёт. Повторный запуск не требуется.",
    "systemd_unavailable": "Системная служба резервирования временно недоступна.",
    "service_missing": "Служба резервирования не установлена. Проверьте развёртывание 910.",
    "start_failed": "systemd не смог запустить резервирование. Нужен журнал службы 910.",
}


class BackupRequestError(ValueError):
    """Разрешённый для отображения оператору код отказа, без сырых подробностей."""


def _backup_request(kind: str) -> dict:
    """Системный посредник возвращает только безопасные агрегированные поля."""
    if kind not in {"backup-overview", "backup-run"}:
        raise ValueError("Неизвестное действие резервирования")
    payload = json.dumps({
        "kind": kind, "vmid": 0, "request_id": secrets.token_hex(16),
    }).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(22)
        connection.connect(SOCKET)
        connection.sendall(payload)
        with connection.makefile("rb") as stream:
            raw = stream.readline(65537)
    if len(raw) > 65536 or not raw.endswith(b"\n"):
        raise ValueError("Посредник не вернул ограниченный ответ резервирования")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Неизвестный ответ посредника")
    if data.get("accepted") is not True:
        code = data.get("code")
        if kind == "backup-run" and isinstance(code, str) and code in BACKUP_REQUEST_ERRORS:
            raise BackupRequestError(BACKUP_REQUEST_ERRORS[code])
        raise ValueError("Посредник отклонил запрос резервирования")
    if kind == "backup-overview" and not isinstance(data.get("overview"), dict):
        raise ValueError("Некорректное состояние резервирования")
    return data


def _backup_overview_page(csrf: str, notice: str = "") -> bytes:
    def e(value: object) -> str:
        return html.escape(str(value) if value is not None else "—")
    try:
        data = _backup_request("backup-overview")["overview"]
    except (ValueError, OSError, TimeoutError):
        return _layout(
            '<div class="notice" data-tone="warning">Системный посредник или '
            'описание хранилищ временно недоступны.</div>'
            '<a class="btn secondary" href="/infrastructure">Повторить проверку</a>',
            csrf=csrf, page="infrastructure",
        )
    volumes = data.get("storage", {}).get("volumes", [])
    nas = data.get("nas", {}).get("volumes", [])
    dests = data.get("destinations", [])
    backup = data.get("backup", {})
    records = backup.get("records", {})
    timer = backup.get("timer", {})

    def volume_table(items: list[dict], include_size: bool = False) -> str:
        rows = []
        for item in items[:20]:
            values = [
                item.get("id"), item.get("kind"),
                (str(item.get("size_gb")) + " GB") if include_size
                and item.get("size_gb") is not None else None,
                item.get("mountpoint") or "Не указан",
                "Не проверено на физической машине",
            ]
            if not include_size:
                values.pop(2)
            rows.append("<tr>" + "".join("<td>" + e(x) + "</td>" for x in values) + "</tr>")
        cells = 5 if include_size else 4
        return "".join(rows) or '<tr><td colspan="' + str(cells) + '">Томов нет</td></tr>'

    dest_rows = []
    configured = 0
    for item in dests[:2]:
        if item.get("configured") is True:
            configured += 1
        pve_state = {
            "not_checked": "Нет связи с PVE",
            "not_registered": "Не зарегистрировано в PVE",
            "reported_active": "PVE сообщает: активно",
            "reported_inactive": "PVE сообщает: неактивно",
            "unknown": "Неизвестно",
        }.get(item.get("pve_state"), "Неизвестно")
        dest_rows.append(
            '<tr data-backup-destination="' + e(item.get("id")) + '"><td>' + e(item.get("id")) + "</td><td>"
            + e(item.get("mountpoint")) + "</td><td>"
            + '<span data-backup-configured>' + ("Объявлен и подтверждён" if item.get("configured") is True
               else "Ожидает настройки") + '</span></td><td>'
            + '<span data-backup-pve-state>' + e(pve_state) + '</span></td></tr>'
        )
    backup_rows = []
    for key, label in (
        ("core", "PVE и состояние 910"),
        ("full", "PVE / 910 / VM 100"),
        ("vm100", "VM 100 (отдельная запись)"),
    ):
        record = records.get(key, {})
        backup_rows.append(
            '<tr data-backup-record="' + key + '"><td>' + e(label) + '</td><td>'
            '<span data-backup-field="state">'
            + e(BACKUP_STATE.get(record.get("state"), "Не проверено")) + '</span></td><td>'
            '<span data-backup-field="created">'
            + _time_html(record.get("created_utc")) + '</span></td><td>'
            '<span data-backup-field="destinations">'
            + e(", ".join(record.get("destinations", [])) or "—") + '</span></td><td>'
            '<span data-backup-field="check">'
            + e("Выборочная проверка данных restic: 1%" if record.get("check")
                == "restic_subset_1_percent" else
                "Проверено при создании" if record.get("check") == "backup_checked"
                else "Не подтверждено") + '</span></td></tr>'
        )

    timer_state = {
        "active": "Работает", "inactive": "Выключен",
        "failed": "Ошибка", "unknown": "Не удалось проверить",
    }.get(timer.get("state"), "Не удалось проверить")
    service_state = {
        "active": "Выполняется", "activating": "Запускается",
        "inactive": "Ожидает запуска", "failed": "Ошибка",
        "unknown": "Не удалось проверить",
    }.get(timer.get("service"), "Не удалось проверить")
    restored = backup.get("restore_tested") is True
    last_failed = timer.get("last_result") in {"exit-code", "timeout", "signal"}
    summary = BACKUP_OVERALL.get(backup.get("overall"), "Неизвестное состояние копий")
    def card(key: str, title: str, description: str) -> str:
        record = records.get(key, {})
        state = record.get("state")
        label = {
            "verified": "Копия создана", "stale": "Копия устарела",
            "missing": "Копии пока нет", "invalid": "Нет подтверждения",
        }.get(state, "Не проверено")
        tone = "success" if state == "verified" else "warning"
        destinations = record.get("destinations")
        places = ", ".join(destinations) if isinstance(destinations, list) else ""
        return (
            '<article class="backup-copy" data-backup-copy="' + key + '" data-tone="' + tone + '">'
            '<h3>' + e(title) + '</h3>'
            '<p class="backup-copy-state" data-backup-copy-state>' + e(label) + '</p>'
            '<p class="muted">' + e(description) + '</p>'
            '<div class="backup-copy-meta">Последняя копия: '
            '<strong data-backup-copy-date>' + _time_html(record.get("created_utc")) + '</strong>'
            '</div><div class="backup-copy-meta">Хранение: '
            '<strong data-backup-copy-places>' + e(places or "—") + '</strong></div></article>'
        )

    core = records.get("core", {})
    vm100 = records.get("vm100", {})
    full = records.get("full", {})
    running = timer.get("service") in {"active", "activating"}
    complete = all(
        item.get("state") == "verified" for item in (core, vm100, full)
    )
    if running:
        headline = "Резервирование выполняется"
        headline_tone = "info"
    elif last_failed:
        headline = "Последний запуск завершился с ошибкой"
        headline_tone = "danger"
    elif complete:
        headline = "Копии PVE/910 и Home Assistant созданы"
        headline_tone = "success"
    elif core.get("state") == "verified":
        headline = "Копия PVE/910 создана; Home Assistant ещё не подтверждён"
        headline_tone = "warning"
    elif core.get("state") == "stale":
        headline = "Резервную копию PVE/910 нужно обновить"
        headline_tone = "warning"
    else:
        headline = "Подтверждённых резервных копий пока нет"
        headline_tone = "warning"
    core_places = core.get("destinations", [])
    ssd_copy = "fast-ssd" in core_places
    nas_copy = "nas-backup" in core_places
    nas_configured = any(x.get("id") == "nas-backup" and x.get("configured") for x in dests)
    media_text = (
        ("SSD: копия PVE/910 записана." if ssd_copy else "SSD: копия не подтверждена.")
        + " "
        + ("NAS: копия PVE/910 записана." if nas_copy else
           "NAS: копия не подтверждена." if nas_configured else "NAS: пока не настроен.")
    )
    plan_state = (
        "Сейчас выполняется" if running else
        "Включено · ожидает запуска" if timer.get("state") == "active" else
        "Таймер выключен" if timer.get("state") == "inactive" else
        "Нужно проверить таймер"
    )
    token = html.escape(csrf, quote=True)
    start_link = (
        '<p><a class="btn" href="/infrastructure/confirm">'
        'Запустить резервирование вручную →</a></p>'
        if configured else '<p>Запуск копирования недоступен: нет настроенных носителей.</p>'
    )
    return _layout(
        '<div class="toolbar backup-controls">'
        '<div><strong>Состояние резервных копий</strong>'
        '<div class="muted">Автообновление каждые 4 секунды, пока страница открыта.</div></div>'
        '<div class="actions"><span class="muted" id="backup-refreshed" role="status" aria-live="polite">'
        'Получаю актуальное состояние…</span>'
        '<button type="button" class="btn secondary" data-backup-refresh>↻ Обновить</button></div></div>'
        '<section id="backup-summary" class="backup-lead" data-tone="' + headline_tone + '">'
        '<h2 id="backup-summary-text" aria-live="polite">' + e(headline) + '</h2>'
        '<p>Последняя подтверждённая копия PVE/910: '
        '<strong id="backup-last-success">' + _time_html(core.get("created_utc")) + '</strong>.</p>'
        '<p class="backup-media" id="backup-media-summary">' + e(media_text) + '</p></section>'
        '<div id="backup-running" class="notice" data-tone="info" role="status"'
        + ('' if running else ' hidden') + '>'
        '<strong>Копирование идёт.</strong> Результат появится автоматически после завершения.</div>'
        + ('<div class="notice" data-tone="warning" role="alert">' + e(notice) + '</div>'
           if notice else '')
        + '<div id="backup-failure" class="notice" data-tone="danger" role="alert"'
        + ('' if last_failed else ' hidden') + '>'
        '<strong>Последнее резервирование не выполнено.</strong> '
        '<span id="backup-failure-text">'
        + e(timer.get("failure_hint") or "Подробности доступны в журнале службы 910.")
        + '</span><p>Ранее созданные копии могут оставаться доступными.</p></div>'
        '<div class="backup-copy-grid">'
        + card("core", "PVE и infra-manager 910", "Настройки и защищённое состояние PVE/910. Проверка Restic: 1% данных.")
        + card("vm100", "Home Assistant · VM 100", "Дата копии VM 100 (точное время не записывается отдельно).")
        + '</div>'
        '<p class="backup-warning">Восстановление: <strong id="backup-restore-state">'
        + ("Испытано" if restored else "Ещё не проверено")
        + '</strong>. Проверка 1% данных restic не является испытанием восстановления.</p>'
        '<section class="panel backup-schedule">'
        '<div class="section-head"><h3>Автоматическое резервирование</h3>'
        '<span class="chip" data-tone="' + (
            "success" if timer.get("state") == "active" else "warning"
        ) + '" id="backup-plan-state">' + e(plan_state) + '</span></div>'
        '<p>Следующий запуск: <strong id="backup-next-run">'
        + _systemd_time_html(timer.get("next")) + '</strong></p>'
        '<div class="actions">' + start_link
        + '<a class="btn secondary" href="/storage">Подготовка SSD</a></div></section>'
        '<details class="panel" id="backup-technical-details"><summary>Технические подробности</summary>'
        '<div class="details-content">'
        '<p>Факт записи подтверждён сохранёнными результатами Restic. Текущая доступность носителя '
        'и возможность восстановления отдельно не проверены.</p>'
        '<h3>Хранилища</h3><div class="table-wrap"><table>'
        '<tr><th>Назначение</th><th>Путь</th><th>Настройка</th><th>Носитель</th></tr>'
        + "".join(dest_rows) + '</table></div>'
        '<p>SSD: <strong id="backup-ssd-state">' + e(data.get("ssd", {}).get("state")) + '</strong>.</p>'
        '<h3>Последние записи</h3><div class="table-wrap"><table>'
        '<tr><th>Что</th><th>Состояние</th><th>Дата и время</th><th>Носители</th><th>Проверка</th></tr>'
        + "".join(backup_rows) + '</table></div>'
        '<h3>Таймер и служба</h3>'
        '<p>Таймер: <strong id="backup-timer-state">' + e(timer_state)
        + '</strong>. Служба: <strong id="backup-service-state">' + e(service_state)
        + '</strong>. Расписание: <strong id="backup-schedule">'
        + e(backup.get("schedule_declared")) + '</strong>.</p>'
        '<p>Последний запуск: <span id="backup-last-run">'
        + _systemd_time_html(timer.get("last")) + '</span>. Результат systemd: '
        '<span id="backup-last-result">' + e(timer.get("last_result")) + '</span>.</p>'
        '<p>Журнал: <code>pct exec 910 -- journalctl -u '
        'infra-manager-backup.service -n 40 --no-pager</code></p>'
        '<details><summary>Общие тома PVE</summary>'
        '<div class="table-wrap"><table>'
        '<tr><th>Том</th><th>Тип</th><th>Размер</th><th>Подключение</th><th>Проверка</th></tr>'
        + volume_table(volumes, include_size=True) + '</table></div></details>'
        '<details><summary>Тома NAS</summary><p>Объявлены в Git; NAS не опрашивается напрямую.</p>'
        '<div class="table-wrap"><table>'
        '<tr><th>Том</th><th>Тип</th><th>Подключение</th><th>Проверка</th></tr>'
        + volume_table(nas) + '</table></div></details>'
        '</div></details>'
        '<script src="/backup-refresh.js" defer></script>',
        csrf=csrf, page="infrastructure",
    )


def _backup_confirm_page(csrf: str) -> bytes:
    token = html.escape(csrf, quote=True)
    return _layout(
        '<div class="breadcrumbs"><a href="/infrastructure">Хранилища</a> / Подтверждение</div>'
        '<section class="panel"><h2>Запустить резервирование</h2>'
        '<p>Штатная служба infra-manager-backup.service проверит носители, '
        'выполнит зашифрованную запись и проверку данных restic. '
        'Операция может выполняться длительное время.</p>'
        '<div class="notice" data-tone="warning">Если доступен только один '
        'проверенный носитель, будет создана одна копия. '
        'Восстановление и форматирование здесь не выполняются.</div>'
        '<div class="actions"><form method="POST" action="/infrastructure/run">'
        f'<input type="hidden" name="csrf" value="{token}">'
        '<button type="submit">Да, начать резервирование</button></form>'
        '<a class="btn secondary" href="/infrastructure">Отмена</a>'
        '</div></section>',
        page="infrastructure", csrf=csrf,
    )



def _send_action(kind: str, vmid: int, request_id: str) -> dict:
    if kind not in {"status", "sync", "repair", "deploy"} or (
        not isinstance(vmid, int) or not 1 <= vmid <= 999999
    ) or not REQ_ID.fullmatch(request_id):
        raise ValueError("Недопустимая операция")
    payload = json.dumps(
        {"kind": kind, "vmid": vmid, "request_id": request_id}
    ).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(SOCKET)
        connection.sendall(payload)
        output = bytearray()
        while not output.endswith(b"\n") and len(output) <= 2048:
            block = connection.recv(2049 - len(output))
            if not block:
                break
            output.extend(block)
    if len(output) > 2048 or not output.endswith(b"\n"):
        raise ValueError("Посредник не подтвердил операцию")
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("accepted") is not True:
        raise ValueError("Посредник отклонил операцию")
    if result.get("id") != request_id:
        raise ValueError("Ответ относится к другому заданию")
    return result


def _send_storage(kind: str, *, plan_id: str = "", serial: str = "") -> dict:
    """Два разрешённых запроса к корневому посреднику, не прямой PVE API."""
    if kind not in {"storage-plan", "storage-confirm"}:
        raise ValueError("Недопустимый тип операции над диском")
    fields = {"kind": kind, "vmid": 0, "request_id": secrets.token_hex(16)}
    if kind == "storage-confirm":
        if not re.fullmatch(r"[0-9a-f]{64}", plan_id) or not re.fullmatch(
            r"[A-Za-z0-9._-]{6,128}", serial,
        ):
            raise ValueError("Неверные параметры подтверждения")
        fields.update({"plan_id": plan_id, "serial": serial})
    payload = json.dumps(fields).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(15)
        connection.connect(SOCKET)
        connection.sendall(payload)
        output = bytearray()
        while not output.endswith(b"\n") and len(output) <= 2048:
            part = connection.recv(2049 - len(output))
            if not part:
                break
            output.extend(part)
    if len(output) > 2048 or not output.endswith(b"\n"):
        raise ValueError("Посредник не подтвердил план SSD")
    result = json.loads(output)
    if not isinstance(result, dict) or result.get("accepted") is not True:
        raise ValueError("Проверка диска или разрешения отклонена")
    return result


def _storage_page(csrf: str, notice: str = "") -> bytes:
    """Административное подтверждение сохраняет лишь однократный допуск."""
    token = html.escape(csrf, quote=True)
    content = ('<p class="page-intro">Проверь диск и его состояние перед '
               'любой подготовкой. Эта операция не нужна для обычного '
               'просмотра резервных копий.</p>')
    if notice:
        content += '<div class="notice" role="status">' + html.escape(notice) + '</div>'
    try:
        record = _send_storage("storage-plan").get("storage", {})
    except (ValueError, OSError, TimeoutError):
        return _layout(content + "<p>Сейчас не удалось проверить SSD. Диск не изменён.</p>",
                       page="storage", csrf=csrf)
    if not isinstance(record, dict):
        return _layout(content + "<p>Неизвестное состояние SSD.</p>",
                       page="storage", csrf=csrf)
    state = record.get("state")
    if state == "running":
        content += (
            "<p>Подготовка SSD выполняется самостоятельно.</p>"
            "<p>Можно закрыть браузер; операция продолжится.</p>"
            '<p><a href="/storage">Обновить состояние</a></p>'
        )
    elif state == "configured":
        content += (
            "<p>SSD подготовлен и находится под контролем infra-manager.</p>"
            "<p>Точка подключения: " + html.escape(str(record.get("mountpoint"))) + "</p>"
            "<p>UUID: " + html.escape(str(record.get("uuid"))) + "</p>"
            "<p>При повторном deploy диск не форматируется.</p>"
            "<p>Подготовка завершена успешно.</p>"
        )
    elif state == "needs-confirmation":
        content += (
            "<p>Подготовка диска ожидает отдельного подтверждения.</p>"
            '<div class="notice" data-tone="danger" role="alert"><strong>'
            'ВНИМАНИЕ: создание GPT/ext4 уничтожит прежние данные выбранного устройства. '
            'Перед операцией параметры будут проверены снова.</strong></div>'
            "<p>Модель: " + html.escape(str(record.get("model"))) + "</p>"
            "<p>Путь: " + html.escape(str(record.get("devpath"))) + "</p>"
            "<p>Объём (байт): " + html.escape(str(record.get("size_bytes"))) + "</p>"
            "<p>Серийный номер: " + html.escape(str(record.get("serial"))) + "</p>"
            "<p>Раздел: GPT + ext4; mount: /mnt/pve/backup-fast</p>"
            '<form method="POST" action="/storage-confirm">'
            f'<input type="hidden" name="csrf" value="{token}">'
            '<input type="hidden" name="plan_id" value="'
            + html.escape(str(record.get("plan_id")), quote=True) + '">'
            '<input type="hidden" name="serial" value="'
            + html.escape(str(record.get("serial")), quote=True) + '">'
            '<button class="danger" type="submit">Да, подготовить SSD</button>'
            '</form>'
            '<p>После подтверждения система сама выполнит подготовку. '
            'Можно закрыть браузер и позднее проверить результат здесь.</p>'
        )
    else:
        content += (
            "<p>Подготовка заблокирована: "
            + html.escape(str(record.get("reason", "нужна проверка устройства")))
            + ". Никаких изменений на диске нет.</p>"
        )
    return _layout('<section class="panel">' + content + '</section>',
                   auto_refresh=state == "running", page="storage", csrf=csrf)


def _text(value: object) -> str:
    return html.escape(str(value) if value is not None else "—")


def _time_html(value: object) -> str:
    """ISO с зоной отдаётся браузеру; без зоны не подменять часовой пояс."""
    if not isinstance(value, str) or not value:
        return "—"
    if re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?"
        r"(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)", value,
    ):
        safe = html.escape(value, quote=True)
        return f'<time class="time" data-local-time datetime="{safe}">{safe}</time>'
    if re.fullmatch(r"\d{4}-\d\d-\d\d", value):
        year, month, day = value.split("-")
        return f'<time datetime="{value}">{day}.{month}.{year}</time>'
    return '<span title="Время сервера; часовой пояс неизвестен">' + _text(value) + '</span>'



def _systemd_time_html(value: object) -> str:
    """Сделать systemd wall-clock удобочитаемым, не угадывая UTC offset.

    Например, "Tue 2026-10-20 21:30:00 MSK" -> "20.10.2026 · 21:30 MSK".
    Для таких строк смещение не известно, поэтому браузер их не сдвигает.
    """
    if not isinstance(value, str) or not value or value == "n/a":
        return "—"
    found = re.fullmatch(
        r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
        r"(\d{4})-(\d{2})-(\d{2})\s+"
        r"(\d{2}):(\d{2}):\d{2}\s+([A-Z][A-Z0-9+-]{1,9})",
        value,
    )
    if found:
        year, month, day, hours, minutes, zone = found.groups()
        return (
            '<span title="' + html.escape(value, quote=True) + ' · время сервера">'
            + f"{day}.{month}.{year} · {hours}:{minutes} {zone}</span>"
        )
    return '<span title="Время сервера; смещение не подтверждено">' + _text(value) + '</span>'



def _chip(value: str, *, category: str = "operation") -> str:
    if category == "guest":
        tone = {
            "running": "success", "stopped": "warning", "paused": "warning",
            "conflict": "danger", "absent": "danger", "unavailable": "warning",
        }.get(value, "")
        label = GUEST_STATES.get(value, "Не проверено")
    else:
        tone = {
            "completed": "success", "failed": "danger",
            "needs-recovery": "danger", "rolled-back": "warning",
            "running": "info", "activating": "info", "verifying": "info",
            "queued": "warning",
        }.get(value, "")
        label = PHASES.get(value, "Неизвестно")
    return '<span class="chip" data-tone="' + tone + '">' + _text(label) + '</span>'


def _section(title: str, body: str, *, extra: str = "") -> str:
    return '<section class="panel"><div class="section-head"><h3>' + _text(title) + (
        '<span>' + extra + '</span>' if extra else ""
    ) + '</div>' + body + '</section>'


def _layout(
    inner: str, *, auto_refresh: bool = False,
    csrf: str | None = None, page: str = "overview",
    authenticated: bool = True,
) -> bytes:
    """Один шаблон для всех страниц, без CDN, фреймворков и inline-JavaScript."""
    title = {
        "overview": "Обзор", "guests": "Гости",
        "operations": "Задания", "infrastructure": "Хранилища и бэкапы",
        "storage": "Подготовка SSD", "login": "Вход",
    }.get(page, "Инфраструктура")
    nav = (
        ("overview", "/", "⌂", "Обзор"),
        ("guests", "/guests", "▦", "Гости"),
        ("operations", "/#recent-operations", "◷", "Задания"),
        ("infrastructure", "/infrastructure", "▤", "Хранилища и бэкапы"),
        ("storage", "/storage", "◈", "Подготовка SSD"),
    )
    links = "".join(
        '<a href="' + href + '"' + (' aria-current="page"' if section == page else "") +
        '><span class="nav-symbol" aria-hidden="true">' + icon +
        '</span>' + name + '</a>'
        for section, href, icon, name in nav
    )
    logout = (
        '<form class="logout-form" method="POST" action="/logout">'
        '<input type="hidden" name="csrf" value="' + html.escape(csrf, quote=True) + '">'
        '<button type="submit">↪ Выйти из панели</button></form>'
        if csrf else '<a href="/">На главную</a>'
    )
    auto = '<meta http-equiv="refresh" content="5;url=/storage">' if auto_refresh else ""
    head = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="theme-color" content="#111b2b">' + auto
        + '<title>infra-manager · ' + _text(title) + '</title>'
        '<link rel="stylesheet" href="/ui.css"></head><body>'
        '<a class="skip-link" href="#content">К основному содержимому</a>'
    )
    if not authenticated:
        return (head + '<div class="login-page"><div class="login-panel">'
                '<div class="brand"><span class="brand-mark">⌘</span>'
                '<span class="brand-label">infra-manager'
                '<span class="brand-small">Управление инфраструктурой</span></span></div>'
                + inner + '</div></div></body></html>').encode()
    return (
        head + '<div class="shell"><aside class="sidebar">'
        '<a class="brand" href="/"><span class="brand-mark">⌘</span>'
        '<span class="brand-label">infra-manager'
        '<span class="brand-small">Панель Proxmox · 910</span></span></a>'
        '<nav class="nav" aria-label="Основные разделы">'
        '<span class="nav-label">Рабочая область</span>' + links + '</nav>'
        '<div class="sidebar-foot"><strong>Часовой пояс</strong>'
        '<label for="timezone-select">Время на страницах</label>'
        '<select id="timezone-select" aria-label="Часовой пояс">'
        '<option value="local">Моё время (браузер)</option>'
        '<option value="Europe/Moscow">Москва</option>'
        '<option value="UTC">UTC</option></select>'
        '<small class="tz-label" id="timezone-label">Время браузера</small>'
        + logout + '</div></aside>'
        '<div class="workspace"><header class="topbar">'
        '<div><div class="topbar-kicker">Инфраструктура / ' + _text(title) +
        '</div><h1 class="topbar-title">' + _text(title) + '</h1></div>'
        '<div class="topbar-actions"><a class="btn secondary" href="/">Обзор</a>'
        '<a class="btn secondary" href="/guests">Все гости</a></div></header>'
        '<main id="content">' + inner + '</main></div></div>'
        '<script src="/ui.js" defer></script></body></html>'
    ).encode()


OPERATION_NAMES = {
    "deploy": "Развёртывание", "status": "Проверка",
    "sync": "Синхронизация", "repair": "Исправление",
}


def _dashboard(csrf: str, notice: str = "") -> bytes:
    """Обзор и недавние задания без опасной формы произвольного deploy."""
    try:
        records = _operation_history()
    except (OSError, ValueError, TimeoutError):
        records = []
        notice = (notice + " " if notice else "") + (
            "История операций временно недоступна; проверьте системный посредник."
        )
    visible = [
        item for item in records
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and REQ_ID.fullmatch(item["id"])
    ]
    phases = [item.get("phase") for item in visible]
    active = sum(phase in {"queued", "running", "activating", "verifying"} for phase in phases)
    failed = sum(phase in {"failed", "needs-recovery"} for phase in phases)
    successful = phases.count("completed")
    alerts = (
        '<div class="notice" data-tone="warning" role="status">' + _text(notice) + '</div>'
        if notice else ""
    )
    stats = "".join(
        '<div class="stat" data-tone="' + tone + '">'
        '<div class="stat-label">' + title + '</div>'
        '<div class="stat-value">' + str(number) + '</div>'
        '<div class="stat-detail">Среди последних ' + str(len(visible)) + ' заданий</div></div>'
        for title, number, tone in (
            ("Всего записей", len(visible), ""),
            ("Выполняются", active, "info"),
            ("Успешно", successful, "success"),
            ("Нужны внимание", failed, "danger" if failed else ""),
        )
    )
    shortcuts = "".join(
        '<a class="card-link" href="' + url + '">'
        '<span class="item-label">' + category + '</span>'
        '<div class="card-heading">' + label + ' →</div>'
        '<div class="muted">' + description + '</div></a>'
        for url, category, label, description in (
            ("/guests", "Управление", "Гости", "Состояния VM/LXC, проверка и обновление"),
            ("/infrastructure", "Хранение", "Резервные копии", "SSD, NAS и последние результаты"),
            ("/storage", "Обслуживание", "Подготовка SSD", "Просмотр и подтверждение диска"),
        )
    )
    rows = []
    for record in visible[:40]:
        identifier = record["id"]
        vmid = record.get("target")
        guest = (
            '<a href="/guests?vmid=' + str(vmid) + '">Гость ' + str(vmid) + '</a>'
            if type(vmid) is int and 1 <= vmid <= 999999 else "—"
        )
        kind = record.get("kind")
        phase = record.get("phase")
        label = OPERATION_NAMES.get(kind, "Операция")
        created = _time_html(record.get("created_at"))
        group = (
            "active" if phase in {"queued", "running", "activating", "verifying"}
            else "attention" if phase in {"failed", "needs-recovery"}
            else "other"
        )
        rows.append(
            '<tr data-filter-item data-state="' + _text(phase) +
            '" data-group="' + group + '"'
            ' data-search="' + _text(f"{identifier} {vmid} {label} {PHASES.get(phase, '')}") + '">'
            '<td><a class="cell-title" href="/operations?id=' + identifier +
            '">' + _text(label) + '</a><span class="cell-sub code">' +
            identifier[:8] + '</span></td>'
            '<td>' + guest + '</td><td>' + _chip(phase) + '</td>'
            '<td>' + created + '</td></tr>'
        )
    history = (
        '<div class="toolbar"><input type="search" aria-label="Поиск заданий" '
        'placeholder="Найти по VMID или действию…" data-search-for="operations">'
        '<select aria-label="Статус заданий" data-state-for="operations">'
        '<option value="all">Все состояния</option>'
        '<option value="active">Выполняются</option>'
        '<option value="completed">Успешные</option>'
        '<option value="attention">Требуют внимания</option>'
        '</select><span class="muted" data-count-for="operations">'
        + str(len(rows)) + ' из ' + str(len(rows)) + '</span></div>'
        '<div class="table-wrap"><table>'
        '<thead><tr><th>Задание</th><th>Гость</th><th>Состояние</th><th>Начато</th></tr></thead>'
        '<tbody data-filter-list="operations">' + "".join(rows) + '</tbody></table></div>'
        '<p class="empty" data-empty-for="operations" hidden>Ничего не найдено. Измени фильтр.</p>'
        if rows else '<p class="empty">Заданий пока нет. Открой нужного гостя для проверки или развёртывания.</p>'
    )
    return _layout(
        '<p class="page-intro">Всё важное под рукой: что выполняется, где возникла ошибка '
        'и что требует действия. Управление гостями — в их карточках.</p>'
        + alerts + '<div class="grid cols-4">' + stats + '</div>'
        '<div class="section-head"><h2>Быстрый переход</h2></div>'
        '<div class="grid cols-3">' + shortcuts + '</div>'
        '<section class="panel" id="recent-operations">'
        '<div class="section-head"><h2>Последние задания</h2>'
        '<span class="muted">Новые сверху</span></div>' + history + '</section>',
        csrf=csrf, page="overview",
    )


def _public_versions() -> str:
    """Публичные версии без токенов, паролей и доступа к Docker socket.

    Версия infra-web берётся из образа, остальные — из декларации deploy.
    Они не выдаются за результаты проверки запущенных контейнеров.
    """
    built = os.environ.get("INFRA_WEB_BUILD_GIT_REVISION", "")
    target = os.environ.get("INFRA_GIT_REVISION", "")
    built_content = os.environ.get("INFRA_WEB_BUILD_REVISION", "")
    target_content = os.environ.get("INFRA_WEB_CONTENT_REVISION", "")

    def git_revision(value: str) -> str:
        return value[:12] if re.fullmatch(r"[0-9a-f]{40}", value) else "не определена"

    def version(name: str) -> str:
        value = os.environ.get(name, "")
        return value[:80] if re.fullmatch(r"[a-zA-Z0-9._+-]{1,80}", value) else "не определена"

    rows = [
        ("infra-web (образ)", f"v1, Git {git_revision(built)}"),
        ("infra-runtime (конфигурация)", version("INFRA_RUNTIME_VERSION")),
        ("OpenBao (конфигурация)", version("INFRA_OPENBAO_VERSION")),
        ("OpenTofu (конфигурация)", version("INFRA_OPENTOFU_VERSION")),
        ("Ansible (конфигурация)", version("INFRA_ANSIBLE_VERSION")),
        ("Packer (конфигурация)", version("INFRA_PACKER_VERSION")),
        ("Git (целевая версия)", git_revision(target)),
    ]
    warning = (
        "<p><strong>Внимание: версия веб-панели отличается от целевой. "
        "Проверьте завершение deploy 910.</strong></p>"
        if re.fullmatch(r"[0-9a-f]{40}", built_content)
        and re.fullmatch(r"[0-9a-f]{40}", target_content)
        and built_content != target_content else ""
    )
    items = "".join(
        "<dt>" + html.escape(label) + "</dt><dd>" + html.escape(value) + "</dd>"
        for label, value in rows
    )
    return (
        '<section class="versions" aria-label="Версии компонентов">'
        "<h3>Версии компонентов</h3>" + warning
        + "<dl>" + items + "</dl>"
        "<p>Кроме infra-web, указаны версии из конфигурации развёртывания, "
        "а не результаты онлайн-проверки контейнеров.</p></section>"
    )


def _login() -> bytes:
    return _layout(
        "<h2>Вход через OpenBao</h2>"
        '<form method="POST" action="/login">'
        '<label>Логин <input name="username" autocomplete="username" required></label>'
        '<label>Пароль <input type="password" name="password" '
        'autocomplete="current-password" required></label>'
        '<label class="remember-option"><input type="checkbox" name="remember" value="30d">'
        'Запомнить вход на 30 дней на этом устройстве</label>'
        '<p class="muted">На общем компьютере не включай запоминание. '
        'Выход из панели сразу отзывает этот вход.</p>'
        '<button>Войти</button></form>'
        + _public_versions(),
        page="login", authenticated=False,
    )


class Handler(BaseHTTPRequestHandler):
    """Только известные маршруты, короткие тела запросов и закрытая сессия."""

    def log_message(self, fmt: str, *args) -> None:
        # URL запроса и данные пользователя не записываются в журнал контейнера.
        return

    def _reply(
        self, status: HTTPStatus, body: bytes, *,
        content_type: str = "text/html; charset=utf-8", cookie: str | None = None,
        location: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        # HTML POST-forms require their true Origin; no-referrer makes it null.
        # Only disclose Referer within our own HTTPS origin.
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Strict-Transport-Security", "max-age=3600")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'",
        )
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> tuple[str, dict] | None:
        return _session(self.headers.get("Cookie"))

    def _same_origin(self) -> bool:
        return (
            self.headers.get("Host") == WEB_HOST
            and self.headers.get("Origin") == "https://" + WEB_HOST
        )

    def _form(self) -> dict[str, str]:
        if self.headers.get("Content-Type", "").split(";", 1)[0] != (
            "application/x-www-form-urlencoded"
        ):
            raise ValueError("Неизвестный тип запроса")
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MAX_BODY:
            raise ValueError("Неверный размер запроса")
        parsed = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        if any(len(values) != 1 for values in parsed.values()):
            raise ValueError("Повторный параметр")
        return {key: value[0] for key, value in parsed.items()}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/health":
            self._reply(
                HTTPStatus.OK, b"ok\n",
                content_type="text/plain; charset=utf-8",
            )
            return
        if path == "/ui.css":
            try:
                style = Path(__file__).with_name("ui.css").read_bytes()
            except OSError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"styles unavailable\n")
                return
            self._reply(HTTPStatus.OK, style, content_type="text/css; charset=utf-8")
            return
        session = self._authorized()
        if path == "/ui.js":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            try:
                script = Path(__file__).with_name("ui.js").read_bytes()
            except OSError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"interface unavailable\n")
                return
            self._reply(HTTPStatus.OK, script, content_type="text/javascript; charset=utf-8")
            return
        if path == "/" or path == "/login":
            self._reply(
                HTTPStatus.OK,
                _dashboard(session[1]["csrf"]) if session else _login(),
            )
        elif path in {"/guests", "/guests/confirm", "/api/guests", "/api/guest"}:
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if path in {"/guests", "/api/guests"} and not query:
                if path == "/guests":
                    self._reply(HTTPStatus.OK, _guests_page(session[1]["csrf"]))
                else:
                    try:
                        data = _guest_catalog("guests")
                    except (ValueError, OSError, TimeoutError):
                        self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"catalog unavailable\n")
                        return
                    self._reply(HTTPStatus.OK, json.dumps(data, ensure_ascii=False).encode(),
                                content_type="application/json; charset=utf-8")
                return
            required = {"vmid", "kind"} if path == "/guests/confirm" else {"vmid"}
            if set(query) != required or any(len(values) != 1 for values in query.values()):
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid query\n")
                return
            try:
                raw = query["vmid"][0]
                if not raw.isascii() or not raw.isdecimal() or len(raw) > 6:
                    raise ValueError("VMID must be numeric")
                vmid = int(raw)
                if not 1 <= vmid <= 999999:
                    raise ValueError("VMID out of range")
                if path == "/guests/confirm":
                    page = _guest_confirmation(vmid, query["kind"][0], session[1]["csrf"])
                    self._reply(HTTPStatus.OK, page)
                elif path == "/api/guest":
                    data = _guest_catalog("guest-detail", vmid)
                    self._reply(HTTPStatus.OK, json.dumps(data, ensure_ascii=False).encode(),
                                content_type="application/json; charset=utf-8")
                elif path == "/guests":
                    self._reply(HTTPStatus.OK, _guest_page(vmid, session[1]["csrf"]))
                else:
                    self._reply(HTTPStatus.BAD_REQUEST, b"invalid query\n")
            except ValueError:
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid guest or action\n")
            except (OSError, TimeoutError):
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"guest unavailable\n")
        elif path in {"/infrastructure", "/infrastructure/confirm", "/api/infrastructure"}:
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
            if (query and not (
                    path == "/infrastructure" and set(query) == {"started"}
                    and query["started"] == ["1"])):
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid query\n")
                return
            if path == "/infrastructure/confirm":
                self._reply(HTTPStatus.OK, _backup_confirm_page(session[1]["csrf"]))
                return
            if path == "/infrastructure":
                notice = ("Запрос отправлен службе systemd; "
                          "результат ещё не подтверждён."
                          if query else "")
                self._reply(HTTPStatus.OK, _backup_overview_page(
                    session[1]["csrf"], notice,
                ))
                return
            try:
                overview = _backup_request("backup-overview")
            except (OSError, ValueError, TimeoutError):
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"backup overview unavailable\n")
                return
            self._reply(HTTPStatus.OK, json.dumps(overview["overview"], ensure_ascii=False).encode(),
                        content_type="application/json; charset=utf-8")
        elif path == "/storage":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            self._reply(HTTPStatus.OK, _storage_page(session[1]["csrf"]))
        elif path == "/backup-refresh.js":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            try:
                script = Path(__file__).with_name("backup-refresh.js").read_bytes()
            except OSError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"backup interface unavailable\n")
                return
            self._reply(HTTPStatus.OK, script, content_type="text/javascript; charset=utf-8")
        elif path == "/diagnostics.js":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\\n")
                return
            try:
                script = Path(__file__).with_name("diagnostics.js").read_bytes()
            except OSError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"diagnostics unavailable\n")
                return
            self._reply(HTTPStatus.OK, script, content_type="text/javascript; charset=utf-8")
        elif path in {"/operations", "/api/operation"}:
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\\n")
                return
            values = parse_qs(urlsplit(self.path).query)
            operation_id = values.get("id", [""])[0]
            if set(values) != {"id"} or len(values["id"]) != 1 or not REQ_ID.fullmatch(operation_id):
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid operation id\\n")
                return
            if path == "/operations":
                self._reply(HTTPStatus.OK, _operation_page(operation_id, session[1]["csrf"]))
                return
            try:
                detail = _operation_detail(operation_id)
            except (OSError, ValueError, TimeoutError):
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"diagnostics unavailable\\n")
                return
            content = json.dumps(detail, ensure_ascii=False).encode()
            self._reply(HTTPStatus.OK, content, content_type="application/json; charset=utf-8")
        elif path == "/api/operations":
            if not session:
                self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
                return
            try:
                history = _operation_history()
            except (OSError, ValueError, TimeoutError):
                self._reply(
                    HTTPStatus.SERVICE_UNAVAILABLE, b"operation history unavailable\n",
                    content_type="text/plain; charset=utf-8",
                )
                return
            data = json.dumps(history, ensure_ascii=False).encode()
            self._reply(
                HTTPStatus.OK, data, content_type="application/json; charset=utf-8"
            )
        else:
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._same_origin():
            self._reply(HTTPStatus.FORBIDDEN, b"origin rejected\n")
            return
        try:
            form = self._form()
        except (ValueError, UnicodeError):
            self._reply(HTTPStatus.BAD_REQUEST, b"invalid form\n")
            return
        if path == "/login":
            if set(form) not in ({"username", "password"},
                                  {"username", "password", "remember"}):
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid login form\n")
                return
            if "remember" in form and form["remember"] != "30d":
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid remember option\n")
                return
            remember = form.get("remember") == "30d"
            ip = self.client_address[0]
            if not _allow_attempt(ip):
                self._reply(HTTPStatus.TOO_MANY_REQUESTS, b"attempt limit\n")
                return
            if not _authenticate(form.get("username", ""), form.get("password", "")):
                self._reply(HTTPStatus.UNAUTHORIZED, _login())
                return
            with guard:
                attempts.pop(ip, None)
            try:
                token, session = _new_session(remember=remember)
            except ValueError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"session limit\n")
                return
            # Post/Redirect/Get: при F5 браузер не отправит пароль повторно.
            self._reply(
                HTTPStatus.SEE_OTHER, b"",
                cookie=(f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/"
                        + (f"; Max-Age={REMEMBER_TTL}" if remember else "")),
                location="/",
            )
            return
        auth = self._authorized()
        if not auth:
            self._reply(HTTPStatus.UNAUTHORIZED, b"login required\n")
            return
        token, session = auth
        if not secrets.compare_digest(form.get("csrf", ""), session["csrf"]):
            self._reply(HTTPStatus.FORBIDDEN, b"csrf rejected\n")
            return
        if path == "/logout":
            try:
                _revoke_session(token, session)
            except ValueError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"logout temporarily unavailable\n")
                return
            # Тот же PRG после выхода; защищённую форму нельзя повторить F5.
            self._reply(
                HTTPStatus.SEE_OTHER, b"",
                cookie=f"{COOKIE}=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/",
                location="/",
            )
            return
        if path == "/infrastructure/run":
            if set(form) != {"csrf"}:
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid backup form\n")
                return
            try:
                _backup_request("backup-run")
            except BackupRequestError as exc:
                self._reply(HTTPStatus.CONFLICT, _backup_overview_page(
                    session["csrf"], str(exc),
                ))
                return
            except (OSError, ValueError, TimeoutError):
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, _backup_overview_page(
                    session["csrf"],
                    "Не удалось обратиться к системному посреднику. "
                    "Проверьте infra-manager status 910.",
                ))
                return
            self._reply(HTTPStatus.SEE_OTHER, b"", location="/infrastructure?started=1")
            return
        if path == "/storage-confirm":
            try:
                if set(form) != {"csrf", "plan_id", "serial"}:
                    raise ValueError("Неизвестные поля подтверждения")
                _send_storage(
                    "storage-confirm", plan_id=form["plan_id"], serial=form["serial"],
                )
            except (ValueError, OSError, TimeoutError):
                self._reply(HTTPStatus.BAD_REQUEST, _storage_page(
                    session["csrf"], "Разрешение не принято: перепроверьте диск и план",
                ))
                return
            self._reply(HTTPStatus.OK, _storage_page(
                session["csrf"], "Подготовка SSD запущена. Можешь закрыть браузер — работа продолжится.",
            ))
            return
        if path != "/action":
            self._reply(HTTPStatus.NOT_FOUND, b"not found\n")
            return
        try:
            kind = form.get("kind", "")
            vmid = int(form.get("vmid", ""))
            request_id = form.get("request_id") or secrets.token_hex(16)
            result = _send_action(kind, vmid, request_id)
        except (ValueError, OSError, TimeoutError):
            self._reply(
                HTTPStatus.BAD_REQUEST, _dashboard(session["csrf"], "Задание не принято")
            )
            return
        # Post/Redirect/Get: обновление страницы больше не повторяет POST.
        self._reply(
            HTTPStatus.SEE_OTHER, b"",
            location="/operations?id=" + result["id"],
        )


def main() -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(CERT, KEY)
    with ThreadingHTTPServer(("0.0.0.0", 8443), Handler) as server:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
