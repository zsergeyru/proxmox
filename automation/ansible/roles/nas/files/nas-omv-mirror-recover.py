#!/usr/bin/env python3
"""Временное восстановление официальных зеркал OMV 8 при HTTP 503.

Запускается только во время deploy NAS. Без изменения ключей APT,
системных пакетов, SSH, накопителей или источников Debian/Docker.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

SOURCE = Path("/etc/apt/sources.list.d/openmediavault.list")
MIRRORS = Path("/etc/apt/mirrors/openmediavault.txt")
STATE = Path("/var/lib/infra-manager/omv-apt-mirror-recovery.json")
KEYRING = Path("/usr/share/keyrings/openmediavault-archive-keyring.gpg")
MIRROR_SOURCE = (
    "deb [signed-by=/usr/share/keyrings/openmediavault-archive-keyring.gpg] "
    "mirror+file:/etc/apt/mirrors/openmediavault.txt synchrony main"
)
MIRROR_CONTENT = (
    "# Official OMV 8.5.12 APT mirror priorities\n"
    "https://packages.openmediavault.org/public/\tpriority:1\n"
    "https://packages.openmediavault.io/\tpriority:2\n"
    "https://openmediavault.github.io/packages/\tpriority:3\n"
    "https://downloads.sourceforge.net/project/openmediavault/packages/\tpriority:4\n"
)
KNOWN_SOURCE = re.compile(
    r"^deb \[signed-by=/usr/share/keyrings/openmediavault-archive-keyring\.gpg\] "
    r"(?:https?://packages\.openmediavault\.org/public/?|"
    r"https?://packages\.openmediavault\.io/?) synchrony main$"
)


def _read_safe(path: Path) -> str | None:
    if path.is_symlink():
        raise ValueError("Обнаружен недопустимый символический путь APT")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Источник APT не является обычным файлом")
    return path.read_text(encoding="utf-8")


def _atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if path.is_symlink():
        raise ValueError("Недопустим символический путь назначения APT")
    fd, temporary = tempfile.mkstemp(prefix=".infra-omv-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = ""
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)


def _new_sources(source: str) -> str:
    lines = source.splitlines()
    positions: list[int] = []
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not KNOWN_SOURCE.fullmatch(stripped):
            raise ValueError(
                "Файл репозитория OMV содержит нестандартную активную запись; "
                "автоматическая замена запрещена"
            )
        positions.append(index)
    if not positions or len(positions) > 2:
        raise ValueError("Не найден однозначный официальный источник APT OMV")
    lines[positions[0]] = MIRROR_SOURCE
    for index in reversed(positions[1:]):
        lines.pop(index)
    return "\n".join(lines) + "\n"


def _save_state(original_source: str, original_mirrors: str | None) -> None:
    if _read_safe(STATE) is not None:
        raise ValueError("Неожиданно существует прежняя запись восстановления APT")
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic(
        STATE,
        json.dumps(
            {"source": original_source, "mirrors": original_mirrors},
            ensure_ascii=False, sort_keys=True,
        ) + "\n",
        0o600,
    )


def _record() -> dict[str, str | None]:
    data = _read_safe(STATE)
    if data is None:
        raise ValueError("Нет сохранённого прежнего источника APT")
    loaded = json.loads(data)
    if not isinstance(loaded, dict):
        raise ValueError("Повреждена запись восстановления APT")
    source, mirrors = loaded.get("source"), loaded.get("mirrors")
    if (
        not isinstance(source, str) or not source
        or (mirrors is not None and not isinstance(mirrors, str))
    ):
        raise ValueError("Некорректная запись восстановления APT")
    _new_sources(source)  # прежняя конфигурация всё ещё должна быть официальной
    return {"source": source, "mirrors": mirrors}


def _apply() -> None:
    if not KEYRING.is_file() or KEYRING.is_symlink():
        raise ValueError("Не найден штатный подписывающий ключ OMV")
    old_source = _read_safe(SOURCE)
    old_mirrors = _read_safe(MIRRORS)
    if old_source is None:
        raise ValueError("Не найден штатный список репозиториев OMV")
    current_state = _read_safe(STATE)
    if current_state is not None:
        saved = _record()
        if old_source == _new_sources(saved["source"]) and old_mirrors == MIRROR_CONTENT:
            print("[ОК] Штатный список официальных зеркал OMV уже установлен")
            return
        raise ValueError("Источники APT изменились после предыдущей попытки")
    replacement = _new_sources(old_source)
    _save_state(old_source, old_mirrors)
    try:
        _atomic(MIRRORS, MIRROR_CONTENT, 0o644)
        _atomic(SOURCE, replacement, 0o644)
    except Exception:
        _atomic(SOURCE, old_source, 0o644)
        if old_mirrors is None:
            MIRRORS.unlink(missing_ok=True)
        else:
            _atomic(MIRRORS, old_mirrors, 0o644)
        STATE.unlink(missing_ok=True)
        raise
    print("[ОК] Подключён официальный список зеркал OMV; проверка Signed-By сохранена")


def _rollback() -> None:
    saved = _record()
    if _read_safe(SOURCE) != _new_sources(saved["source"]):
        raise ValueError("Источники APT изменились: откат запрещён")
    if _read_safe(MIRRORS) != MIRROR_CONTENT:
        raise ValueError("Список зеркал изменился: откат запрещён")
    _atomic(SOURCE, saved["source"], 0o644)
    if saved["mirrors"] is None:
        MIRRORS.unlink()
    else:
        _atomic(MIRRORS, saved["mirrors"], 0o644)
    STATE.unlink()
    print("[ОК] Прежняя конфигурация репозитория OMV восстановлена")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("apply", "rollback"):
            raise ValueError("Ожидается один аргумент apply или rollback")
        if sys.argv[1] == "apply":
            _apply()
        else:
            _rollback()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        # Не выводить произвольные данные файлов APT и сетевые адреса с userinfo.
        print("[ОШИБКА] Безопасное восстановление APT остановлено: "
              "проверьте существующий источник OMV, ключ подписи и запись отката",
              file=sys.stderr)
        raise SystemExit(1) from None
