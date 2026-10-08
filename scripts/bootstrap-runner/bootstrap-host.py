#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_BOOTSTRAP_MODULE_ROOT = Path(__file__).resolve().parent
if str(_BOOTSTRAP_MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_MODULE_ROOT))

from bootstrap_runner.access import BootstrapAccessMixin
from bootstrap_runner.cleanup import BootstrapCleanupMixin
from bootstrap_runner.constants import INFRA_MANAGER_ROLE, VERSION
from bootstrap_runner.errors import BootstrapError
from bootstrap_runner.infra import BootstrapInfraMixin
from bootstrap_runner.persistence import BootstrapPersistenceMixin

# Этот файл выполняется на физическом PVE после того, как публичный bootstrap
# уже создал 990 и получил закрытый проект. Здесь находится вся оркестрация
# постоянного infra-manager.

def _plain_top_level_scalars(path: Path) -> dict[str, str]:
    """Прочитать простые верхнеуровневые scalar-поля guest.yaml без PyYAML."""

    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise BootstrapError(f"Не удалось прочитать {path}: {exc}") from exc

    for raw in lines:
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition(":")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key] = value
    return values


def _find_role_guest(project_dir: Path, role: str) -> tuple[int, str]:
    """Найти VMID и имя единственного гостя с указанной ролью."""

    guests_dir = project_dir / "infrastructure" / "guests"
    matches: list[tuple[int, str, Path]] = []
    for manifest in sorted(guests_dir.glob("*/guest.yaml")):
        values = _plain_top_level_scalars(manifest)
        if values.get("role") != role:
            continue

        directory_match = re.fullmatch(r"(\d{3})-(.+)", manifest.parent.name)
        if directory_match is None:
            raise BootstrapError(
                f"Каталог гостя с ролью {role!r} имеет неверное имя: "
                f"{manifest.parent.name}"
            )
        directory_vmid = int(directory_match.group(1))
        directory_name = directory_match.group(2)
        try:
            manifest_vmid = int(values.get("vmid", ""))
        except ValueError as exc:
            raise BootstrapError(
                f"{manifest}: роль {role!r} имеет некорректный vmid"
            ) from exc
        manifest_name = values.get("name", "")

        if manifest_vmid != directory_vmid or manifest_name != directory_name:
            raise BootstrapError(
                f"{manifest}: vmid/name не соответствуют имени каталога"
            )
        matches.append((manifest_vmid, manifest_name, manifest))

    if not matches:
        raise BootstrapError(f"Не найден гость с ролью {role!r}")
    if len(matches) != 1:
        locations = ", ".join(str(item[2]) for item in matches)
        raise BootstrapError(
            f"Роль {role!r} должна принадлежать одному гостю: {locations}"
        )
    vmid, name, _ = matches[0]
    return vmid, name



def _format_duration(seconds: float) -> str:
    """Показать прошедшее время в формате часы:минуты:секунды."""

    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if not hours:
        return f"{minutes:02d}:{remaining_seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}"


class BootstrapHost(
    BootstrapPersistenceMixin,
    BootstrapAccessMixin,
    BootstrapInfraMixin,
    BootstrapCleanupMixin,
):
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self._active_timing: tuple[str, float] | None = None
        self._timed_ok_count = 0
        self._started_at = time.monotonic()
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

        self.host_bootstrap_dir = Path(
            os.environ.get("HOST_BOOTSTRAP_DIR", "/root/.config/proxmox-bootstrap")
        )
        self.host_github_key = Path(
            os.environ.get(
                "HOST_GITHUB_KEY",
                str(self.host_bootstrap_dir / "github_proxmox_repo_ed25519"),
            )
        )

        self.host_persistent_root = Path("/mnt/bindmounts/infra-manager")
        self.host_pve_only_dir = self.host_persistent_root / "pve-only"
        self.host_access_dir = self.host_persistent_root / "access"
        self.host_state_dir = self.host_persistent_root / "state"
        self.host_access_pve_host_dir = self.host_access_dir / "pve-host"
        self.host_access_ca_dir = self.host_access_dir / "ca"
        self.host_pve_root_key = self.host_access_pve_host_dir / "root_ed25519"
        self.host_pve_root_known_hosts = (
            self.host_access_pve_host_dir / "known_hosts"
        )
        self.host_pve_ca = self.host_access_ca_dir / "pve-root-ca.crt"
        self.host_openbao_dir = self.host_pve_only_dir / "openbao"
        self.host_recovery_dir = self.host_pve_only_dir / "recovery"
        self.host_recovery_github_key = (
            self.host_recovery_dir / "github_proxmox_repo_ed25519"
        )
        self.host_openbao_unseal_key = self.host_openbao_dir / "unseal.key"
        self.host_openbao_ssh_access = self.host_openbao_dir / "ssh-access.json"
        self.host_openbao_kv_access = self.host_openbao_dir / "kv-access.json"
        self.host_state_openbao_dir = self.host_state_dir / "openbao"
        self.host_state_openbao_raft_dir = (
            self.host_state_openbao_dir / "raft"
        )
        self.host_state_opentofu_file = (
            self.host_state_dir / "opentofu" / "state" / "proxmox.tfstate"
        )
        self.host_state_semaphore_db = (
            self.host_state_dir / "semaphore" / "semaphore.sqlite"
        )
        self.host_root_authorized_keys = Path("/root/.ssh/authorized_keys")
        self.host_ssh_public_key = Path("/etc/ssh/ssh_host_ed25519_key.pub")
        self.host_pve_root_key_comment = "infra-manager-pve-root"
        self.host_template_marker = Path(
            os.environ.get(
                "HOST_TEMPLATE_MARKER",
                str(self.host_bootstrap_dir / "debian13-template.ref"),
            )
        )
        self.log_file = Path(
            os.environ.get("HOST_LOG_FILE", "/var/log/proxmox-bootstrap.log")
        )
        self.host_openbao_unseal_command = Path(
            "/usr/local/sbin/infra-manager-openbao-unseal"
        )
        self.host_openbao_library = Path(
            "/usr/local/lib/infra-manager/openbao_host"
        )
        self.host_openbao_config = Path(
            "/etc/infra-manager/openbao-host.json"
        )

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
        self.infra_project_dir = Path("/var/lib/infra-manager/bootstrap-repo")
        self.infra_access_dir = Path("/mnt/pve-access")
        self.infra_state_dir = Path("/mnt/persistent-state")
        self.infra_bootstrap_secret_dir = Path(
            "/run/infra-manager/bootstrap-secrets"
        )
        self.infra_github_key = (
            self.infra_bootstrap_secret_dir / "github_proxmox_repo_ed25519"
        )
        self.infra_github_config = Path("/root/.ssh/github_config")
        self.infra_github_known_hosts = Path("/root/.ssh/github_known_hosts")
        self.infra_pve_api_env = (
            self.infra_bootstrap_secret_dir / "pve-api.env"
        )
        self.infra_runtime_pve_api_env = Path(
            "/run/infra-manager/secrets/pve-api.env"
        )
        self.infra_pve_ca = self.infra_access_dir / "ca" / "pve-root-ca.crt"
        self.runner_pve_host_dir = Path("/etc/bootstrap-runner/pve-host")
        self.infra_bootstrap_key_comment = "infra-manager-bootstrap"
        self.infra_pve_host_dir = self.infra_access_dir / "pve-host"

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

        script = r"""
import json
import re
import sys
from pathlib import Path

project_dir = Path(sys.argv[1])
role = sys.argv[2]
guests_dir = project_dir / "infrastructure" / "guests"
matches = []

for manifest in sorted(guests_dir.glob("*/guest.yaml")):
    values = {}
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        key, separator, value = raw.partition(":")
        if not separator:
            continue
        value = value.strip()
        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in {"'", '"'}
        ):
            value = value[1:-1]
        values[key.strip()] = value

    if values.get("role") != role:
        continue

    directory_match = re.fullmatch(r"(\d{3})-(.+)", manifest.parent.name)
    if directory_match is None:
        raise SystemExit(f"invalid guest directory: {manifest.parent.name}")

    try:
        manifest_vmid = int(values.get("vmid", ""))
    except ValueError as exc:
        raise SystemExit(f"invalid vmid in {manifest}") from exc

    directory_vmid = int(directory_match.group(1))
    directory_name = directory_match.group(2)
    manifest_name = values.get("name", "")
    if manifest_vmid != directory_vmid or manifest_name != directory_name:
        raise SystemExit(f"guest identity mismatch: {manifest}")

    matches.append((manifest_vmid, manifest_name))

if len(matches) != 1:
    raise SystemExit(f"role {role!r} matches {len(matches)} guests")

print(json.dumps({"vmid": matches[0][0], "name": matches[0][1]}))
""".strip()

        result = self.ct_exec(
            "python3",
            "-c",
            script,
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
        self.finish_section()
        self._section_title = message
        self._section_started = time.monotonic()
        print(f"\n{self.c_bold}{self.c_blue}==> {message}{self.c_reset}")

    def ok(self, message: str) -> None:
        if self._active_timing is not None:
            _, started = self._active_timing
            message = f"{message:<55} ({_format_duration(time.monotonic() - started)})"
            self._timed_ok_count += 1
        print(f"{self.c_bold}{self.c_green}[ОК]{self.c_reset} {message}")

    def info(self, message: str) -> None:
        print(f"{self.c_bold}{self.c_cyan}[ИНФО]{self.c_reset} {message}")

    def fail(self, message: str) -> None:
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

    def check_ready(self) -> None:
        if not self.infra_exists():
            self.fail(f"LXC {self.infra_ctid} отсутствует")
        if self.ct_exists():
            self.fail("после успешного bootstrap временный LXC 990 не должен существовать")
        if self.temporary_token_exists():
            self.fail("после успешного bootstrap временный token 990 не должен существовать")
        self.verify_infra_ready()

    def prepare_runner(self) -> None:
        self.verify_runner_contract()
        self.ensure_host_root_ssh_access()
        self.stage_runner_pve_root_access()
        self.prepare_runner_pve_access()
        self.prepare_runtime()

    def apply(self) -> None:
        existed = self.infra_exists()

        if self.mode == "recover":
            self.timed_step("Проверка сохранённых данных", self.verify_recovery_state)

        if existed:
            if self.mode == "recover":
                self.timed_step("Проверка подключённых данных", self.verify_persistent_layout)
                self.timed_step("Удаление старого rootfs", self.remove_infra_rootfs_for_recovery)
                existed = False
            else:
                self.timed_step("Проверка подключённых данных", self.verify_persistent_layout)
                self.fail(
                    f"LXC {self.infra_ctid} уже существует; "
                    "для рабочего infra-manager используйте обычный deploy/repair, "
                    "а для полного пересоздания — recovery"
                )
        elif self.mode == "recover":
            self.info(
                f"LXC {self.infra_ctid} отсутствует; "
                "recovery создаст новый rootfs на сохранённом состоянии"
            )
        else:
            self.timed_step("Создание постоянных каталогов", self.prepare_new_persistent_layout)

        self.timed_step("Подготовка 990", self.prepare_runner)
        self.timed_step(f"Создание LXC {self.infra_ctid}", self.create_infra_manager)
        self.timed_step("Подключение постоянных данных", self.attach_persistent_layout)
        self.timed_step("Базовая настройка ОС", self.configure_infra_manager_base)
        self.timed_step(
            "Передача учётных данных",
            self.handoff_infra,
            "recover" if self.mode == "recover" else "apply",
        )
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
        self.timed_step("Удаление временного 990", self.finalize_runner)
        self.timed_step("Итоговая проверка", self.check_ready)

    def execute(self) -> None:
        # Любой режим приходит сюда уже после подготовки временного 990
        # публичным bootstrap-pve.py.
        self.require_host()
        self.verify_runner_contract()
        self.info(f"Закрытый bootstrap {VERSION}, режим: {self.mode}")

        try:
            if self.mode in {"apply", "recover"}:
                self.apply()
            elif self.mode == "check":
                self.verify_infra_ready(quiet=True)
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
            raise
        else:
            self.finish_section()


def parse_args() -> argparse.Namespace:
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
