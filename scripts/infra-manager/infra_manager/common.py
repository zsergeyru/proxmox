"""Общие команды, уровни вывода и обработка ошибок infra-manager.

Внешние команды выполняются с проверкой результата. Известные секретные
аргументы скрываются в диагностике; сырой stderr автоматически безопасным
не считается и не должен без проверки показываться пользователю.
"""

from __future__ import annotations

import errno
import os
import re
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


# Диагностику Ansible очищаем по принципу разрешённого списка.
# Полный вывод может содержать пароли из параметров заданий.
_ANSIBLE_SAFE_ERRORS: tuple[tuple[str, str], ...] = (
    (
        "Истекло время ожидания команды Immich для администратора",
        "Immich не ответил на запрос сброса пароля администратора за 90 секунд.",
    ),
    (
        "Immich не подтвердил изменение пароля администратора",
        "Immich не подтвердил сброс пароля администратора.",
    ),
    (
        "Администратор в базе Immich не совпадает с описанием гостя",
        "Адрес администратора в Immich отличается от указанного в проекте; пароль не менялся.",
    ),
    (
        "Extra data: line",
        "JSON настройки содержит лишние данные.",
    ),
)


def _ansible_failure_summary(output: str | None) -> str:
    """Показать этап и классифицированную причину, но не сырой fatal/msg.

    Вывод Ansible способен содержать секреты в полях args/msg/stdout.
    Поэтому извлекаем только название задания и заранее заданные пояснения
    к известным типам ошибок. Сырые пути и тексты исключений не печатаем.
    """
    if not output:
        return ""
    details: list[str] = []
    tasks = re.findall(r"(?m)^TASK \[([^\]\r\n]{1,180})\]", output)
    if tasks:
        # Не печатать динамические заголовки с переменными или слишком длинными полями.
        task = tasks[-1].strip()
        if "{{" not in task and "}}" not in task:
            details.append(f"Последнее задание Ansible: {task}.")

    # Учитываем только последний шаг, чтобы старые предупреждения не
    # выдавались за причину окончательного отказа.
    final_task = output.rfind("TASK [")
    failure = (output[final_task:] if final_task >= 0 else output).lower()
    lookup_missing = (
        "could not locate file in lookup" in failure
        or (
            ("lookup('file'" in failure or "lookup plugin 'file'" in failure)
            and ("not found" in failure or "no such file" in failure)
        )
    )
    classified = (
        (
            lookup_missing,
            "[ANSIBLE_FILE_LOOKUP] Файл для lookup('file') не найден на "
            "контроллере Ansible. Проверьте путь относительно снимка Git, "
            "а не путь внутри настраиваемого LXC.",
        ),
        (
            "permission denied" in failure or "errno 13" in failure,
            "[ANSIBLE_EACCES] Доступ к файлу или ресурсу запрещён. "
            "Проверьте владельца и права в соответствующем задании.",
        ),
        (
            "is undefined" in failure or "undefined variable" in failure,
            "[ANSIBLE_UNDEFINED] В задании не определена обязательная "
            "переменная. Проверьте vars, provision.yaml и параметры playbook.",
        ),
        (
            "no such file or directory" in failure or "errno 2" in failure,
            "[ANSIBLE_ENOENT] Не найден необходимый файл или каталог. "
            "Проверьте наличие источника и место выполнения задания.",
        ),
        (
            "yaml.parser.parsererror" in failure
            or "yaml.scanner.scannererror" in failure,
            "[ANSIBLE_YAML] Ошибка синтаксиса YAML. Проверьте файл "
            "конфигурации, используемый последним заданием.",
        ),
        (
            "unreachable!" in failure,
            "[ANSIBLE_UNREACHABLE] Управляемый узел недоступен через "
            "подтверждённый SSH-доступ.",
        ),
    )
    for matched, description in classified:
        if matched:
            details.append(f"Причина: {description}")
            break
    else:
        for fingerprint, description in _ANSIBLE_SAFE_ERRORS:
            if fingerprint.lower() in failure:
                details.append(f"Причина: {description}")
                break
        else:
            details.append(
                "Причина не распознана безопасным фильтром Ansible; "
                "необработанный вывод скрыт, так как может содержать секреты."
            )
    return " ".join(details)


_SAFE_EXECUTABLES = frozenset({
    "docker", "git", "ssh", "python3", "sh", "bash", "pct",
    "tofu", "packer", "ansible-playbook", "systemd-run", "findmnt",
})


def _os_error_diagnostic(exc: OSError) -> str:
    """Безопасная причина системного отказа без произвольных путей и секретов."""
    code = errno.errorcode.get(exc.errno, "UNKNOWN")
    explanation = {
        "ENOENT": "файл или программа не найдены",
        "EACCES": "нет разрешения на доступ",
        "EPERM": "операция запрещена системой",
        "ENOTDIR": "часть пути не является каталогом",
        "EROFS": "файловая система доступна только для чтения",
        "ENOSPC": "недостаточно свободного места",
        "EMFILE": "достигнут лимит открытых файлов процесса",
    }.get(code, "системная ошибка при обращении к файлу или запуске программы")
    target = Path(exc.filename).name if isinstance(exc.filename, str) else ""
    program = f"; программа: {target}" if target in _SAFE_EXECUTABLES else ""
    return f"{type(exc).__name__} ({code}): {explanation}{program}"


def read_env_file(path: Path) -> dict[str, str]:
    """Прочитать KEY=VALUE из файла без выполнения его содержимого."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


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
    отдельно для анализа причины, но не включается в текст исключения.
    """

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stderr: str | None = None,
        sensitive_indices: Sequence[int] = (),
    ) -> None:
        """Сохранить код отказа и очищенные аргументы команды для диагностики.

        Полученный stderr хранится отдельно и не считается очищенным
        от секретов; его нельзя без проверки передавать пользователю."""
        self.argv = _redact_argv(argv, sensitive_indices)
        self.returncode = returncode
        self.stderr = stderr

        # stderr может содержать токены и значения конфигурации.
        # Храним его для внутренней классификации ошибок, но исключение
        # при преобразовании в строку не должно раскрывать сырые данные.
        command = " ".join(self.argv)
        super().__init__(f"Команда завершилась с кодом {returncode}: {command}")


def format_duration(seconds: float) -> str:
    """Показать прошедшее время с точностью до секунды."""
    total = max(0, int(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"



def _tofu_failure_summary(
    argv: Sequence[str], output: str | None,
) -> tuple[str, str]:
    """Назвать этап и безопасную причину отказа OpenTofu.

    Вывод tofu не доверен: в нём могут оказаться значения переменных,
    токены и пути. Отображаются только фиксированные сообщения, а не
    произвольные строки stdout/stderr.
    """
    stage = "неизвестный этап"
    for index, arg in enumerate(argv):
        if arg in {"init", "plan", "apply", "show", "validate", "state"}:
            stage = arg
            if arg == "state" and index + 1 < len(argv):
                if argv[index + 1] in {"pull", "list", "show"}:
                    stage += " " + argv[index + 1]
            break

    detail = (output or "").casefold()
    if "permission denied" in detail or "operation not permitted" in detail:
        reason = "Отказано в доступе к рабочим файлам, провайдеру или состоянию OpenTofu."
    elif (
        "error acquiring the state lock" in detail
        or "state lock" in detail
        or "lock info" in detail
    ):
        reason = "Не удалось получить блокировку состояния OpenTofu; проверьте параллельные задания."
    elif (
        "backend initialization required" in detail
        or "backend configuration changed" in detail
    ):
        reason = "Рабочая копия требует согласования конфигурации backend; автоматическая миграция состояния запрещена."
    elif (
        "failed to install provider" in detail
        or "failed to query available provider packages" in detail
        or "failed to load plugin schemas" in detail
    ):
        reason = "Не удалось подготовить или запустить провайдер Proxmox."
    elif (
        "certificate signed by unknown authority" in detail
        or "x509:" in detail
        or "tls handshake" in detail
    ):
        reason = "Не подтверждён TLS-сертификат или защищённое подключение."
    elif (
        "connection refused" in detail
        or "connection timed out" in detail
        or "no such host" in detail
    ):
        reason = "Не удалось подключиться к необходимой службе; проверьте сеть и доступность PVE."
    elif "no space left on device" in detail:
        reason = "Недостаточно свободного места в рабочем или постоянном хранилище."
    elif "no such file or directory" in detail:
        reason = "Не найден требуемый файл или каталог OpenTofu."
    else:
        reason = "Причина не распознана; требуется закрытая диагностика вывода OpenTofu."
    return stage, reason


@dataclass(frozen=True)
class Console:
    """Единый формат сообщений, совместимый с текущими shell-скриптами."""

    out: object = sys.stdout
    err: object = sys.stderr

    def _color(self, code: str, *, error: bool = False) -> str:
        """Выбрать цвет терминального сообщения с учётом настройки и среды вывода."""
        stream = self.err if error else self.out
        mode = os.environ.get("INFRA_MANAGER_COLOR", "auto")
        enabled = (
            (mode == "1" or (mode == "auto" and getattr(stream, "isatty", lambda: False)()))
            and not os.environ.get("NO_COLOR")
            and os.environ.get("TERM") != "dumb"
        )
        return code if enabled else ""

    def info(self, message: str) -> None:
        """Показать обычный этап операции, кроме режима минимального вывода."""
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
        """Показать подтверждённый успешный шаг в обычном журнале."""
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
        """Вывести понятную ошибку в отдельный поток без секретов."""
        red = self._color("\033[31m", error=True)
        bold = self._color("\033[1m", error=True)
        reset = self._color("\033[0m", error=True)
        print(
            f"{bold}{red}ОШИБКА:{reset} {message}",
            file=self.err,
            flush=True,
        )

    def diagnostic(self, message: str) -> None:
        """Показать только очищенную техническую причину, даже в quiet."""
        print(
            f"[ДИАГНОСТИКА] {message}",
            file=self.err,
            flush=True,
        )

    def failure(self, exc: Exception, *, vmid: int | None = None) -> None:
        """Показать причину и действие без аргументов команды и сырого stderr."""
        if isinstance(exc, CommandError):
            tool = Path(exc.argv[0]).name
            detail = (exc.stderr or "").lower()
            # Итог Ansible всегда содержит unreachable=0, даже если упало
            # локальное задание: нельзя считать его сетевой ошибкой.
            if tool == "ansible-playbook":
                message = "Настройка гостя не завершена. " + _ansible_failure_summary(exc.stderr) + " Проверьте состояние: infra-manager status <VMID>."
            elif tool in {"tofu", "terraform"}:
                stage, reason = _tofu_failure_summary(exc.argv, exc.stderr)
                message = f"Ошибка OpenTofu на этапе {stage}. {reason}"

            elif "unreachable" in detail or "connection refused" in detail or "connection timed out" in detail:
                message = "Не удалось подключиться к узлу или службе. Проверьте, что нужный узел запущен и доступен по сети; затем повторите операцию."
            elif tool == "git":
                message = "Не удалось обновить проект. Проверьте доступ к Git-серверу, ключ доступа и наличие выбранной ветки; затем повторите операцию."
            elif tool == "docker":
                message = "Не удалось выполнить операцию Docker. Проверьте службу Docker и состояние контейнеров; затем повторите операцию."
            else:
                message = "Операция не завершена. Проверьте состояние: infra-manager status <VMID>. Передайте администратору название операции и код ошибки."
            if vmid is not None:
                message = message.replace("<VMID>", str(vmid))
            self.error(f"{message} Код ошибки: {exc.returncode}.")
            safe_tool = tool if tool in _SAFE_EXECUTABLES else "неизвестная команда"
            stage_note = (
                f"; этап {_tofu_failure_summary(exc.argv, exc.stderr)[0]}"
                if tool in {"tofu", "terraform"} else ""
            )
            self.diagnostic(
                f"CommandError: программа {safe_tool}{stage_note}; код возврата {exc.returncode}"
            )
        elif isinstance(exc, OSError):
            self.error("Не удалось обратиться к файлу или запустить программу. Проверьте наличие необходимых файлов, программ и права доступа; затем повторите операцию.")
            self.diagnostic(_os_error_diagnostic(exc))
        else:
            self.error(str(exc))


console = Console()


HOST_OPERATIONS_STATE_DIR = Path("/mnt/persistent-state/operations/activation")
RUNTIME_OPERATIONS_STATE_DIR = Path("/run/infra-manager/activation")
# Первое обновление работающего 910: старый runtime ещё не имеет нового bind mount.
# У него уже подключён state/locks. Это только канал передачи между runtime и systemd,
# без базы, контейнера или API Semaphore.
HOST_TRANSITION_LOCK_DIR = Path("/mnt/persistent-state/locks")
RUNTIME_TRANSITION_LOCK_DIR = Path("/var/lib/infra-manager/locks")
ACTIVATION_MARKER_NAME = "runtime-activation-pending"


def _runtime_activation_scope() -> str:
    """Отличить управляющую службу от процесса в рабочем контейнере."""
    return "host" if HOST_OPERATIONS_STATE_DIR.is_dir() else "runtime"


def _activation_bind_mounted(path: Path) -> bool:
    """Проверить Docker bind mount по mountinfo, включая монтирования одной ФС."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split(" - ", 1)[0].split()
                if len(fields) > 4 and fields[4] == str(path):
                    return True
    except OSError as exc:
        raise InfraManagerError(
            "Невозможно проверить общее подключение каталога активации"
        ) from exc
    return False


def _default_runtime_activation_marker() -> Path:
    """Найти общий маркер, доступный уже запущенному поколению runtime."""
    if _runtime_activation_scope() == "host":
        root = HOST_OPERATIONS_STATE_DIR
    elif _activation_bind_mounted(RUNTIME_OPERATIONS_STATE_DIR):
        root = RUNTIME_OPERATIONS_STATE_DIR
    elif _activation_bind_mounted(RUNTIME_TRANSITION_LOCK_DIR):
        # Первый переход: системная служба прочтёт маркер из того же state/locks.
        root = RUNTIME_TRANSITION_LOCK_DIR
    else:
        # Ничего не создавать в rootfs: reserve_runtime_activation явно откажет.
        root = RUNTIME_OPERATIONS_STATE_DIR
    return root / ACTIVATION_MARKER_NAME


RUNTIME_ACTIVATION_MARKER = _default_runtime_activation_marker()


def _activation_markers() -> tuple[Path, ...]:
    """Проверить оба маршрута на время первого перехода между поколениями."""
    if _runtime_activation_scope() == "host":
        return (
            HOST_OPERATIONS_STATE_DIR / ACTIVATION_MARKER_NAME,
            HOST_TRANSITION_LOCK_DIR / ACTIVATION_MARKER_NAME,
        )
    return (
        RUNTIME_OPERATIONS_STATE_DIR / ACTIVATION_MARKER_NAME,
        RUNTIME_TRANSITION_LOCK_DIR / ACTIVATION_MARKER_NAME,
    )


def require_runtime_activation_idle() -> None:
    """Не запускать новое задание во время замены infra-runtime."""
    if RUNTIME_ACTIVATION_MARKER.exists() or any(
        marker.exists() for marker in _activation_markers()
    ):
        raise InfraManagerError(
            "Идёт активация новой управляющей среды infra-manager; "
            "дождитесь её завершения и повторите задание"
        )


def reserve_runtime_activation() -> None:
    """Зарезервировать окно активации только на подтверждённом общем диске."""
    if (
        _runtime_activation_scope() == "runtime"
        and RUNTIME_ACTIVATION_MARKER.parent
        in {RUNTIME_OPERATIONS_STATE_DIR, RUNTIME_TRANSITION_LOCK_DIR}
        and not _activation_bind_mounted(RUNTIME_ACTIVATION_MARKER.parent)
    ):
        raise InfraManagerError(
            "Нет общего подключения state/operations или state/locks в infra-runtime; "
            "маркер в локальном rootfs создавать запрещено"
        )
    if not RUNTIME_ACTIVATION_MARKER.parent.is_dir():
        raise InfraManagerError(
            "Каталог для общей активации infra-runtime отсутствует; "
            "проверьте bind mount state/locks или state/operations"
        )
    require_runtime_activation_idle()
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
            operation_id = os.environ.get("INFRA_OPERATION_ID", "").strip()
            if operation_id:
                import re

                if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
                    raise InfraManagerError("Некорректный идентификатор самообновления")
            suffix = f":{operation_id}" if operation_id else ""
            stream.write(f"{_runtime_activation_scope()}:{os.getpid()}{suffix}\n")
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
