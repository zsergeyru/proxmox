"""Первый SSH-вход и подтверждение ключа физического сервера на PVE."""

from __future__ import annotations

import getpass
import json
import re
import sys


BOOTSTRAP_REQUIRED = 20


def initial_host_access(name: str, forward, error_type: type[Exception]) -> int:
    """Первый вход без сверки ключа, повторный — только после ответа «да»."""

    state = forward(["bootstrap-state", name])
    if state == 0:
        result = forward(["ssh-trust-plan", name], capture_output=True)
        if result.returncode:
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            return result.returncode
        prefix = "PHYSICAL_SSH_TRUST_JSON="
        records = [line[len(prefix):] for line in result.stdout.splitlines()
                   if line.startswith(prefix)]
        if len(records) != 1:
            raise error_type("Не удалось получить SSH-ключ физического сервера")
        try:
            plan = json.loads(records[0])
        except (json.JSONDecodeError, TypeError) as exc:
            raise error_type("Некорректные сведения об SSH-ключе сервера") from exc
        if not isinstance(plan, dict) or plan.get("name") != name:
            raise error_type("SSH-ключ получен для другого сервера")
        fingerprint = plan.get("current")
        if not isinstance(fingerprint, str) or not re.fullmatch(
            r"SHA256:[A-Za-z0-9+/]{43}", fingerprint
        ):
            raise error_type("Некорректный отпечаток SSH-ключа сервера")
        if not sys.stdin.isatty():
            raise error_type(
                f"Для повторного deploy {name} требуется ответ «да» в терминале PVE"
            )
        print(f"Повторное развёртывание сервера {name} ({plan.get('address')})")
        print(f"Текущий SSH-ключ: {fingerprint}")
        if plan.get("changed"):
            print(f"ВНИМАНИЕ: ранее сохранён другой ключ: {plan.get('previous')}")
        answer = input("Подтвердить SSH-ключ этого сервера? [да/нет]: ").strip().casefold()
        if answer != "да":
            raise error_type("SSH-ключ не подтверждён; развёртывание остановлено")
        return forward(
            ["ssh-trust-confirm", name],
            input_text=json.dumps({"fingerprint": fingerprint}) + "\n",
        )
    if state != BOOTSTRAP_REQUIRED:
        return state
    if not sys.stdin.isatty():
        raise error_type(
            "Для первого подключения физического сервера откройте терминал PVE"
        )
    print(f"Первый доступ к {name}: существующий пользователь Linux с правами sudo.")
    username = input("Пользователь сервера: ").strip()
    if not username or len(username) > 64 or not all(
        char.isascii() and (char.isalnum() or char in "._-") for char in username
    ):
        raise error_type("Некорректное имя пользователя сервера")
    password = getpass.getpass("Пароль сервера (ввод не отображается): ")
    if not password or "\n" in password or "\r" in password:
        raise error_type("Пароль сервера не может быть пустым или многострочным")
    payload = json.dumps(
        {"username": username, "password": password}, ensure_ascii=False
    ) + "\n"
    del password
    return forward(["bootstrap", name], input_text=payload)

# Совместимость с тестами и ранее установленным именем помощника.
initial_nas_access = initial_host_access
