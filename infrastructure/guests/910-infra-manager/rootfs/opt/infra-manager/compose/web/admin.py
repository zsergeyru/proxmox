"""Административная панель infra-manager: HTTPS, OpenBao, закрытая сессия.

Пароли проверяются OpenBao userpass и не хранятся в контейнере.
Команды передаются в проверяемый системный посредник по Unix-сокету.
"""

from __future__ import annotations

import html
import json
import os
from pathlib import Path
import re
import secrets
import socket
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
    with guard:
        session = sessions.get(token)
        if session and session["expires"] > time.monotonic():
            return token, session
        sessions.pop(token, None)
    return None


def _new_session() -> tuple[str, dict]:
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    session = {"csrf": secrets.token_urlsafe(32), "expires": now + SESSION_TTL}
    with guard:
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


def _operation_page(operation_id: str) -> bytes:
    """Показать текущий снимок и оставить обновление отдельному HTTPS-запросу."""
    try:
        result = _operation_detail(operation_id)
    except (OSError, ValueError, TimeoutError):
        result = {"operation": {"phase": "недоступно"}, "events": [], "logs": [],
                  "diagnostic": "Системный посредник недоступен. Повторяем проверку автоматически."}
    record = result["operation"]
    events = result["events"]
    def clean(value: object) -> str:
        return html.escape(str(value) if value is not None else "—")
    phases = "".join(
        "<li>" + clean(event.get("at", "")) + " — "
        + clean(PHASES.get(event.get("phase"), "Неизвестно"))
        + "</li>" for event in events if isinstance(event, dict)
    ) or "<li>Этапы ещё не записаны</li>"
    logs = "\n".join(str(item) for item in result["logs"] if isinstance(item, str))
    body = (
        '<p><a href="/">← Ко всем заданиям</a></p>'
        '<h2>Диагностика задания</h2>'
        '<p>Идентификатор: <code>' + operation_id + '</code></p>'
        '<section id="operation-view" data-operation-id="' + operation_id + '">'
        '<p>Операция: <strong id="op-kind">' + clean(record.get("kind")) + '</strong>'
        ' · Гость: <strong id="op-target">' + clean(record.get("target")) + '</strong></p>'
        '<p>Состояние: <strong id="op-phase">'
        + clean(PHASES.get(record.get("phase"), record.get("phase"))) + '</strong></p>'
        '<p>Начало: <span id="op-start">' + clean(record.get("started_at") or record.get("created_at")) + '</span>'
        ' · Завершение: <span id="op-finish">' + clean(record.get("finished_at")) + '</span>'
        ' · Код: <span id="op-exit">' + clean(record.get("exit_code")) + '</span></p>'
        '<p id="op-diagnostic">' + clean(result.get("diagnostic", "")) + '</p>'
        '<h3>Этапы выполнения</h3>'
        '<p>Продолжительность каждого этапа обновляется при поступлении новых событий.</p>'
        '<ol id="op-timeline">' + phases + '</ol>'
        '<h3>Журнал задания (без секретов)</h3>'
        '<p>Доступны только безопасные служебные сообщения. Полный журнал остаётся внутри 910.</p>'
        '<pre id="op-log" class="diagnostic-log">' + html.escape(logs) + '</pre>'
        '<p id="op-refresh">Обновление каждые 4 секунды.</p>'
        '</section>'
        '<script src="/diagnostics.js" defer></script>'
    )
    return _layout(body)




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


def _guests_page() -> bytes:
    try:
        data = _guest_catalog("guests")
        guests = data["guests"]
        warning = (
            "" if data.get("pve_available") is True
            else "<p>Нет связи с PVE: состояния не подтверждены. "
                 "Описание гостей из Git остаётся доступным.</p>"
        )
        content = (
            '<p><a href="/">← Задания</a></p>'
            '<h2>Гости Proxmox</h2>'
            '<p>Список сформирован из guest.yaml/provision.yaml проекта. '
            'Состояния PVE проверяются отдельно.</p>' + warning
            + '<p>Гостей в каталоге: ' + str(len(guests))
            + ' · <a href="/guests">Обновить состояния</a></p>'
            '<table><thead><tr><th>VMID</th><th>Имя</th><th>Роль</th>'
            '<th>Тип</th><th>IP</th><th>Состояние</th></tr></thead><tbody>'
            + _guest_rows(guests) + '</tbody></table>'
        )
    except (OSError, ValueError, TimeoutError):
        content = (
            '<p><a href="/">← Задания</a></p><h2>Гости Proxmox</h2>'
            '<p>Каталог временно недоступен. Проверьте системный посредник '
            'и повторите запрос.</p><p><a href="/guests">Повторить</a></p>'
        )
    return _layout(content)


def _guest_page(vmid: int, csrf: str) -> bytes:
    try:
        data = _guest_catalog("guest-detail", vmid)
    except (OSError, ValueError, TimeoutError):
        return _layout(
            '<p><a href="/guests">← Все гости</a></p>'
            '<h2>Гость ' + str(vmid) + '</h2>'
            '<p>Пока нельзя прочитать карточку. Попробуйте обновить страницу.</p>'
        )
    guest = data["guest"]
    actions = guest.get("actions", [])
    if not isinstance(actions, list):
        actions = []
    token = html.escape(csrf, quote=True)
    commands = []
    for kind in GUEST_ACTION_LABELS:
        if kind not in actions:
            continue
        label = GUEST_ACTION_LABELS[kind]
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
                f'<button type="submit">{html.escape(label)}</button></form>'
            )
    history = []
    for row in data["operations"]:
        identifier = row.get("id")
        if not isinstance(identifier, str) or not REQ_ID.fullmatch(identifier):
            continue
        phase = PHASES.get(row.get("phase"), "Неизвестно")
        history.append(
            '<tr><td><a href="/operations?id=' + identifier + '">'
            + identifier[:8] + '</a></td><td>'
            + html.escape(str(row.get("kind", ""))) + '</td><td>'
            + html.escape(phase) + '</td><td>'
            + html.escape(str(row.get("created_at", ""))) + '</td></tr>'
        )
    notice = (
        "<p>Не удалось прочитать историю операций, но карточка доступна.</p>"
        if data.get("history_available") is False else ""
    )
    state = GUEST_STATES.get(guest.get("state"), "Неизвестно")
    if data.get("pve_available") is False:
        state += " — PVE недоступен; состояние не проверено"
    fields = (
        ("Имя", guest.get("name") or "—"),
        ("Роль", guest.get("role") or "—"),
        ("Тип", "VM" if guest.get("type") == "vm" else
         "LXC" if guest.get("type") == "lxc" else "—"),
        ("Адрес управления", guest.get("address") or "—"),
        ("Состояние PVE", state),
    )
    detail = "".join(
        '<dt>' + html.escape(label) + '</dt><dd>'
        + html.escape(str(value)) + '</dd>' for label, value in fields
    )
    return _layout(
        '<p><a href="/guests">← Все гости</a></p>'
        '<h2>Гость ' + str(vmid) + '</h2>'
        '<dl class="guest-meta">' + detail + '</dl>'
        '<h3>Разрешённые действия</h3>'
        '<div class="guest-actions">' + "".join(commands) + '</div>'
        + ('<p>Нет разрешённых действий для этого гостя.</p>'
           if not commands else '')
        + ('<p>Самообновление 910 выполняется через PVE CLI.</p>'
           if vmid == 910 else '')
        + '<h3>Последние задания</h3>' + notice
        + '<table><tr><th>ID</th><th>Действие</th>'
        '<th>Состояние</th><th>Создано UTC</th></tr>'
        + ("".join(history) or '<tr><td colspan="4">Истории пока нет</td></tr>')
        + '</table><p><a href="/guests?vmid=' + str(vmid)
        + '">Обновить карточку</a></p>'
    )


def _guest_confirmation(vmid: int, kind: str, csrf: str) -> bytes:
    if kind not in {"sync", "repair", "deploy"}:
        raise ValueError("Для этого действия подтверждение не требуется")
    data = _guest_catalog("guest-detail", vmid)
    guest = data["guest"]
    if kind not in guest.get("actions", []):
        raise ValueError("Операция запрещена для выбранного гостя")
    token = html.escape(csrf, quote=True)
    return _layout(
        '<p><a href="/guests?vmid=' + str(vmid) + '">← Отмена</a></p>'
        '<h2>Подтвердите действие</h2>'
        '<p>Гость: ' + str(vmid) + ' — <strong>'
        + html.escape(str(guest.get("name", ""))) + '</strong></p>'
        '<p>Операция: <strong>' + html.escape(GUEST_ACTION_LABELS[kind])
        + '</strong>. Команда изменит состояние инфраструктуры.</p>'
        '<form method="POST" action="/action">'
        f'<input type="hidden" name="csrf" value="{token}">'
        f'<input type="hidden" name="vmid" value="{vmid}">'
        f'<input type="hidden" name="kind" value="{kind}">'
        '<button type="submit">Да, выполнить</button></form>'
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
    if not isinstance(data, dict) or data.get("accepted") is not True:
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
            '<p><a href="/">← Главная</a></p><h2>Хранилища и резервирование</h2>'
            '<p>Системный посредник или описание хранилищ временно недоступны.</p>'
            '<p><a href="/infrastructure">Повторить проверку</a></p>'
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
            "<tr><td>" + e(item.get("id")) + "</td><td>"
            + e(item.get("mountpoint")) + "</td><td>"
            + ("Объявлен и подтверждён" if item.get("configured") is True
               else "Ожидает настройки") + "</td><td>"
            + e(pve_state) + "</td></tr>"
        )
    backup_rows = []
    for key, label in (
        ("core", "PVE и состояние 910"),
        ("full", "PVE / 910 / VM 100"),
        ("vm100", "VM 100 (отдельная запись)"),
    ):
        record = records.get(key, {})
        backup_rows.append(
            "<tr><td>" + e(label) + "</td><td>"
            + e(BACKUP_STATE.get(record.get("state"), "Не проверено")) + "</td><td>"
            + e(record.get("created_utc")) + "</td><td>"
            + e(", ".join(record.get("destinations", [])) or "—") + "</td><td>"
            + e("Выборочная проверка данных restic: 1%" if record.get("check")
                == "restic_subset_1_percent" else
                "Проверено при создании" if record.get("check") == "backup_checked"
                else "Не подтверждено") + "</td></tr>"
        )

    timer_state = {
        "active": "Работает", "inactive": "Выключен",
        "failed": "Ошибка", "unknown": "Не удалось проверить",
    }.get(timer.get("state"), "Не удалось проверить")
    service_state = {
        "active": "Выполняется", "activating": "Запускается",
        "inactive": "Не выполняется", "failed": "Ошибка",
        "unknown": "Не удалось проверить",
    }.get(timer.get("service"), "Не удалось проверить")
    restored = backup.get("restore_tested") is True
    summary = BACKUP_OVERALL.get(backup.get("overall"), "Неизвестное состояние копий")
    token = html.escape(csrf, quote=True)
    start_link = (
        '<p><a href="/infrastructure/confirm">Запустить копирование вручную</a></p>'
        if configured else '<p>Запуск копирования недоступен: нет настроенных носителей.</p>'
    )
    return _layout(
        '<p><a href="/">← Главная</a> · <a href="/guests">Гости</a> · '
        '<a href="/storage">Подготовка SSD</a></p>'
        '<h2>Хранилища и резервирование</h2>'
        '<p><strong>' + e(summary) + '</strong></p>'
        + ('<p>' + e(notice) + '</p>' if notice else '')
        + '<p>Проверенные ранее записи не подтверждают, что SSD/NAS сейчас подключены '
        'или что восстановление испытано. Даже активное PVE storage не '
        'является проверкой идентичности физического носителя.</p>'
        '<h3>Общие тома PVE — из storage.yaml</h3>'
        '<table><tr><th>Том</th><th>Тип</th><th>Размер</th><th>Подключение</th><th>Проверка</th></tr>'
        + volume_table(volumes, include_size=True) + '</table>'
        '<h3>Тома NAS — из storage.yaml</h3>'
        '<table><tr><th>Том</th><th>Тип</th><th>Подключение</th><th>Проверка</th></tr>'
        + volume_table(nas) + '</table>'
        '<h3>Места для резервных копий — из backup.yaml</h3>'
        '<table><tr><th>Назначение</th><th>Путь</th><th>Настройка</th><th>Носитель</th></tr>'
        + "".join(dest_rows) + '</table>'
        '<p>SSD: ' + e(data.get("ssd", {}).get("state")) + '. '
        'Физические подключения необходимо независимо проверить перед восстановлением.</p>'
        '<h3>Последние сохранённые результаты</h3>'
        '<table><tr><th>Что</th><th>Состояние</th><th>Время UTC</th><th>Носители</th><th>Контроль</th></tr>'
        + "".join(backup_rows) + '</table>'
        '<p><strong>Восстановление: '
        + ("Отмечено как испытанное" if restored else "Не подтверждено")
        + '</strong>. Проверка 1% данных restic не является испытанием восстановления.</p>'
        '<h3>Расписание</h3>'
        '<p>Из backup.yaml: ' + e(backup.get("schedule_declared")) + ' (часы сервера). '
        'Таймер systemd: ' + e(timer_state) + '. '
        'Служба: ' + e(service_state) + '.</p>'
        '<p>Следующий запуск: ' + e(timer.get("next")) + '. '
        'Предыдущий запуск: ' + e(timer.get("last"))
        + '. Последний результат systemd: ' + e(timer.get("last_result"))
        + '.</p>'
        + start_link
        + '<p><a href="/infrastructure">Обновить состояние</a></p>'
    )


def _backup_confirm_page(csrf: str) -> bytes:
    token = html.escape(csrf, quote=True)
    return _layout(
        '<p><a href="/infrastructure">← Отмена</a></p>'
        '<h2>Запустить резервирование</h2>'
        '<p>Штатная служба infra-manager-backup.service проверит носители, '
        'выполнит зашифрованную запись и проверку данных restic. '
        'Задание может выполняться длительное время. '
        'Оно не запускает восстановление и не форматирует диски.</p>'
        '<p>Если доступен только один проверенный носитель, будет создана '
        'одна копия с предупреждением о второй.</p>'
        '<form method="POST" action="/infrastructure/run">'
        f'<input type="hidden" name="csrf" value="{token}">'
        '<button type="submit">Да, начать резервирование</button></form>'
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
    content = '<p><a href="/">← Задания</a></p><h2>SSD быстрых копий</h2>'
    if notice:
        content += '<p>' + html.escape(notice) + '</p>'
    try:
        record = _send_storage("storage-plan").get("storage", {})
    except (ValueError, OSError, TimeoutError):
        return _layout(content + "<p>Сейчас не удалось проверить SSD. Диск не изменён.</p>")
    if not isinstance(record, dict):
        return _layout(content + "<p>Неизвестное состояние SSD.</p>")
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
            "<p><strong>ВНИМАНИЕ: создание GPT/ext4 уничтожит прежние данные "
            "выбранного устройства. Перед операцией параметры будут проверены снова.</strong></p>"
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
            '<button type="submit">Да, подготовить SSD</button>'
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
    return _layout(content, auto_refresh=state == "running")


def _layout(inner: str, *, auto_refresh: bool = False) -> bytes:
    return ("""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
""" + ('<meta http-equiv="refresh" content="5;url=/storage">' if auto_refresh else '') + """
<title>infra-manager</title><style>
:root{color-scheme:dark}body{font-family:system-ui;background:#0c1525;
color:#eaf0f7;max-width:1050px;margin:3rem auto;padding:0 1rem}
main{background:#142237;padding:1.5rem;border-radius:12px}
table{width:100%;border-collapse:collapse}td,th{padding:.55rem;
border-bottom:1px solid #345;text-align:left}input,select,button{
padding:.55rem;margin:.25rem;border-radius:6px}
a{color:#9bded8}code{color:#96d1f1}
.versions{margin-top:2rem;border-top:1px solid #345;padding-top:1rem;
font-size:.85rem;color:#bdcbd9}.versions dl{display:grid;grid-template-columns:
minmax(180px,1fr) 1fr;gap:.4rem .7rem}.versions dt{color:#a9bcce}
.versions dd{margin:0}.versions p{color:#a9bcce}
.guest-meta{display:grid;grid-template-columns:minmax(150px,1fr) 2fr;
gap:.4rem 1rem}.guest-meta dt{color:#a9bcce}.guest-meta dd{margin:0}
.guest-actions{display:flex;gap:.7rem;flex-wrap:wrap}
.guest-actions form{margin:0}
.diagnostic-log{background:#061222;padding:1rem;border-radius:8px;
overflow:auto;max-height:55vh;white-space:pre-wrap;overflow-wrap:anywhere;
min-height:8rem;border:1px solid #345}
#op-diagnostic{color:#e7cf9e}
</style></head><body><h1>Управление инфраструктурой</h1>
<main>""" + inner + "</main></body></html>").encode()


def _dashboard(csrf: str, notice: str = "") -> bytes:
    try:
        records = _operation_history()
    except (OSError, ValueError, TimeoutError):
        records = []
        notice = (notice + " " if notice else "") + (
            "История операций временно недоступна; проверьте системный посредник."
        )
    rows = []
    for record in records:
        operation_id = str(record.get("id", ""))
        if not REQ_ID.fullmatch(operation_id):
            continue
        values = (operation_id[:8], str(record.get("target", "")),
                  str(record.get("kind", "")),
                  PHASES.get(record.get("phase"), "Неизвестно"),
                  str(record.get("created_at", "")))
        cells = ["<td><a href=\"/operations?id=" + operation_id + "\">"
                 + html.escape(values[0]) + "</a></td>"]
        cells.extend("<td>" + html.escape(value) + "</td>" for value in values[1:])
        rows.append("<tr>" + "".join(cells) + "</tr>")
    table = "".join(rows) or '<tr><td colspan="5">Заданий пока нет</td></tr>'
    token = html.escape(csrf, quote=True)
    form = (
        f'<p>{html.escape(notice)}</p>'
        + '<form method="POST" action="/action">'
        f'<input type="hidden" name="csrf" value="{token}">'
        '<label>Гость (VMID) <input name="vmid" type="number" min="1" required></label>'
        '<label>Действие <select name="kind">'
        '<option value="status">Проверить</option>'
        '<option value="sync">Синхронизировать</option>'
        '<option value="repair">Исправить</option>'
        '<option value="deploy">Развернуть</option>'
        '</select></label><button type="submit">Выполнить</button></form>'
        '<p><a href="/guests">Управление гостями</a> · '
        '<a href="/infrastructure">Хранилища и бэкапы</a> · '
        '<a href="/storage">Подготовить резервный SSD</a></p>'
        '<p>Изменяйте только известные гости. Система повторно проверяет права '
        'и записывает результат задания независимо от браузера.</p>'
        '<table><tr><th>ID</th><th>VMID</th><th>Действие</th>'
        '<th>Состояние</th><th>Время UTC</th></tr>' + table + '</table>'
        f'<form method="POST" action="/logout"><input type="hidden" '
        f'name="csrf" value="{token}"><button>Выйти</button></form>'
    )
    return _layout(form)


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
        '<button>Войти</button></form>'
        + _public_versions()
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
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; connect-src 'self'; form-action 'self'; base-uri 'none'",
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
        session = self._authorized()
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
                    self._reply(HTTPStatus.OK, _guests_page())
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
                self._reply(HTTPStatus.OK, _operation_page(operation_id))
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
                token, session = _new_session()
            except ValueError:
                self._reply(HTTPStatus.SERVICE_UNAVAILABLE, b"session limit\n")
                return
            self._reply(
                HTTPStatus.OK, _dashboard(session["csrf"]),
                cookie=f"{COOKIE}={token}; HttpOnly; Secure; SameSite=Strict; Path=/",
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
            with guard:
                sessions.pop(token, None)
            self._reply(
                HTTPStatus.OK, _login(),
                cookie=f"{COOKIE}=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/",
            )
            return
        if path == "/infrastructure/run":
            if set(form) != {"csrf"}:
                self._reply(HTTPStatus.BAD_REQUEST, b"invalid backup form\n")
                return
            try:
                _backup_request("backup-run")
            except (OSError, ValueError, TimeoutError):
                self._reply(HTTPStatus.BAD_REQUEST, _backup_overview_page(
                    session["csrf"],
                    "Резервирование не принято. Проверьте настройки и состояние службы.",
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
