"""Однократный запрос существующего пароля NAS в терминале PVE."""

from __future__ import annotations

import getpass
import json
import sys


BOOTSTRAP_REQUIRED = 20


def initial_nas_access(name: str, forward, error_type: type[Exception]) -> int:
    """Запросить разовый пароль только при отсутствии отметки первого доверия."""

    state = forward(["bootstrap-state", name])
    if state == 0:
        return 0
    if state != BOOTSTRAP_REQUIRED:
        return state
    if not sys.stdin.isatty():
        raise error_type(
            "Для первого подключения NAS откройте терминал PVE"
        )
    print("Первый доступ: существующий пользователь NAS с правами sudo.")
    username = input("Пользователь NAS: ").strip()
    if not username or len(username) > 64 or not all(
        char.isascii() and (char.isalnum() or char in "._-") for char in username
    ):
        raise error_type("Некорректное имя пользователя NAS")
    password = getpass.getpass("Пароль NAS (ввод не отображается): ")
    if not password or "\n" in password or "\r" in password:
        raise error_type("Пароль NAS не может быть пустым или многострочным")
    payload = json.dumps(
        {"username": username, "password": password}, ensure_ascii=False
    ) + "\n"
    del password
    return forward(["bootstrap", name], input_text=payload)
