#!/usr/bin/env python3
"""Подготовить постоянные параметры дополнений Immich без перезаписи настроек."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from pathlib import Path


def ensure_directory(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Каталог является ссылкой: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    if not path.is_dir():
        raise RuntimeError(f"Ожидался каталог: {path}")


def ensure_file(path: Path, content: str) -> bool:
    """Создать файл один раз; пользовательские изменения не перезаписывать."""
    if path.is_symlink():
        raise RuntimeError(f"Файл является ссылкой: {path}")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if not path.is_file():
            raise RuntimeError(f"Ожидался обычный файл: {path}") from None
        # Проверять права существующего файла, но не изменять его содержимое.
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"Слишком широкие права на файл: {path}")
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    return True


def prepare(state: Path) -> list[str]:
    ensure_directory(state)
    ensure_directory(state / "secrets")
    for subdir in ("memories", "memories/state", "power-tools", "power-tools/data"):
        ensure_directory(state / "addons" / subdir)

    # Power Tools работает в контейнере от UID/GID 1001 и хранит SQLite.
    power_data = state / "addons/power-tools/data"
    if (power_data.stat().st_uid, power_data.stat().st_gid) != (1001, 1001):
        os.chown(power_data, 1001, 1001)

    entries = {
        state / "secrets/immich-memories.env": (
            "IMMICH_URL=http://immich-server:2283\n"
            "NTFY_URL=\n"
            "IMMICH_EXTERNAL_URL=\n"
            "NTFY_EXTERNAL_URL=\n"
            "DASHBOARD_USER=admin\n"
            f"DASHBOARD_TOKEN={secrets.token_urlsafe(32)}\n"
            "DASHBOARD_PORT=5000\n"
            "TZ=Europe/Moscow\n"
        ),
        state / "addons/memories/config.yaml": "",
        state / "secrets/immich-power-tools.env": (
            "IMMICH_URL=http://immich-server:2283\n"
            "IMMICH_API_KEY=\n"
            "EXTERNAL_IMMICH_URL=\n"
            "DB_HOST=database\n"
            "DB_PORT=5432\n"
            "SECURE_COOKIE=false\n"
            "APP_DB_PATH=/app/data/app.db\n"
            f"JWT_SECRET={secrets.token_urlsafe(48)}\n"
        ),
        state / "secrets/immich-analyze.env": (
            "IMMICH_API_URL=http://immich-server:2283\n"
            "IMMICH_API_KEY=\n"
            "IMMICH_ANALYZE_INTERFACE=llamacpp\n"
            "IMMICH_ANALYZE_HOSTS=\n"
            "IMMICH_ANALYZE_MODEL_NAME=\n"
            "IMMICH_ANALYZE_MODE=monitor\n"
            "IMMICH_ANALYZE_OVERWRITE_POLICY=none\n"
            "IMMICH_ANALYZE_MAX_CONCURRENT=1\n"
            "IMMICH_ANALYZE_MAX_IMAGE_SIZE=1024\n"
            "IMMICH_ANALYZE_THUMBNAIL_SIZE=preview\n"
            "IMMICH_ANALYZE_LANG=ru\n"
            "IMMICH_ANALYZE_PREFLIGHT_MODEL_CHECK=true\n"
        ),
    }
    created = [str(path.relative_to(state)) for path, value in entries.items()
               if ensure_file(path, value)]
    return created


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    args = parser.parse_args()
    created = prepare(args.state)
    print(f"Дополнения Immich: подготовлено файлов {len(created)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
