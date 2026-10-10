#!/usr/bin/env python3
"""Внутренний первоначальный загрузчик управляющего контура Proxmox.

Выполняется от root на физическом PVE после подготовки временного LXC
публичным установщиком. Выбирает режим, проверяет сохранённые данные,
последовательно создаёт или восстанавливает управляющий контейнер и
удаляет временный контур после подтверждения готовности.

Обычное обслуживание работающего гостя сюда не входит. Полное
восстановление проверяет сохранённое состояние до удаления rootfs.
На контролируемой ошибке временный токен отзывается, однако если процесс
аварийно убит, его отзыв не гарантируется.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_BOOTSTRAP_MODULE_ROOT = Path(__file__).resolve().parent
if str(_BOOTSTRAP_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_MODULE_ROOT))

from bootstrap_runner.access import BootstrapAccessMixin
from bootstrap_runner.cleanup import BootstrapCleanupMixin
from bootstrap_runner.commands import BootstrapCommandsMixin, _format_duration
from bootstrap_runner.constants import INFRA_MANAGER_ROLE, VERSION
from bootstrap_runner.errors import BootstrapError
from bootstrap_runner.infra import BootstrapInfraMixin
from bootstrap_runner.persistence import BootstrapPersistenceMixin
from bootstrap_runner.paths import BootstrapPathsMixin
from bootstrap_runner.role import RoleLookupError, find_role_guest

# Этот файл выполняется на физическом PVE после того, как публичный bootstrap
# уже создал 990 и получил закрытый проект. Здесь находится вся оркестрация
# постоянного infra-manager.

def _find_role_guest(project_dir: Path, role: str) -> tuple[int, str]:
    """Найти управляющего гостя единой проверенной функцией из role.py."""
    try:
        return find_role_guest(project_dir, role)
    except RoleLookupError as exc:
        raise BootstrapError(str(exc)) from exc


class BootstrapHost(
    BootstrapPathsMixin,
    BootstrapCommandsMixin,
    BootstrapPersistenceMixin,
    BootstrapAccessMixin,
    BootstrapInfraMixin,
    BootstrapCleanupMixin,
):
    """Координатор первоначального контура PVE, доступов и его проверки."""
    def __init__(self, mode: str) -> None:
        """Подготовить режим, пути, цвета и роль; до изменения PVE найти управляющий гостевой объект."""
        self.mode = mode
        self._active_timing: tuple[str, float] | None = None
        self._timed_ok_count = 0
        self._section_title: str | None = None
        self._section_started = 0.0
        self.ctid = int(os.environ.get("BOOTSTRAP_RUNNER_CTID", "990"))
        self.ct_hostname = "bootstrap-runner"
        self.project_branch = os.environ.get("PROJECT_BRANCH", "main")
        default_project_dir = Path("/var/lib/bootstrap-runner/project")
        if not default_project_dir.is_dir():
            default_project_dir = Path(__file__).resolve().parents[2]
        self.project_dir = Path(
            os.environ.get("PROJECT_DIR", str(default_project_dir))
        )

        self.setup_host_paths()

        self.infra_role = INFRA_MANAGER_ROLE
        if self.project_dir.is_dir():
            self.infra_ctid, self.infra_hostname = _find_role_guest(
                self.project_dir,
                self.infra_role,
            )
        else:
            self.infra_ctid, self.infra_hostname = self._find_role_guest_in_runner(
                self.infra_role,
            )
        self.setup_infra_paths()
        self.color = not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.c_reset = "\033[0m" if self.color else ""
        self.c_bold = "\033[1m" if self.color else ""
        self.c_green = "\033[32m" if self.color else ""
        self.c_blue = "\033[34m" if self.color else ""
        self.c_yellow = "\033[33m" if self.color else ""
        self.c_red = "\033[31m" if self.color else ""
        self.c_cyan = "\033[36m" if self.color else ""

    def _find_role_guest_in_runner(self, role: str) -> tuple[int, str]:
        """Найти гостя по роли в закрытом проекте внутри временного 990."""

        helper = (
            self.project_dir
            / "scripts"
            / "bootstrap-runner"
            / "bootstrap_runner"
            / "role.py"
        )
        result = self.ct_exec(
            "python3",
            str(helper),
            str(self.project_dir),
            role,
            check=False,
            capture=True,
        )
        if result.returncode:
            detail = (result.stderr or "").strip()
            suffix = f": {detail}" if detail else ""
            self.fail(
                f"Не удалось найти гостя с ролью {role!r} "
                f"в закрытом проекте LXC {self.ctid}{suffix}"
            )

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                f"LXC {self.ctid} вернул некорректное описание роли {role!r}"
            ) from exc

        vmid = payload.get("vmid")
        name = payload.get("name")
        if not isinstance(vmid, int) or vmid <= 0 or not isinstance(name, str) or not name:
            self.fail(
                f"LXC {self.ctid} вернул некорректное описание роли {role!r}"
            )
        return vmid, name

    def finish_section(self, *, interrupted: bool = False) -> None:
        """Подвести итог раздела без изменения результата операций."""

        title = self._section_title
        if title is None:
            return
        elapsed = _format_duration(time.monotonic() - self._section_started)
        marker = "[ИТОГ]" if not interrupted else "[ПРЕРВАНО]"
        print(f"{marker} {title:<55} ({elapsed})", flush=True)
        self._section_title = None

    def log(self, message: str) -> None:
        """Начать новый раздел, завершив учёт времени предыдущего."""
        self.finish_section()
        self._section_title = message
        self._section_started = time.monotonic()
        print(f"\n{self.c_bold}{self.c_blue}==> {message}{self.c_reset}")

    def ok(self, message: str) -> None:
        """Сообщить об успешном действии; при активном этапе добавить длительность."""
        if self._active_timing is not None:
            _, started = self._active_timing
            message = f"{message:<55} ({_format_duration(time.monotonic() - started)})"
            self._timed_ok_count += 1
        print(f"{self.c_bold}{self.c_green}[ОК]{self.c_reset} {message}")

    def info(self, message: str) -> None:
        """Вывести информационное сообщение без изменения состояния этапа."""
        print(f"{self.c_bold}{self.c_cyan}[ИНФО]{self.c_reset} {message}")

    def fail(self, message: str) -> None:
        """Остановить сценарий ожидаемой ошибкой для операторского вывода."""
        raise BootstrapError(message)

    def timed_step(self, name: str, operation, *args, **kwargs):
        """Дополнить успешный статус временем, не добавляя отдельной строки."""

        started = time.monotonic()
        previous = self._active_timing
        previous_count = self._timed_ok_count
        self._active_timing = (name, started)
        self._timed_ok_count = 0
        try:
            result = operation(*args, **kwargs)
            if not self._timed_ok_count:
                self.ok(name)
            return result
        except BootstrapError as exc:
            elapsed = _format_duration(time.monotonic() - started)
            raise BootstrapError(f"{exc} (этап «{name}»: {elapsed})") from exc
        finally:
            self._active_timing = previous
            self._timed_ok_count = previous_count

    def check_ready(self) -> None:
        """Подтвердить, что управляющий контейнер работает, а временный 990 и токен удалены."""
        if not self.infra_exists():
            self.fail(f"LXC {self.infra_ctid} отсутствует")
        if self.ct_exists():
            self.fail("после успешного bootstrap временный LXC 990 не должен существовать")
        if self.temporary_token_exists():
            self.fail("после успешного bootstrap временный token 990 не должен существовать")
        self.verify_infra_ready()

    def prepare_runner(self) -> None:
        """Проверить 990 и передать ему только необходимые начальные доступы и инструменты."""
        self.verify_runner_contract()
        self.ensure_host_root_ssh_access()
        self.stage_runner_pve_root_access()
        self.prepare_runner_pve_access()
        self.prepare_runtime()

    def apply(self) -> None:
        """Выбрать новую установку или восстановление по состоянию на PVE.

        При наличии постоянных данных без готового маркера новая установка
        запрещена. Для прерванной установки разрешён повтор только до появления
        невоспроизводимых данных; старая rootfs удаляется после подготовки 990.
        После проверки готовности фиксируется маркер ready и удаляется временный
        контур. Любой промежуточный отказ оставляет часть выполненных действий.
        """
        existed = self.infra_exists()
        unfinished = False

        if self.mode == "apply":
            installation = self.installation_state(guest_exists=existed)
            if installation == "recovery":
                # Маркер отсутствует у старых установок: состояние не трогаем,
                # пока полный аварийный набор не пройдёт проверку.
                self.mode = "recover"
                self.info(
                    f"Обнаружено прежнее состояние {self.host_persistent_root}; "
                    "выбран режим восстановления"
                )
            elif installation == "unfinished":
                self.timed_step(
                    "Проверка незавершённой установки",
                    self.verify_unfinished_installation,
                    guest_exists=existed,
                )
                unfinished = True
                self.info("Повторяем первоначальную установку без сохранённых данных")
                self.timed_step(
                    "Завершение подготовки постоянных каталогов",
                    self.prepare_new_persistent_layout,
                )
            elif installation == "new":
                self.timed_step(
                    "Создание постоянных каталогов",
                    self.prepare_new_persistent_layout,
                )
            # existing проверяется ниже: обычная установка не меняет управляющий контейнер.

        if self.mode == "recover":
            self.timed_step("Проверка сохранённых данных", self.verify_recovery_state)

        if existed and not unfinished:
            self.timed_step("Проверка подключённых данных", self.verify_persistent_layout)
            if self.mode != "recover" and self._read_installation_marker() == "ready":
                # Гость уже готов; повторение установочных шагов и ротация
                # рабочего токена здесь запрещены. Нужно лишь удалить 990.
                self.timed_step("Проверка готового гостя", self.verify_infra_ready, quiet=True)
                self.timed_step("Удаление временного 990", self.finalize_runner)
                self.timed_step("Итоговая проверка", self.check_ready)
                return
            if self.mode != "recover":
                self.fail(
                    f"LXC {self.infra_ctid} уже существует; "
                    "для рабочего infra-manager используйте обычный deploy/repair, "
                    "а для полного пересоздания — recovery"
                )
        elif not existed and self.mode == "recover":
            self.info(
                f"LXC {self.infra_ctid} отсутствует; "
                "recovery создаст новый rootfs на сохранённом состоянии"
            )

        # Подготовка 990 должна предшествовать любому удалению rootfs.
        self.timed_step("Подготовка 990", self.prepare_runner)
        if existed and (self.mode == "recover" or unfinished):
            self.timed_step("Удаление старого rootfs", self.remove_infra_rootfs_for_recovery)
        self.timed_step(f"Создание LXC {self.infra_ctid}", self.create_infra_manager)
        self.timed_step("Подключение постоянных данных", self.attach_persistent_layout)
        self.timed_step("Базовая настройка ОС", self.configure_infra_manager_base)
        self.timed_step("Передача учётных данных", self.handoff_infra)
        self.timed_step(
            "Запуск управляющего контура", self.configure_infra_manager_control_plane
        )
        self.timed_step("Восстановление OpenBao", self.initialize_infra_openbao)
        self.timed_step("Передача SSH CA", self.sync_infra_ssh_ca_to_runner)
        self.timed_step("Настройка SSH-доверия", self.configure_infra_manager_ssh_trust)
        self.timed_step(
            "Удаление начального SSH-доступа",
            self.remove_bootstrap_ssh_access_from_infra,
        )
        self.timed_step("Полная настройка Ansible", self.configure_infra_manager)
        self.timed_step("Проверка готовности", self.verify_infra_ready, quiet=True)
        self.write_installation_marker("ready")
        self.timed_step("Удаление временного 990", self.finalize_runner)
        self.timed_step("Итоговая проверка", self.check_ready)

    @contextmanager
    def execution_lock(self):
        """Запретить параллельную оркестрацию на одном PVE.

        Блокировка находится на PVE, а не внутри временного 990. Она
        освобождается ядром также при аварийном завершении процесса.
        """
        path = self.host_lock_file
        if path.is_symlink() or not path.parent.is_dir():
            self.fail(f"Небезопасный путь блокировки: {path}")
        try:
            fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as exc:
            raise BootstrapError(f"Не удалось открыть блокировку {path}: {exc}") from exc
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_nlink != 1
                or (info.st_mode & 0o077)
            ):
                self.fail(f"Небезопасный файл блокировки: {path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise BootstrapError(
                    "Первоначальное развёртывание уже выполняется на PVE; "
                    "дождитесь завершения текущего процесса"
                ) from exc
            yield
        finally:
            os.close(fd)

    def execute(self) -> None:
        """Выполнить режим под блокировкой; после отказа убрать свой временный 990."""
        with self.execution_lock():
            # Остаточный токен принадлежит первоначальному контуру PVE,
            # а не конкретному объекту 990. После успешной проверки хоста
            # его нужно отзывать даже при ошибке проверки самого LXC.
            host_verified = False
            try:
                self.require_host()
                host_verified = True
                self.verify_runner_contract()
                self.info(f"Закрытый bootstrap {VERSION}, режим: {self.mode}")

                if self.mode in {"apply", "recover"}:
                    self.apply()
                elif self.mode == "check":
                    self.verify_infra_ready(quiet=True)
                    if self._read_installation_marker() == "installing":
                        self.write_installation_marker("ready")
                    self.finalize_runner()
                    self.check_ready()
                elif self.mode == "remove":
                    self.remove_infra()
                elif self.mode == "purge":
                    self.remove_infra()
                    shutil.rmtree(self.host_bootstrap_dir, ignore_errors=True)
                    self.ok(
                        "Старый bootstrap-каталог удалён; "
                        "постоянное состояние /mnt/bindmounts/infra-manager сохранено"
                    )
                else:
                    self.fail(f"неизвестный режим: {self.mode}")
            except BaseException:
                self.finish_section(interrupted=True)
                if host_verified:
                    # Обе очистки независимы: отказ отзыва токена не должен
                    # оставлять в 990 копию постоянного root SSH-ключа PVE.
                    for label, operation in (
                        ("отозвать временный токен PVE", self.remove_private_access),
                        ("удалить одноразовый LXC 990", self.remove_runner_if_present),
                    ):
                        try:
                            operation()
                        except (BootstrapError, OSError) as cleanup_error:
                            print(
                                f"ОШИБКА: не удалось {label}: {cleanup_error}",
                                file=sys.stderr,
                            )
                raise
            else:
                self.finish_section()




def parse_args() -> argparse.Namespace:
    """Выбрать один из взаимоисключающих внутренних режимов; по умолчанию apply."""
    parser = argparse.ArgumentParser(
        description="Закрытая оркестрация первоначального контура Proxmox."
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--check", action="store_const", const="check", dest="mode")
    group.add_argument("--recover", action="store_const", const="recover", dest="mode")
    group.add_argument("--remove", action="store_const", const="remove", dest="mode")
    group.add_argument("--purge", action="store_const", const="purge", dest="mode")
    parser.set_defaults(mode="apply")
    return parser.parse_args()


def main() -> int:
    """Запустить выбранный режим; ожидаемые ошибки вывести кратко и вернуть код 1."""
    args = parse_args()
    try:
        BootstrapHost(args.mode).execute()
    except BootstrapError as exc:
        color = not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        prefix = "\033[1;31mОШИБКА:\033[0m" if color else "ОШИБКА:"
        print(f"\n{prefix} {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
