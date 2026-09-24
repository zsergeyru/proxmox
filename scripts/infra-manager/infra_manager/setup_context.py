"""Общий контекст и вывод процесса настройки infra-manager."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .common import CommandRunner


@dataclass(frozen=True)
class SetupContext:
    """Явные параметры и инфраструктурные зависимости настройки."""

    log_file: Path
    staging_secret: str
    recover: bool
    project_branch: str
    color: bool
    runner: CommandRunner
    logged_runner: CommandRunner


class SetupReporter:
    """Запись технического журнала и вывод этапов настройки."""

    def __init__(self, context: SetupContext) -> None:
        self.context = context

    def _color(self, code: str) -> str:
        return code if self.context.color else ""

    def initialize(self) -> None:
        self.context.runner.run(
            [
                "install",
                "-d",
                "-o",
                "root",
                "-g",
                "root",
                "-m",
                "0755",
                str(self.context.log_file.parent),
            ]
        )
        self.context.log_file.touch(exist_ok=True)
        self.context.log_file.chmod(0o640)
        with self.context.log_file.open("a", encoding="utf-8") as stream:
            stamp = datetime.now(timezone.utc).astimezone().strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            stream.write(f"\n===== setup infra-manager {stamp} =====\n")

    def write_log(self, message: str) -> None:
        stamp = datetime.now(timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with self.context.log_file.open("a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")

    def stage(self, message: str) -> None:
        self.write_log(f"ЭТАП: {message}")
        print(
            f"\n{self._color(chr(27)+'[1m')}{self._color(chr(27)+'[34m')}"
            f"==> {message}{self._color(chr(27)+'[0m')}"
        )

    def ok(self, message: str) -> None:
        self.write_log(f"ОК: {message}")
        print(
            f"{self._color(chr(27)+'[1m')}{self._color(chr(27)+'[32m')}"
            f"[ОК]{self._color(chr(27)+'[0m')} {message}"
        )

    def info(self, message: str) -> None:
        self.write_log(f"ИНФО: {message}")
        print(
            f"{self._color(chr(27)+'[1m')}{self._color(chr(27)+'[36m')}"
            f"[ИНФО]{self._color(chr(27)+'[0m')} {message}"
        )

    def error(self, message: str) -> None:
        try:
            self.write_log(f"ОШИБКА: {message}")
        except OSError:
            pass
        print(
            f"\n{self._color(chr(27)+'[1m')}{self._color(chr(27)+'[31m')}"
            f"ОШИБКА:{self._color(chr(27)+'[0m')} {message}",
            file=sys.stderr,
        )
        print(
            f"Полный технический лог: {self.context.log_file}",
            file=sys.stderr,
        )


def file_is_nonempty(path: Path) -> bool:
    """Проверить существование непустого обычного файла."""

    return path.is_file() and path.stat().st_size > 0


def install_file(
    context: SetupContext,
    source: Path,
    target: Path,
    mode: str,
    owner: str = "root",
    group: str = "root",
) -> None:
    """Установить файл через общий исполнитель команд настройки."""

    context.runner.run(
        [
            "install",
            "-o",
            owner,
            "-g",
            group,
            "-m",
            mode,
            str(source),
            str(target),
        ]
    )
