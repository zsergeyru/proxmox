"""Запуск внешних программ и команды управления контейнерами на PVE.

Все дочерние команды вызываются списком аргументов без shell-подстановок.
Обычный запуск может возвращать вывод, а тихий записывает его в журнал.
При progress=True из потока Ansible извлекаются только названия задач;
полный вывод всегда остаётся в техническом журнале.

BootstrapCommandsMixin требует от владельца атрибутов log_file, c_*,
ctid, infra_ctid и метода info(). Интерфейс run()/pct()/ct_exec()
сохраняется для всех существующих вызывающих модулей и проверок.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .errors import BootstrapError


def _format_duration(seconds: float) -> str:
    """Показать прошедшее время в формате часы:минуты:секунды."""

    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if not hours:
        return f"{minutes:02d}:{remaining_seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


class BootstrapCommandsMixin:
    """Общие команды PVE и запуск внешних средств с журналированием."""

    def show_log_tail(self) -> None:
        print(
            f"{self.c_yellow}Последние строки технического журнала:{self.c_reset}",
            file=sys.stderr,
        )
        try:
            lines = self.log_file.read_text(errors="replace").splitlines()[-30:]
            for line in lines:
                print(line, file=sys.stderr)
        except OSError:
            pass
        print(f"Полный журнал: {self.log_file}", file=sys.stderr)

    def run(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        quiet: bool = False,
        progress: bool = False,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        if env:
            command_env.update(env)

        if quiet and progress:
            # Полный вывод остаётся в журнале, но названия долгих Ansible-задач
            # показываются в основной консоли, чтобы bootstrap не выглядел зависшим.
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(
                    args,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=command_env,
                )
                assert process.stdout is not None
                active_task: str | None = None
                task_started = 0.0

                def finish_task() -> None:
                    nonlocal active_task
                    if active_task is None:
                        return
                    elapsed = time.monotonic() - task_started
                    message = (
                        f"[ВРЕМЯ] Ansible: {active_task}: "
                        f"{_format_duration(elapsed)}"
                    )
                    log.write(message + "\n")
                    log.flush()
                    # Время отдельных Ansible-задач сохраняется только
                    # в подробном журнале; основная консоль остаётся краткой.
                    active_task = None

                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    stripped = line.strip()
                    if stripped.startswith("TASK ["):
                        finish_task()
                        end = stripped.find("]")
                        task = stripped[6:end] if end > 6 else stripped
                        active_task = task
                        task_started = time.monotonic()
                        self.info(f"Ansible: {task}")
                    elif stripped.startswith("PLAY RECAP"):
                        finish_task()
                        self.info("Ansible: формирование итогов")
                    elif stripped.startswith("[ИНФО] "):
                        self.info(stripped.removeprefix("[ИНФО] "))
                    elif stripped.startswith("[ОК] "):
                        # Сообщение вложенного процесса — не итог нашего этапа.
                        print(
                            f"{self.c_bold}{self.c_green}[ОК]{self.c_reset} "
                            f"{stripped.removeprefix('[ОК] ')}",
                            flush=True,
                        )
                    elif stripped.startswith("ОШИБКА: "):
                        print(
                            f"{self.c_bold}{self.c_red}ОШИБКА:{self.c_reset} "
                            f"{stripped.removeprefix('ОШИБКА: ')}",
                            file=sys.stderr,
                            flush=True,
                        )
                returncode = process.wait()
                finish_task()

            result = subprocess.CompletedProcess(args, returncode)
            if check and returncode:
                self.show_log_tail()
                raise BootstrapError(
                    f"команда завершилась с кодом {returncode}: {' '.join(args)}"
                )
            return result

        if quiet:
            # Подробности команд сохраняются в журнале. В консоли остаются
            # только понятные этапы и итоговые статусы.
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with self.log_file.open("a", encoding="utf-8") as log:
                result = subprocess.run(
                    args,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=command_env,
                    check=False,
                )
            if check and result.returncode:
                self.show_log_tail()
                raise BootstrapError(
                    f"команда завершилась с кодом {result.returncode}: {' '.join(args)}"
                )
            return result

        result = subprocess.run(
            args,
            text=True,
            capture_output=capture,
            env=command_env,
            check=False,
        )
        if check and result.returncode:
            if capture and result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
            raise BootstrapError(
                f"команда завершилась с кодом {result.returncode}: {' '.join(args)}"
            )
        return result

    def command_exists(self, name: str) -> bool:
        return shutil.which(name) is not None

    def require_host(self) -> None:
        if os.geteuid() != 0:
            self.fail("сценарий должен выполняться от root на PVE")
        for command in ("pct", "pveum", "pvesh", "pvesm", "python3", "ssh-keygen"):
            if not self.command_exists(command):
                self.fail(f"не найден {command}")
        if not self.host_github_key.is_file() or self.host_github_key.stat().st_size == 0:
            self.fail(f"отсутствует GitHub Deploy Key: {self.host_github_key}")

    def pct(self, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
        return self.run("pct", *args, **kwargs)

    def ct_exec(
        self,
        *args: str,
        quiet: bool = False,
        progress: bool = False,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "pct",
            "exec",
            str(self.ctid),
            "--",
            *args,
            quiet=quiet,
            progress=progress,
            check=check,
            capture=capture,
        )

    def infra_exec(
        self,
        *args: str,
        quiet: bool = False,
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "pct",
            "exec",
            str(self.infra_ctid),
            "--",
            *args,
            quiet=quiet,
            check=check,
            capture=capture,
        )

    def ct_exists(self) -> bool:
        return self.pct("config", str(self.ctid), check=False, capture=True).returncode == 0

    def infra_exists(self) -> bool:
        return self.pct("config", str(self.infra_ctid), check=False, capture=True).returncode == 0

    def pct_config(self, ctid: int) -> str:
        return self.pct("config", str(ctid), capture=True).stdout

    def pct_status(self, ctid: int) -> str:
        output = self.pct("status", str(ctid), capture=True).stdout.strip()
        return output.split()[-1] if output else ""

    def _set_mode_owner(
        self,
        path: Path,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        path.chmod(mode)
        os.chown(path, uid, gid)

    def _copy_access_file(
        self,
        source: Path,
        target: Path,
        *,
        mode: int,
        uid: int,
        gid: int,
    ) -> None:
        if not source.is_file() or source.stat().st_size == 0:
            self.fail(f"отсутствует исходный файл доступа: {source}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        self._set_mode_owner(target, mode, uid, gid)
