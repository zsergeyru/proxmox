"""Общие безопасные примитивы для поэтапного переноса infra-manager на Python."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


class InfraManagerError(RuntimeError):
    """Ожидаемая ошибка infra-manager с сообщением для пользователя."""


class CommandError(InfraManagerError):
    """Внешняя команда завершилась с ошибкой."""

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stderr: str | None = None,
    ) -> None:
        self.argv = tuple(argv)
        self.returncode = returncode
        self.stderr = stderr

        command = " ".join(self.argv)
        message = f"Команда завершилась с кодом {returncode}: {command}"
        if stderr:
            message += f"\n{stderr.rstrip()}"

        super().__init__(message)


@dataclass(frozen=True)
class Console:
    """Единый формат сообщений, совместимый с текущими shell-скриптами."""

    out: object = sys.stdout
    err: object = sys.stderr

    def info(self, message: str) -> None:
        print(f"[ИНФО] {message}", file=self.out)

    def ok(self, message: str) -> None:
        print(f"[ОК] {message}", file=self.out)

    def error(self, message: str) -> None:
        print(f"ОШИБКА: {message}", file=self.err)


console = Console()


def require_root() -> None:
    """Остановиться, если команда запущена не от root."""

    if os.geteuid() != 0:
        raise InfraManagerError("Команда должна выполняться от root")


def require_command(name: str) -> Path:
    """Вернуть путь к обязательной внешней команде."""

    path = shutil.which(name)
    if path is None:
        raise InfraManagerError(f"Не найдена обязательная команда: {name}")
    return Path(path)


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Запустить внешнюю команду без shell-интерпретации аргументов."""

    if not argv:
        raise ValueError("argv не должен быть пустым")

    result = subprocess.run(
        list(argv),
        check=False,
        cwd=cwd,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=capture_output,
    )

    if check and result.returncode != 0:
        raise CommandError(argv, result.returncode, result.stderr)

    return result
