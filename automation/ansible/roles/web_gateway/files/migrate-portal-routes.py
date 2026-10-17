#!/usr/bin/env python3
"""Однократно сменить только внутренние назначения Homarr на Caddy.

Остальные пользовательские настройки, включение маршрутов и данные других
гостей не изменяются. Подключение mp0 обязательно.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import tempfile
import yaml

OLD = ("homarr", "http://homarr:7575")
NEW = ("portal", "http://caddy:8080")
DOMAINS = frozenset({"home.zsergey.ru", "check.zsergey.ru"})


def migrate(state: Path) -> bool:
    if state.is_symlink() or not os.path.ismount(state):
        raise RuntimeError("Постоянный том не подключён")
    path = state / "management/routes.yaml"
    lock_path = state / "management/.routes.lock"
    if path.is_symlink() or lock_path.is_symlink() or not path.is_file():
        raise RuntimeError("Сохранённые маршруты отсутствуют или подменены")
    with lock_path.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        rows = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise RuntimeError("Повреждён список маршрутов")
        changed = False
        seen = set()
        for item in rows:
            if not isinstance(item, dict) or not isinstance(item.get("domain"), str):
                raise RuntimeError("Неправильный маршрут")
            domain = item["domain"]
            if domain in seen:
                raise RuntimeError("Повторяющийся домен")
            seen.add(domain)
            if domain not in DOMAINS:
                continue
            old = (item.get("service"), item.get("upstream"))
            if old == OLD:
                item["service"], item["upstream"] = NEW
                changed = True
            elif old != NEW:
                raise RuntimeError("Неожиданное назначение домена; миграция остановлена")
        if not changed:
            return False
        fd, tmp = tempfile.mkstemp(prefix=".routes-migrate-", dir=path.parent)
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yaml.safe_dump(rows, stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("/var/lib/web-gateway"))
    args = parser.parse_args()
    migrate(args.state)
    print("140: назначения домашней страницы согласованы; данные Homarr сохранены")
