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


_SENSITIVE_OPTIONS = {"--token", "--password", "--secret"}
_SENSITIVE_PACKER_VARS = {"proxmox_token", "build_password"}


def _redact_argv(
    argv: Sequence[str],
    sensitive_indices: Sequence[int] = (),
) -> tuple[str, ...]:
    """Скрыть только явно известные чувствительные аргументы."""
    redacted = list(argv)
    explicit = set(sensitive_indices)

    for index, arg in enumerate(argv):
        if index in explicit:
            redacted[index] = "[СКРЫТО]"
            continue

        if arg in _SENSITIVE_OPTIONS:
            if index + 1 < len(redacted):
                redacted[index + 1] = "[СКРЫТО]"
            continue

        for option in _SENSITIVE_OPTIONS:
            prefix = f"{option}="
            if arg.startswith(prefix):
                redacted[index] = f"{option}=[СКРЫТО]"
                break
        else:
            if arg.startswith("-var="):
                assignment = arg[len("-var="):]
                if "=" in assignment:
                    name, _value = assignment.split("=", 1)
                    if name in _SENSITIVE_PACKER_VARS:
                        redacted[index] = f"-var={name}=[СКРЫТО]"

    return tuple(redacted)


class CommandError(InfraManagerError):
    """Внешняя команда завершилась с ошибкой.

    Аргументы команды редактируются по известным правилам. stderr сохраняется
    как диагностический вывод и не считается автоматически очищенным от секретов.
    """

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stderr: str | None = None,
        sensitive_indices: Sequence[int] = (),
    ) -> None:
        self.argv = _redact_argv(argv, sensitive_indices)
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

    @staticmethod
    def _color(code: str) -> str:
        enabled = (
            os.environ.get("INFRA_MANAGER_COLOR", "0") == "1"
            and not os.environ.get("NO_COLOR")
        )
        return code if enabled else ""

    def info(self, message: str) -> None:
        cyan = self._color("\033[36m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{cyan}[ИНФО]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def ok(self, message: str) -> None:
        green = self._color("\033[32m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{green}[ОК]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def error(self, message: str) -> None:
        red = self._color("\033[31m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{red}ОШИБКА:{reset} {message}",
            file=self.err,
            flush=True,
        )


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
    sensitive_indices: Sequence[int] = (),
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
        raise CommandError(
            argv,
            result.returncode,
            result.stderr,
            sensitive_indices,
        )

    return result
