#!/usr/bin/env python3
"""Показать начальный пароль 140 ровно один раз из доверенного терминала PVE.

Команду запускают вручную: pct exec 140 -- python3 /usr/local/lib/web-gateway/show-initial-password.py
Не использовать через Semaphore: его журнал сохранит стандартный вывод.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys


def main() -> int:
    """Прочитать пароль только root и удалить файл до вывода в терминал."""
    if os.geteuid() != 0:
        raise PermissionError("Требуется root в госте 140")
    path = Path("/run/web-gateway/initial-admin-password")
    if path.is_symlink() or not path.is_file():
        print("Первоначальный пароль уже получен либо гость перезагружен.")
        return 1
    password = path.read_text(encoding="utf-8").strip()
    path.unlink()
    if not password:
        raise RuntimeError("Пустой первоначальный пароль")
    print("Администратор: admin")
    print("Первоначальный пароль:", password)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(f"140: не удалось прочитать пароль: {error}", file=sys.stderr)
        sys.exit(1)
