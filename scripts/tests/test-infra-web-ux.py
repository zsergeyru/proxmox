#!/usr/bin/env python3
"""UX infra-web: реальные HTML-страницы и безопасный локальный формат времени."""

from __future__ import annotations

import importlib.util
from html.parser import HTMLParser
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web"
sys.path.insert(0, str(WEB))
spec = importlib.util.spec_from_file_location("infra_web_ui_ux", WEB / "admin.py")
assert spec and spec.loader
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class StructureCheck(HTMLParser):
    """Не допускаем незакрытые блоки, вложенные ссылки/формы и лишний main."""

    VOID = frozenset({"meta", "link", "input", "br", "hr", "img", "source", "wbr"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.main_count = 0
        self.forms = 0

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.main_count += 1
        if tag in {"a", "form"} and tag in self.stack:
            raise AssertionError(f"Вложенный {tag} в HTML")
        if tag == "form":
            self.forms += 1
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack[-1] != tag:
            raise AssertionError(
                f"Неверная структура HTML: закрывается {tag}, ожидался {self.stack[-1] if self.stack else 'пусто'}"
            )
        self.stack.pop()


def check_markup(page: str, *, authenticated: bool):
    parser = StructureCheck()
    parser.feed(page)
    parser.close()
    assert not parser.stack, ("Не закрыты HTML-теги", parser.stack)
    assert parser.main_count == (1 if authenticated else 0), "Неверная рабочая область"
    if authenticated:
        assert parser.forms >= 1, "Должна быть доступна защищённая форма выхода"



def main() -> None:
    stamp = "2026-10-20T18:05:15+00:00"
    assert 'data-local-time datetime="' + stamp + '"' in admin._time_html(stamp)
    assert 'data-local-time datetime="2026-10-20T18:05:15Z"' in admin._time_html(
        "2026-10-20T18:05:15Z"
    )
    assert "20.10.2026" in admin._time_html("2026-10-20")
    assert "data-local-time" not in admin._time_html("Tue 2026-10-20 21:30:00 MSK")
    assert "20.10.2026 · 21:30 MSK" in admin._systemd_time_html(
        "Tue 2026-10-20 21:30:00 MSK"
    )
    assert "data-local-time" not in admin._systemd_time_html(
        "Tue 2026-10-20 21:30:00 MSK"
    )
    assert "&lt;script&gt;" in admin._systemd_time_html("<script>")
    assert "&lt;script&gt;" in admin._time_html("<script>")
    assert admin._time_html(None) == "—"

    task = "a" * 32
    rows = [
        {"id": task, "kind": "deploy", "target": 109, "phase": "running",
         "created_at": stamp},
        {"id": "b" * 32, "kind": "status", "target": 910, "phase": "failed",
         "created_at": "2026-10-20T18:00:00Z"},
        {"id": "c" * 32, "kind": "status", "target": 999, "phase": "completed",
         "created_at": stamp},
    ]
    with patch.object(admin, "_operation_history", return_value=rows):
        home = admin._dashboard("csrf-token").decode()
    for label in ("Обзор", "Гости", "Хранилища и бэкапы", "Последние задания",
                  "Выполняются", "Нужны внимание", "Быстрый переход"):
        assert label in home, label
    assert 'data-search-for="operations"' in home
    assert 'data-filter-list="operations"' in home
    assert 'data-state="running"' in home
    assert 'href="/operations?id=' + task + '"' in home
    assert 'name="csrf" value="csrf-token"' in home
    assert 'datetime="' + stamp + '"' in home
    assert '<script src="/ui.js" defer>' in home
    assert '<link rel="stylesheet" href="/ui.css">' in home
    # Унифицированное меню есть на всех рабочих страницах, не только на главной.
    guest_data = {
        "accepted": True, "pve_available": True,
        "guests": [
            {"vmid": 109, "name": "<script>alert(1)</script>",
             "role": "gateway", "type": "lxc", "state": "running",
             "address": "192.168.9.109"},
            {"vmid": 910, "name": "infra-manager",
             "role": "infra-manager", "type": "lxc", "state": "stopped",
             "address": "192.168.9.10"},
        ],
    }
    with patch.object(admin, "_guest_catalog", return_value=guest_data):
        listing = admin._guests_page("csrf-token").decode()
    assert 'class="guest-grid"' in listing or 'class="guest-grid" data-filter-list=' in listing
    assert 'data-search-for="guests"' in listing
    assert 'data-state-for="guests"' in listing
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in listing
    assert "<script>alert(1)</script>" not in listing
    assert 'href="/guests?vmid=109"' in listing
    assert 'aria-current="page"' in listing

    guest_detail = {
        "accepted": True, "pve_available": True,
        "history_available": True,
        "guest": {**guest_data["guests"][0], "actions": ["status", "sync", "repair"]},
        "operations": [{"id": task, "kind": "status",
                        "phase": "completed", "created_at": stamp}],
    }
    with patch.object(admin, "_guest_catalog", return_value=guest_detail):
        card = admin._guest_page(109, "csrf-token").decode()
        confirmation = admin._guest_confirmation(109, "repair", "csrf-token").decode()
    assert 'name="kind" value="status"' in card
    assert 'href="/operations?id=' + task + '"' in card
    assert 'datetime="' + stamp + '"' in card
    assert 'action="/guests/confirm"' in card
    assert "Да, выполнить" in confirmation
    assert 'name="csrf"' in confirmation

    detail = {
        "accepted": True,
        "operation": {"id": task, "kind": "deploy", "target": 109,
                      "phase": "running", "created_at": stamp,
                      "started_at": stamp, "finished_at": None, "exit_code": None},
        "events": [{"at": stamp, "phase": "running"}],
        "logs": ["[ИНФО] <script>alert(1)</script>"],
        "diagnostic": "Выполнение ещё не завершено.",
    }
    with patch.object(admin, "_operation_detail", return_value=detail):
        operation = admin._operation_page(task, "csrf-token").decode()
    assert "<h2>Диагностика задания</h2>" in operation
    assert 'data-operation-id="' + task + '"' in operation
    assert 'id="op-log-search"' in operation
    assert 'data-duration-start="' + stamp + '"' in operation
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in operation
    assert "<script>alert(1)</script>" not in operation
    assert 'src="/diagnostics.js"' in operation
    assert 'id="timezone-select"' in operation
    assert 'name="csrf" value="csrf-token"' in operation

    for markup in (home, listing, card, confirmation, operation):
        check_markup(markup, authenticated=True)
    login = admin._login().decode()
    assert 'action="/login"' in login
    assert 'class="login-panel"' in login
    check_markup(login, authenticated=False)
    assert "Версии компонентов" in login
    assert 'id="timezone-select"' not in login
    css = (WEB / "ui.css").read_text(encoding="utf-8")
    js = (WEB / "ui.js").read_text(encoding="utf-8")
    assert "[hidden]{display:none!important}" in css
    assert "@media(max-width:760px)" in css
    assert "Europe/Moscow" in js and "Intl.DateTimeFormat" in js
    assert 'window.localStorage' in js
    assert "data-local-time" in js
    assert "data-filter-list" in js
    assert "fetch(" not in js, "UI script must not access APIs by itself"
    print("[ОК] UX: карточки, доступность, время браузера, CSRF и HTML-экранирование")


if __name__ == "__main__":
    main()
