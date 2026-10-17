#!/usr/bin/env python3
"""Краткая проверка причин ошибок домашней страницы без раскрытия журнала.

Никогда не выводит содержимое запросов, пользователей или секреты из логов.
"""
from __future__ import annotations

import subprocess
import sys


CATEGORIES = {
    "подключение": ("econnrefused", "etimedout", "fetch failed", "network error",
                    "getaddrinfo", "dns resolution", "connection refused"),
    "доступ": ("unauthorized", "forbidden", "invalid credentials", "401 unauthorized",
               "403 forbidden"),
    "виджеты": ("custom-widget", "custom widget", "widget error", "integration error"),
}


def summarize(logs: str) -> list[str]:
    """Считать категории, но не показывать строки, содержащие личные сведения."""
    counts: dict[str, int] = {}
    for line in logs.splitlines():
        lowered = line.lower()
        if not any(term in lowered for term in ("error", "fail", "exception", "ошибка", "econnrefused")):
            continue
        category = next(
            (name for name, patterns in CATEGORIES.items()
             if any(pattern in lowered for pattern in patterns)), "прочее"
        )
        counts[category] = counts.get(category, 0) + 1
    if not counts:
        return []
    return [f"[ПРЕДУПРЕЖДЕНИЕ] Homarr: в последних записях найдены ошибки ({name}: {count})"
            for name, count in sorted(counts.items())]


def main() -> int:
    result = subprocess.run(
        ["docker", "logs", "--since", "15m", "--tail", "300", "web-gateway-homarr"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if result.returncode != 0:
        print("[ПРЕДУПРЕЖДЕНИЕ] Homarr: не удалось прочитать журнал (без изменения настроек)")
        return 0
    for message in summarize(result.stdout + "\n" + result.stderr):
        print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
