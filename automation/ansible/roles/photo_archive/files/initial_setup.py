#!/usr/bin/env python3
"""Однократная настройка Immich; повторный запуск сохраняет изменения из веб-интерфейса."""

from __future__ import annotations

import argparse
import errno
import json
import os
import pty
import re
import secrets
import select
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ApiError(RuntimeError):
    """Ошибка запроса к Immich без вывода паролей и ответов с секретами."""

    def __init__(self, method: str, path: str, code: int):
        self.code = code
        super().__init__(f"Immich {method} {path}: HTTP {code}")


def api(base: str, method: str, path: str, payload: dict | None = None, token: str = ""):
    """Выполнить запрос к API закреплённой версии Immich v3.3.1."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(base.rstrip("/") + "/api" + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read()
            return json.loads(body) if body else None
    except HTTPError as exc:
        raise ApiError(method, path, exc.code) from None
    except URLError as exc:
        raise RuntimeError(f"Immich недоступен: {exc.reason}") from None


def initial_passwords(path: Path, config: dict) -> dict:
    """Сгенерировать пароли один раз и сохранить до обращения к API."""
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    passwords = {
        entry["email"]: secrets.token_urlsafe(32)
        for entry in [config["admin"], *config["users"]]
    }
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(passwords, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return passwords


def complete_marker(path: Path) -> None:
    """Зафиксировать успешный этап на постоянном томе."""
    path.write_text("1\n", encoding="utf-8")


def reset_existing_admin(email: str, password: str) -> None:
    """Один раз принять существующего администратора через штатную команду Immich.

    CLI интерактивная: отдельный псевдотерминал позволяет проверить email
    администратора ДО передачи пароля. Вывод (включая ввод) никогда не печатаем.
    """
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [
                "docker", "exec", "-it", "-e", "TERM=xterm",
                "immich_server", "immich-admin", "reset-admin-password",
            ],
            stdin=slave, stdout=slave, stderr=slave,
        )
    finally:
        os.close(slave)

    stage = "identify"
    transcript = b""
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 1)
            if ready:
                try:
                    chunk = os.read(master, 4096)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    chunk = b""
                if not chunk:
                    break
                # Весь вывод остаётся только в памяти: CLI может повторить пароль.
                transcript = (transcript + chunk)[-65536:]
                visible = re.sub(
                    r"\\x1b\\[[0-?]*[ -/]*[@-~]", "",
                    transcript.decode("utf-8", errors="replace"),
                )
                if stage == "identify" and "Please choose a new password" in visible:
                    found = re.search(r"- Email=([^\\r\\n]+)", visible)
                    if not found or found.group(1).strip().lower() != email.lower():
                        raise RuntimeError(
                            "Администратор в базе Immich не совпадает с описанием гостя; "
                            "пароль оставлен без изменений"
                        )
                    os.write(master, password.encode("utf-8") + b"\\n")
                    stage = "confirm"
                elif stage == "confirm" and "Invalidate existing sessions?" in visible:
                    os.write(master, b"y\\n")
                    stage = "finish"
            if process.poll() is not None and not ready:
                break
        else:
            raise RuntimeError("Истекло время ожидания команды Immich для администратора")

        # Команда Immich сама ловит исключения и иногда завершает работу с кодом 0:
        # проверять нужно и сообщение об успехе, и затем вход по API.
        if (
            process.wait(timeout=5) != 0
            or stage != "finish"
            or b"The admin password has been updated." not in transcript
        ):
            raise RuntimeError("Immich не подтвердил изменение пароля администратора")
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        os.close(master)


def prepare_admin(base: str, config: dict, passwords: dict) -> str:
    """Создать первого администратора либо войти после частичного запуска."""
    admin = config["admin"]
    email = admin["email"]
    try:
        api(base, "POST", "/auth/admin-sign-up",
            {"email": email, "name": admin["name"], "password": passwords[email]})
    except ApiError as exc:
        # Immich v3.3.1 отвечает HTTP 400, если администратор уже создан.
        # После частичного первого запуска пробуем войти, не создавая дубль.
        if exc.code not in (400, 403):
            raise
    try:
        login = api(base, "POST", "/auth/login", {"email": email, "password": passwords[email]})
    except ApiError as exc:
        if exc.code != 401:
            raise
        # Администратор уже существовал до первого deploy или пароль меняли
        # во время незавершённой настройки. Сброс допустим только до маркера
        # initial-setup.done; пароли остальных пользователей не затрагиваются.
        reset_existing_admin(email, passwords[email])
        login = api(base, "POST", "/auth/login", {"email": email, "password": passwords[email]})
    if not login.get("isAdmin"):
        raise RuntimeError("Указанная учётная запись не является администратором Immich")
    return login["accessToken"]


def ensure_users(base: str, config: dict, passwords: dict, token: str) -> dict[str, str]:
    """Добавить только отсутствующих пользователей, существующих не менять."""
    current = {
        user["email"].lower(): user
        for user in api(base, "GET", "/admin/users", token=token)
    }
    for user in config["users"]:
        email = user["email"]
        if email.lower() not in current:
            current[email.lower()] = api(
                base, "POST", "/admin/users",
                {
                    "email": email,
                    "name": user["name"],
                    "password": passwords[email],
                    "isAdmin": False,
                    "shouldChangePassword": True,
                    "notify": False,
                },
                token,
            )
    return {email: info["id"] for email, info in current.items()}


def ensure_library(base: str, config: dict, admin_id: str, token: str) -> None:
    """Зарегистрировать внешний архив один раз, не трогая созданные библиотеки."""
    name = config["library"]["name"]
    path = config["library"]["import_path"]
    libraries = api(base, "GET", "/libraries", token=token)
    if any(item["ownerId"] == admin_id and path in item["importPaths"] for item in libraries):
        return
    if any(item["name"] == name for item in libraries):
        raise RuntimeError(f"Библиотека «{name}» уже существует с другим путём; проверить вручную")
    api(base, "POST", "/libraries", {
        "ownerId": admin_id,
        "name": name,
        "importPaths": [path],
    }, token)


def ensure_partners(base: str, config: dict, users: dict[str, str], token: str) -> None:
    """Открыть архив администратора двум пользователям, но не их личные архивы."""
    existing = {
        item["id"] for item in api(base, "GET", "/partners?direction=shared-by", token=token)
    }
    for person in config["users"]:
        user_id = users[person["email"].lower()]
        if user_id not in existing:
            api(base, "POST", "/partners", {"sharedWithId": user_id}, token)


def ensure_settings(base: str, config: dict, token: str, marker: Path) -> None:
    """Записать начальные параметры один раз; далее управлять ими через UI."""
    if marker.exists():
        return
    settings = api(base, "GET", "/admin/config", token=token)
    settings["library"]["scan"] = {"enabled": True, "cronExpression": config["scan_cron"]}
    settings["library"]["watch"]["enabled"] = False
    settings["machineLearning"]["enabled"] = False
    settings["ffmpeg"]["transcode"] = "disabled"
    for key in ("thumbnailGeneration", "metadataExtraction", "library", "videoConversion"):
        settings["job"][key]["concurrency"] = 1
    api(base, "PUT", "/admin/config", settings, token)
    complete_marker(marker)


def initialize(base: str, config: dict, state: Path, originals: Path) -> bool:
    """Подготовить фотоархив без повторного применения после успешного запуска."""
    marker = state / "initial-setup.done"
    if marker.exists():
        return False
    if not originals.is_dir():
        raise RuntimeError(f"Нет каталога оригиналов: {originals}")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret_path = state / "secrets" / "immich-initial-users.json"
    passwords = initial_passwords(secret_path, config)
    token = prepare_admin(base, config, passwords)
    users = ensure_users(base, config, passwords, token)
    admin_email = config["admin"]["email"].lower()
    ensure_library(base, config, users[admin_email], token)
    ensure_partners(base, config, users, token)
    ensure_settings(base, config, token, state / "initial-settings.done")
    complete_marker(marker)
    return True


def main() -> int:
    """Прочитать параметры начальной настройки и сообщить результат без секретов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--originals", type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        changed = initialize(args.base_url, config, args.state, args.originals)
    except (ApiError, RuntimeError, KeyError, ValueError, OSError) as exc:
        print(f"ОШИБКА первоначальной настройки Immich: {exc}", file=sys.stderr)
        return 1
    print("Первоначальная настройка Immich: применена" if changed
          else "Первоначальная настройка Immich: уже выполнена")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
