"""Общие безопасные примитивы для поэтапного переноса infra-manager на Python."""

from __future__ import annotations

import os
import shlex
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
LOG_LEVEL_ENV = "INFRA_LOG_LEVEL"
LOG_LEVELS = frozenset({"normal", "verbose", "quiet"})


def log_level() -> str:
    """Вернуть выбранный уровень вывода инфраструктурных заданий."""
    value = os.environ.get(LOG_LEVEL_ENV, "normal").strip().lower()
    if value not in LOG_LEVELS:
        allowed = ", ".join(sorted(LOG_LEVELS))
        raise InfraManagerError(
            f"{LOG_LEVEL_ENV} должен быть одним из значений: {allowed}"
        )
    return value


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


def format_duration(seconds: float) -> str:
    """Показать прошедшее время с точностью до секунды."""
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


@dataclass(frozen=True)
class Console:
    """Единый формат сообщений, совместимый с текущими shell-скриптами."""

    out: object = sys.stdout
    err: object = sys.stderr

    def _color(self, code: str, *, error: bool = False) -> str:
        stream = self.err if error else self.out
        mode = os.environ.get("INFRA_MANAGER_COLOR", "auto")
        enabled = (
            (mode == "1" or (mode == "auto" and getattr(stream, "isatty", lambda: False)()))
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )
        return code if enabled else ""

    def info(self, message: str) -> None:
        if log_level() == "quiet":
            return
        cyan = self._color("\033[36m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{cyan}[ИНФО]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def detail(self, message: str) -> None:
        """Показать диагностическое сообщение только в verbose."""
        if log_level() != "verbose":
            return
        self.info(message)

    def ok(self, message: str) -> None:
        if log_level() == "quiet":
            return
        green = self._color("\033[32m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{green}[ОК]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def result(self, message: str) -> None:
        """Показать итог задания независимо от уровня вывода."""
        green = self._color("\033[32m")
        bold = self._color("\033[1m")
        reset = self._color("\033[0m")
        print(
            f"{bold}{green}[ОК]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def timing(
        self,
        message: str,
        elapsed: float,
        *,
        interrupted: bool = False,
        failed: bool = False,
    ) -> None:
        """Показать длительность операции, включая прерванную."""
        label = "ПРЕРВАНО" if interrupted else "ОШИБКА" if failed else "ВРЕМЯ"
        color = self._color("\033[33m" if interrupted else "\033[31m" if failed else "\033[36m")
        reset = self._color("\033[0m")
        print(
            f"{color}[{label}]{reset} {message}: {format_duration(elapsed)}",
            file=self.out,
            flush=True,
        )

    def warning(self, message: str) -> None:
        """Показать предупреждение даже при тихой проверке состояния."""
        yellow = self._color("\033[33m")
        reset = self._color("\033[0m")
        print(
            f"{yellow}[ПРЕДУПРЕЖДЕНИЕ]{reset} {message}",
            file=self.out,
            flush=True,
        )

    def error(self, message: str) -> None:
        red = self._color("\033[31m", error=True)
        bold = self._color("\033[1m", error=True)
        reset = self._color("\033[0m", error=True)
        print(
            f"{bold}{red}ОШИБКА:{reset} {message}",
            file=self.err,
            flush=True,
        )

    def failure(self, exc: Exception, *, vmid: int | None = None) -> None:
        """Показать причину и действие без аргументов команды и сырого stderr."""
        if isinstance(exc, CommandError):
            tool = Path(exc.argv[0]).name
            detail = (exc.stderr or "").lower()
            if "unreachable" in detail or "connection refused" in detail or "connection timed out" in detail:
                message = "Не удалось подключиться к узлу или службе. Проверьте, что нужный узел запущен и доступен по сети; затем повторите операцию."
            elif tool == "ansible-playbook":
                message = "Настройка гостя не завершена. Проверьте состояние: infra-manager status <VMID>. Если служба не готова, проверьте её журнал внутри гостя, исправьте причину и повторите deploy."
            elif tool == "git":
                message = "Не удалось обновить проект. Проверьте доступ к Git-серверу, ключ доступа и наличие выбранной ветки; затем повторите операцию."
            elif tool == "docker":
                message = "Не удалось выполнить операцию Docker. Проверьте службу Docker и состояние контейнеров; затем повторите операцию."
            elif tool in {"tofu", "terraform"}:
                message = "Не удалось применить описание инфраструктуры. Проверьте доступ к PVE и параметры выбранного гостя; затем повторите операцию."
            else:
                message = "Операция не завершена. Проверьте состояние: infra-manager status <VMID>. Передайте администратору название операции и код ошибки."
            if vmid is not None:
                message = message.replace("<VMID>", str(vmid))
            self.error(f"{message} Код ошибки: {exc.returncode}.")
        elif isinstance(exc, OSError):
            self.error("Не удалось обратиться к файлу или запустить программу. Проверьте наличие необходимых файлов, программ и права доступа; затем повторите операцию.")
        else:
            self.error(str(exc))


console = Console()


HOST_SEMAPHORE_STATE_DIR = Path("/mnt/persistent-state/semaphore")
RUNTIME_SEMAPHORE_STATE_DIR = Path("/var/lib/semaphore")


def _runtime_activation_scope() -> str:
    """Вернуть пространство PID текущего исполнителя."""

    return "host" if HOST_SEMAPHORE_STATE_DIR.is_dir() else "runtime"


def _default_runtime_activation_marker() -> Path:
    """Выбрать один физический marker для управляющего гостя и infra-runtime."""

    if _runtime_activation_scope() == "host":
        root = HOST_SEMAPHORE_STATE_DIR
    else:
        root = RUNTIME_SEMAPHORE_STATE_DIR
    return root / ".infra-manager-runtime-activation-pending"


RUNTIME_ACTIVATION_MARKER = _default_runtime_activation_marker()


def require_runtime_activation_idle() -> None:
    """Не запускать новое задание во время замены infra-runtime."""
    if RUNTIME_ACTIVATION_MARKER.exists():
        raise InfraManagerError(
            "Идёт активация новой управляющей среды infra-manager; "
            "дождитесь её завершения и повторите задание"
        )


def reserve_runtime_activation() -> None:
    """Зарезервировать окно активации и записать PID текущего задания."""
    RUNTIME_ACTIVATION_MARKER.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            RUNTIME_ACTIVATION_MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
    except FileExistsError as exc:
        raise InfraManagerError(
            "Активация управляющей среды infra-manager уже ожидается; "
            "повторное самообновление infra-manager запрещено"
        ) from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(f"{_runtime_activation_scope()}:{os.getpid()}\n")
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        RUNTIME_ACTIVATION_MARKER.unlink(missing_ok=True)
        raise


def cancel_runtime_activation() -> None:
    """Снять резерв активации после неуспешного самообновления infra-manager."""
    RUNTIME_ACTIVATION_MARKER.unlink(missing_ok=True)


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


@dataclass(frozen=True)
class CommandRunner:
    """Единый исполнитель внешних команд infra-manager."""

    default_log_file: Path | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        capture: bool = False,
        quiet: bool = False,
        stream_output: bool = False,
        log_file: Path | None = None,
        check: bool = True,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        sensitive_args: Sequence[int] = (),
    ) -> subprocess.CompletedProcess[str]:
        """Запустить внешнюю команду с едиными правилами вывода и ошибок."""
        if not argv:
            raise ValueError("argv не должен быть пустым")
        if sum((capture, quiet, stream_output)) > 1:
            raise ValueError(
                "capture, quiet и stream_output нельзя включать одновременно"
            )

        actual_log = log_file if log_file is not None else self.default_log_file
        if stream_output and actual_log is not None:
            raise ValueError(
                "stream_output нельзя сочетать с записью вывода в файл"
            )
        redacted = _redact_argv(argv, sensitive_args)

        stdout: object | None = None
        stderr: object | None = None
        stream = None

        auto_capture = False
        if actual_log is not None:
            actual_log.parent.mkdir(parents=True, exist_ok=True)
            stream = actual_log.open("a", encoding="utf-8")
            stream.write(f"КОМАНДА: {shlex.join(redacted)}\n")
            stream.flush()
            stdout = stream
            stderr = subprocess.STDOUT
        elif quiet:
            stdout = subprocess.DEVNULL
            stderr = subprocess.DEVNULL
        elif capture:
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
        elif stream_output:
            # Высокоуровневые дочерние команды infra-manager должны передавать
            # свой уже отфильтрованный вывод вызывающему терминалу или HTTP-потоку.
            pass
        elif log_level() != "verbose":
            # В обычном режиме внешние инструменты не засоряют журнал
            # успешными подробностями. При ошибке их хвост попадёт в исключение.
            stdout = subprocess.PIPE
            stderr = subprocess.PIPE
            auto_capture = True

        try:
            result = subprocess.run(
                list(argv),
                check=False,
                cwd=cwd,
                env=dict(env) if env is not None else None,
                input=input_text,
                text=True,
                stdout=stdout,
                stderr=stderr,
            )
        finally:
            if stream is not None:
                stream.close()

        if check and result.returncode != 0:
            detail = result.stderr
            if auto_capture:
                combined = "\n".join(
                    part.rstrip()
                    for part in (result.stdout, result.stderr)
                    if part and part.strip()
                )
                detail = "\n".join(combined.splitlines()[-25:]) or None
            if actual_log is not None and actual_log.is_file():
                tail = actual_log.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).splitlines()[-25:]
                detail = "\n".join(tail)
            raise CommandError(
                argv,
                result.returncode,
                detail,
                sensitive_args,
            )

        return result


command_runner = CommandRunner()


def run(
    argv: Sequence[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    stream_output: bool = False,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
    sensitive_indices: Sequence[int] = (),
) -> subprocess.CompletedProcess[str]:
    """Совместимая оболочка над единым CommandRunner."""
    return command_runner.run(
        argv,
        check=check,
        capture=capture_output,
        stream_output=stream_output,
        cwd=cwd,
        env=env,
        input_text=input_text,
        sensitive_args=sensitive_indices,
    )
