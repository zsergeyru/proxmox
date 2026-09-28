#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

VERSION = "2.0.0-dev1"

# Этот файл выполняется на физическом PVE после того, как публичный bootstrap
# уже создал 990 и получил закрытый проект. Здесь находится вся оркестрация 910.

# Закреплённый официальный Ed25519 host key GitHub защищает первое подключение
# к github.com от подмены ключа через сеть.
GITHUB_ED25519_KNOWN_HOST = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


class BootstrapError(RuntimeError):
    pass


class BootstrapHost:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.ctid = int(os.environ.get("BOOTSTRAP_RUNNER_CTID", "990"))
        self.ct_hostname = "bootstrap-runner"
        self.project_branch = os.environ.get("PROJECT_BRANCH", "feature/bootstrap-990")
        self.project_dir = Path(os.environ.get("PROJECT_DIR", "/var/lib/bootstrap-runner/project"))

        self.host_bootstrap_dir = Path(
            os.environ.get("HOST_BOOTSTRAP_DIR", "/root/.config/proxmox-bootstrap")
        )
        self.host_github_key = Path(
            os.environ.get(
                "HOST_GITHUB_KEY",
                str(self.host_bootstrap_dir / "github_proxmox_repo_ed25519"),
            )
        )
        self.host_pve_root_key = self.host_bootstrap_dir / "pve_root_ed25519"
        self.host_pve_root_known_hosts = (
            self.host_bootstrap_dir / "pve_root_known_hosts"
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

        # 910 — постоянный управляющий контейнер. В отличие от временного 990
        # он сохраняется между запусками и не входит в постоянный OpenTofu-state.
        self.infra_ctid = 910
        self.infra_hostname = "infra-manager"
        self.infra_project_dir = Path("/var/lib/infra-manager/bootstrap-repo")
        self.infra_github_key = Path("/root/.ssh/github_proxmox_repo_ed25519")
        self.infra_github_config = Path("/root/.ssh/github_config")
        self.infra_github_known_hosts = Path("/root/.ssh/github_known_hosts")
        self.infra_staging_secret = Path("/root/.infra-manager-bootstrap/pve-api.env")
        self.runner_pve_host_dir = Path("/etc/bootstrap-runner/pve-host")
        self.infra_pve_host_dir = Path("/etc/infra-manager/pve-host")

        self.color = not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb"
        self.c_reset = "\033[0m" if self.color else ""
        self.c_bold = "\033[1m" if self.color else ""
        self.c_green = "\033[32m" if self.color else ""
        self.c_blue = "\033[34m" if self.color else ""
        self.c_yellow = "\033[33m" if self.color else ""
        self.c_red = "\033[31m" if self.color else ""
        self.c_cyan = "\033[36m" if self.color else ""

    def log(self, message: str) -> None:
        print(f"\n{self.c_bold}{self.c_blue}==> {message}{self.c_reset}")

    def ok(self, message: str) -> None:
        print(f"{self.c_bold}{self.c_green}[ОК]{self.c_reset} {message}")

    def info(self, message: str) -> None:
        print(f"{self.c_bold}{self.c_cyan}[ИНФО]{self.c_reset} {message}")

    def fail(self, message: str) -> None:
        raise BootstrapError(message)

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
        check: bool = True,
        capture: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command_env = os.environ.copy()
        if env:
            command_env.update(env)

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
        for command in ("pct", "pveum", "pvesm", "python3"):
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
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "pct",
            "exec",
            str(self.ctid),
            "--",
            *args,
            quiet=quiet,
            check=check,
        )

    def infra_exec(
        self,
        *args: str,
        quiet: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return self.run(
            "pct",
            "exec",
            str(self.infra_ctid),
            "--",
            *args,
            quiet=quiet,
            check=check,
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

    def ensure_host_root_ssh_access(self) -> None:
        """Подготовить отдельный root SSH-ключ для управляющего контура."""
        self.host_bootstrap_dir.mkdir(parents=True, exist_ok=True)
        if not self.host_pve_root_key.is_file():
            self.run(
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                self.host_pve_root_key_comment,
                "-f",
                str(self.host_pve_root_key),
            )

        public_key = Path(f"{self.host_pve_root_key}.pub")
        if not public_key.is_file() or public_key.stat().st_size == 0:
            self.fail("не удалось подготовить открытый root SSH-ключ PVE")

        root_ssh_dir = self.host_root_authorized_keys.parent
        root_ssh_dir.mkdir(parents=True, exist_ok=True)
        root_ssh_dir.chmod(0o700)

        existing = (
            self.host_root_authorized_keys.read_text(encoding="utf-8").splitlines()
            if self.host_root_authorized_keys.is_file()
            else []
        )
        marker = f" {self.host_pve_root_key_comment}"
        lines = [line for line in existing if not line.rstrip().endswith(marker)]
        lines.append(public_key.read_text(encoding="utf-8").strip())
        self.host_root_authorized_keys.write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
        )
        self.host_root_authorized_keys.chmod(0o600)

        if not self.host_ssh_public_key.is_file():
            self.fail(f"не найден SSH host key PVE: {self.host_ssh_public_key}")
        host_key_parts = self.host_ssh_public_key.read_text(
            encoding="utf-8"
        ).strip().split()
        if len(host_key_parts) < 2:
            self.fail("SSH host key PVE имеет некорректный формат")
        node = self.run("hostname", "-s", capture=True).stdout.strip()
        self.host_pve_root_known_hosts.write_text(
            f"{node} {host_key_parts[0]} {host_key_parts[1]}\n",
            encoding="utf-8",
        )
        self.host_pve_root_known_hosts.chmod(0o644)
        self.ok("Root SSH-доступ управляющего контура к PVE подготовлен")

    def remove_host_root_ssh_authorization(self) -> None:
        """Удалить из root authorized_keys только ключ infra-manager."""
        if not self.host_root_authorized_keys.is_file():
            return
        marker = f" {self.host_pve_root_key_comment}"
        lines = [
            line
            for line in self.host_root_authorized_keys.read_text(
                encoding="utf-8"
            ).splitlines()
            if not line.rstrip().endswith(marker)
        ]
        text = "\n".join(lines)
        self.host_root_authorized_keys.write_text(
            f"{text}\n" if text else "",
            encoding="utf-8",
        )
        self.host_root_authorized_keys.chmod(0o600)

    def stage_runner_pve_root_access(self) -> None:
        """Передать временному 990 тот же root SSH-доступ, что использует deploy-guest."""
        self.ct_exec("install", "-d", "-m", "0700", str(self.runner_pve_host_dir))
        self.pct(
            "push",
            str(self.ctid),
            str(self.host_pve_root_key),
            str(self.runner_pve_host_dir / "root_ed25519"),
            "--user",
            "0",
            "--group",
            "0",
            "--perms",
            "0600",
        )
        self.pct(
            "push",
            str(self.ctid),
            str(self.host_pve_root_known_hosts),
            str(self.runner_pve_host_dir / "known_hosts"),
            "--user",
            "0",
            "--group",
            "0",
            "--perms",
            "0644",
        )

    def prepare_infra_pve_root_access(self) -> None:
        """Передать постоянный root SSH-доступ внутрь 910 для infra-runtime."""
        self.verify_infra_object()
        self.infra_exec("install", "-d", "-m", "0700", str(self.infra_pve_host_dir))
        self.push_to_infra(
            self.host_pve_root_key,
            self.infra_pve_host_dir / "root_ed25519",
            "0600",
        )
        self.push_to_infra(
            self.host_pve_root_known_hosts,
            self.infra_pve_host_dir / "known_hosts",
            "0644",
        )
        self.infra_exec(
            "chown",
            "1001:0",
            str(self.infra_pve_host_dir),
            str(self.infra_pve_host_dir / "root_ed25519"),
            str(self.infra_pve_host_dir / "known_hosts"),
        )
        self.infra_exec("chmod", "0700", str(self.infra_pve_host_dir))
        self.infra_exec("chmod", "0600", str(self.infra_pve_host_dir / "root_ed25519"))
        self.infra_exec("chmod", "0644", str(self.infra_pve_host_dir / "known_hosts"))
        self.ok("Root SSH-доступ PVE передан в 910")

    def assert_owned_runner(self) -> None:
        # VMID недостаточно для доказательства владения: проверяем также
        # hostname, tags и description, чтобы не затронуть чужой LXC 990.
        if not self.ct_exists():
            self.fail(f"LXC {self.ctid} отсутствует")
        config = self.pct_config(self.ctid)
        if f"hostname: {self.ct_hostname}\n" not in f"{config}\n":
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")
        if "bootstrap-runner" not in next(
            (line for line in config.splitlines() if line.startswith("tags:")), ""
        ):
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")
        if "managed-by=proxmox-bootstrap" not in next(
            (line for line in config.splitlines() if line.startswith("description:")), ""
        ):
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")

    def verify_runner_contract(self) -> None:
        self.assert_owned_runner()
        if self.pct_status(self.ctid) != "running":
            self.fail(f"LXC {self.ctid} должен быть запущен")
        checks = (
            (self.project_dir / ".git", "-d", "закрытый репозиторий"),
            (
                self.project_dir / "scripts/bootstrap-runner/pve-access.sh",
                "-s",
                "pve-access.sh",
            ),
            (
                self.project_dir / "scripts/bootstrap-runner/prepare-runtime.sh",
                "-s",
                "prepare-runtime.sh",
            ),
            (
                self.project_dir / "scripts/bootstrap-runner/deploy-910.sh",
                "-s",
                "deploy-910.sh",
            ),
        )
        for path, test_flag, label in checks:
            result = self.ct_exec("test", test_flag, str(path), check=False)
            if result.returncode:
                self.fail(f"в LXC {self.ctid} отсутствует {label}")

    def pull_helper(self, source: Path, prefix: str) -> Path:
        fd, name = tempfile.mkstemp(prefix=prefix, dir="/run")
        os.close(fd)
        target = Path(name)
        try:
            self.pct("pull", str(self.ctid), str(source), str(target))
            target.chmod(0o700)
        except Exception:
            target.unlink(missing_ok=True)
            raise
        return target

    def run_private_host_access(self, mode: str = "apply") -> None:
        # Политика временного доступа к PVE API хранится в закрытом проекте,
        # поэтому вспомогательный сценарий копируется на PVE только на время выполнения.
        helper = self.pull_helper(
            self.project_dir / "scripts/bootstrap-runner/pve-access.sh",
            "bootstrap-runner-pve-access.",
        )
        try:
            self.run(
                "bash",
                str(helper),
                env={
                    "BOOTSTRAP_RUNNER_MODE": mode,
                    "BOOTSTRAP_RUNNER_CTID": str(self.ctid),
                },
                quiet=True,
            )
        finally:
            helper.unlink(missing_ok=True)

    def prepare_runtime(self) -> None:
        self.log("Подготовка среды bootstrap-runner")
        self.ct_exec(
            "bash",
            str(self.project_dir / "scripts/bootstrap-runner/prepare-runtime.sh"),
            str(self.project_dir),
            quiet=True,
        )
        self.ok("Среда bootstrap-runner готова")

    def deploy_910_phase(self, phase: str, title: str, success: str) -> None:
        self.log(title)

        # Shell здесь остаётся тонкой оболочкой над общим deploy-guest.
        deploy_args = [
            "env", f"INFRA_PROJECT_BRANCH={self.project_branch}",
            "bash", str(self.project_dir / "scripts/bootstrap-runner/deploy-910.sh"),
            phase, str(self.project_dir),
        ]
        self.ct_exec(*deploy_args, quiet=True)
        self.ok(success)

    def infra_config_is_expected(self) -> bool:
        # Для любых разрушительных действий одного hostname недостаточно.
        # Строгая метка в description подтверждает, что 910 создан этим проектом.
        if not self.infra_exists():
            return False
        config = self.pct_config(self.infra_ctid)
        lines = config.splitlines()
        hostname_ok = f"hostname: {self.infra_hostname}" in lines
        unprivileged_ok = "unprivileged: 1" in lines
        description = next(
            (line for line in lines if line.startswith("description: ")),
            "",
        )
        owner_ok = "owner=proxmox-project" in description
        role_ok = "role=infra-manager" in description
        return hostname_ok and unprivileged_ok and owner_ok and role_ok

    def ensure_existing_infra_running(self) -> None:
        if not self.infra_exists():
            return
        if not self.infra_config_is_expected():
            self.fail(f"VMID {self.infra_ctid} занят чужим LXC")
        if self.pct_status(self.infra_ctid) != "running":
            self.pct("start", str(self.infra_ctid))

    def verify_infra_object(self) -> None:
        if not self.infra_exists():
            self.fail(f"LXC {self.infra_ctid} отсутствует")
        if not self.infra_config_is_expected():
            self.fail(
                f"VMID {self.infra_ctid} не имеет строгой метки владения infra-manager"
            )
        if self.pct_status(self.infra_ctid) != "running":
            self.fail(f"LXC {self.infra_ctid} должен быть запущен")

    def infra_test(self, flag: str, path: Path | str) -> bool:
        return self.infra_exec("test", flag, str(path), check=False).returncode == 0

    def prepare_infra_pve_access(self, access_mode: str = "apply") -> None:
        # Постоянные учётные данные создаются на PVE и передаются в 910.
        # Сам секрет никогда не передаётся через аргументы Python-процесса.
        self.verify_infra_object()
        helper = self.pull_helper(
            self.project_dir / "scripts/infra-manager/pve-bootstrap-access.sh",
            "infra-manager-pve-access.",
        )
        try:
            self.run(
                "bash",
                str(helper),
                env={
                    "INFRA_MANAGER_CTID": str(self.infra_ctid),
                    "INFRA_MANAGER_MODE": access_mode,
                    "INFRA_MANAGER_COLOR": "1" if self.color else "0",
                    "INFRA_MANAGER_SECRET_FILE": str(self.infra_staging_secret),
                },
                quiet=True,
            )
        finally:
            helper.unlink(missing_ok=True)

        persistent = Path("/etc/infra-manager/secrets/pve-api.env")
        if not self.infra_test("-s", self.infra_staging_secret) and not self.infra_test(
            "-s", persistent
        ):
            self.fail("PVE API credential не передан в 910")
        self.ok("Постоянный PVE API-доступ 910 подготовлен")

    def push_to_infra(self, source: Path, target: Path, mode: str) -> None:
        """Передать файл в 910 от root с явно заданными правами."""
        push_args = [
            "push", str(self.infra_ctid), str(source), str(target),
            "--user", "0",
            "--group", "0",
            "--perms", mode,
        ]
        self.pct(*push_args)

    def prepare_infra_project_access(self) -> None:
        self.verify_infra_object()
        self.infra_exec("install", "-d", "-m", "0700", "/root/.ssh")

        # В 910 передаётся тот же Deploy Key только для чтения, которым публичная
        # часть получила закрытый проект через временный 990.
        self.push_to_infra(self.host_github_key, self.infra_github_key, "0600")

        fd, tmp_name = tempfile.mkstemp(prefix="infra-manager-known-hosts.", dir="/run")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(f"{GITHUB_ED25519_KNOWN_HOST}\n")
            self.push_to_infra(tmp, self.infra_github_known_hosts, "0644")
        finally:
            tmp.unlink(missing_ok=True)

        config = """Host github.com
    HostName github.com
    User git
    IdentityFile /root/.ssh/github_proxmox_repo_ed25519
    IdentitiesOnly yes
    UserKnownHostsFile /root/.ssh/github_known_hosts
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
"""
        self._write_infra_file(self.infra_github_config, config, "0600")
        self.ok("GitHub-доступ передан в 910")

    def _write_infra_file(self, path: Path, content: str, mode: str) -> None:
        # pct push работает с локальным файлом, поэтому текст сначала
        # записывается во временный файл на PVE.
        fd, tmp_name = tempfile.mkstemp(prefix="infra-manager-file.", dir="/run")
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(content)
            self.push_to_infra(tmp, path, mode)
        finally:
            tmp.unlink(missing_ok=True)

    def checkout_infra_project(self) -> None:
        git_ssh = f"ssh -F {self.infra_github_config}"

        if self.infra_test("-d", self.infra_project_dir / ".git"):
            # Существующую рабочую копию приводим точно к выбранной ветке.
            fetch_args = [
                "env", f"GIT_SSH_COMMAND={git_ssh}",
                "git", "-C", str(self.infra_project_dir),
                "fetch", "--depth", "1", "origin", self.project_branch,
            ]
            self.infra_exec(*fetch_args, quiet=True)
            self.infra_exec(
                "git", "-C", str(self.infra_project_dir),
                "reset", "--hard", "FETCH_HEAD",
                quiet=True,
            )
            self.infra_exec(
                "git", "-C", str(self.infra_project_dir),
                "clean", "-ffdx",
                quiet=True,
            )
        else:
            # На чистом 910 создаём рабочую копию сразу нужной ветки.
            self.infra_exec("rm", "-rf", str(self.infra_project_dir))
            self.infra_exec(
                "install", "-d", "-m", "0755", str(self.infra_project_dir.parent)
            )
            clone_args = [
                "env", f"GIT_SSH_COMMAND={git_ssh}",
                "git", "clone",
                "--depth", "1",
                "--branch", self.project_branch,
                "git@github.com:zsergeyru/proxmox.git",
                str(self.infra_project_dir),
            ]
            self.infra_exec(*clone_args, quiet=True)

        self.ok("Закрытый проект передан в 910")

    def verify_infra_handoff(self) -> None:
        # Перед полной настройкой Ansible требуем весь минимальный набор доверия:
        # учётные данные PVE, CA, Deploy Key и рабочую копию проекта.
        self.verify_infra_object()
        persistent = Path("/etc/infra-manager/secrets/pve-api.env")
        if not self.infra_test("-s", self.infra_staging_secret) and not self.infra_test(
            "-s", persistent
        ):
            self.fail("в 910 отсутствует PVE API credential")
        required = (
            (Path("/usr/local/share/ca-certificates/pve-root-ca.crt"), "PVE CA"),
            (self.infra_pve_host_dir / "root_ed25519", "PVE root SSH key"),
            (self.infra_pve_host_dir / "known_hosts", "PVE SSH known_hosts"),
            (self.infra_github_key, "GitHub Deploy Key"),
        )
        for path, label in required:
            if not self.infra_test("-s", path):
                self.fail(f"в 910 отсутствует {label}")
        if not self.infra_test("-d", self.infra_project_dir / ".git"):
            self.fail("в 910 отсутствует рабочая копия проекта")
        self.ok("Данные для настройки 910 переданы")

    def handoff_infra(self, access_mode: str = "apply") -> None:
        self.prepare_infra_pve_access(access_mode)
        self.prepare_infra_pve_root_access()
        self.prepare_infra_project_access()
        self.checkout_infra_project()
        self.verify_infra_handoff()

    def handoff_existing_infra(self) -> None:
        self.ensure_host_root_ssh_access()
        self.prepare_infra_pve_root_access()
        self.prepare_infra_project_access()
        self.checkout_infra_project()
        if not self.infra_test("-s", "/etc/infra-manager/secrets/pve-api.env"):
            self.fail("в существующем 910 отсутствует постоянный PVE API credential")

    def verify_infra_ready(self) -> None:
        self.verify_infra_object()
        if not self.infra_test("-x", "/usr/local/sbin/infra-manager-status"):
            self.fail("в 910 отсутствует infra-manager-status")
        self.infra_exec(
            "env",
            f"INFRA_PROJECT_BRANCH={self.project_branch}",
            f"INFRA_MANAGER_COLOR={'1' if self.color else '0'}",
            "/usr/local/sbin/infra-manager-status",
            "--full",
        )
        self.ok("910 infra-manager готов по полному контракту")

    def pveum_json(self, *args: str) -> list[dict]:
        result = self.run("pveum", *args, "--output-format", "json", capture=True)
        try:
            value = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise BootstrapError("PVE вернул некорректный JSON") from exc
        return value if isinstance(value, list) else []

    def token_exists(self, user: str, token_name: str) -> bool:
        return any(
            row.get("tokenid") == token_name
            for row in self.pveum_json("user", "token", "list", user)
        )

    def remove_token_acls(self, token_id: str) -> None:
        for row in self.pveum_json("acl", "list"):
            if row.get("type") != "token" or row.get("ugid") != token_id:
                continue
            path = str(row.get("path", ""))
            role = str(row.get("roleid", ""))
            if path and role:
                delete_args = [
                    "pveum", "acl", "delete", path,
                    "--tokens", token_id,
                    "--roles", role,
                ]
                self.run(*delete_args, check=False)

    def remove_named_token(self, user: str, token_name: str) -> None:
        token_id = f"{user}!{token_name}"
        self.remove_token_acls(token_id)
        if self.token_exists(user, token_name):
            self.run("pveum", "user", "token", "remove", user, token_name)

    def temporary_token_exists(self) -> bool:
        return self.token_exists("root@pam", "bootstrap-runner")

    def remove_private_access(self) -> None:
        # Если 990 сохранился, временный token удаляется тем же закрытым сценарием,
        # который его создавал. При отсутствии 990 используется прямое удаление.
        source = self.project_dir / "scripts/bootstrap-runner/pve-access.sh"
        if self.ct_exists():
            try:
                self.assert_owned_runner()
            except BootstrapError:
                self.remove_named_token("root@pam", "bootstrap-runner")
            else:
                if self.ct_exec("test", "-f", str(source), check=False).returncode == 0:
                    self.run_private_host_access("remove")
                else:
                    self.remove_named_token("root@pam", "bootstrap-runner")
        else:
            self.remove_named_token("root@pam", "bootstrap-runner")

        if self.temporary_token_exists():
            self.fail("временный PVE API token 990 не удалён")

    def remove_downloaded_template(self) -> None:
        if not self.host_template_marker.is_file():
            return
        ref = self.host_template_marker.read_text().strip()
        if ref and self.run("pvesm", "path", ref, check=False, capture=True).returncode == 0:
            path_result = self.run("pvesm", "path", ref, capture=True)
            actual_path = Path(path_result.stdout.strip())
            if self.run("pvesm", "free", ref, check=False).returncode != 0:
                actual_path.unlink(missing_ok=True)
            self.info(f"Удалён временно скачанный LXC-шаблон: {ref.split(':', 1)[-1]}")
        self.host_template_marker.unlink(missing_ok=True)

    def finalize_runner(self) -> None:
        # Успешный bootstrap не должен оставлять состояние, секреты, token или 990.
        self.assert_owned_runner()
        self.ct_exec(
            "rm",
            "-rf",
            "/etc/bootstrap-runner/secrets",
            "/var/lib/bootstrap-runner/opentofu/state",
        )
        if self.ct_exec("test", "!", "-e", "/etc/bootstrap-runner/secrets", check=False).returncode:
            self.fail("временные секреты 990 не удалены")
        if self.ct_exec(
            "test", "!", "-e", "/var/lib/bootstrap-runner/opentofu/state", check=False
        ).returncode:
            self.fail("временное состояние 990 не удалено")

        self.remove_private_access()
        if self.pct_status(self.ctid) == "running":
            self.pct("stop", str(self.ctid))
        self.run("pct", "destroy", str(self.ctid), "--purge", "1", quiet=True)
        if self.ct_exists():
            self.fail("LXC 990 не удалён")
        self.remove_downloaded_template()
        self.ok("Временный контур 990 полностью удалён")

    def remove_runner_if_present(self) -> None:
        if self.ct_exists():
            self.assert_owned_runner()
            self.remove_private_access()
            if self.pct_status(self.ctid) == "running":
                self.pct("stop", str(self.ctid))
            self.run("pct", "destroy", str(self.ctid), "--purge", "1", quiet=True)
        else:
            self.remove_named_token("root@pam", "bootstrap-runner")
        self.remove_downloaded_template()

    def remove_infra(self) -> None:
        # Сначала убираем временный контур. Сам 910 удаляем только после
        # строгой проверки метки владения.
        self.remove_runner_if_present()
        if self.infra_exists():
            if not self.infra_config_is_expected():
                self.fail(f"VMID {self.infra_ctid} занят чужим объектом")
            self.remove_named_token("root@pam", "infra-manager")
            self.pct("set", str(self.infra_ctid), "--protection", "0", quiet=True)
            if self.pct_status(self.infra_ctid) == "running":
                self.pct("stop", str(self.infra_ctid))
            self.run(
                "pct", "destroy", str(self.infra_ctid), "--purge", "1", quiet=True
            )
            self.ok("LXC 910 удалён")
        else:
            self.remove_named_token("root@pam", "infra-manager")
            self.ok("LXC 910 уже отсутствует")

        self.remove_host_root_ssh_authorization()
        self.ok("Root SSH-доступ infra-manager к PVE удалён")

    def check_ready(self) -> None:
        if not self.infra_exists():
            self.fail("LXC 910 отсутствует")
        self.verify_infra_ready()
        if self.ct_exists():
            self.fail("после успешного bootstrap временный LXC 990 не должен существовать")
        if self.temporary_token_exists():
            self.fail("после успешного bootstrap временный token 990 не должен существовать")
        self.ok("Постоянный 910 готов, временный контур отсутствует")

    def runner_owns_910(self) -> bool:
        # Наличие состояния OpenTofu означает незавершённую первоначальную установку:
        # новый 990 не должен импортировать или заново присваивать себе 910.
        return (
            self.ct_exec(
                "test",
                "-s",
                "/var/lib/bootstrap-runner/opentofu/state/proxmox.tfstate",
                check=False,
            ).returncode
            == 0
        )

    def prepare_runner(self) -> None:
        self.verify_runner_contract()
        self.ensure_host_root_ssh_access()
        self.stage_runner_pve_root_access()
        self.run_private_host_access()
        self.prepare_runtime()

    def apply(self) -> None:
        existed = self.infra_exists()
        self.prepare_runner()
        owns_910 = self.runner_owns_910()

        # Три пути намеренно разделены:
        # 1) продолжение оборванной первоначальной установки;
        # 2) обновление уже постоянного 910 без временного состояния;
        # 3) чистое создание нового 910.
        if existed and owns_910:
            self.info(
                "Найден незавершённый первоначальный контур; продолжается его состояние"
            )
            self.ensure_existing_infra_running()
            self.deploy_910_phase(
                "infrastructure",
                "Продолжение создания LXC 910 через OpenTofu",
                "LXC 910 приведён к состоянию bootstrap-runner",
            )
            self.deploy_910_phase(
                "base",
                "Базовая настройка LXC 910 через Ansible",
                "Базовая настройка 910 завершена",
            )
            self.handoff_infra("recover" if self.mode == "recover" else "apply")
            self.deploy_910_phase(
                "provision",
                "Полная настройка LXC 910 через Ansible",
                "Полная настройка 910 завершена",
            )
        elif existed:
            self.ensure_existing_infra_running()
            if self.mode == "recover":
                self.prepare_infra_pve_access("recover")
            self.handoff_existing_infra()
            self.deploy_910_phase(
                "existing",
                "Обновление существующего LXC 910",
                "Существующий 910 обновлён",
            )
        else:
            self.deploy_910_phase(
                "infrastructure",
                "Создание LXC 910 через OpenTofu",
                "LXC 910 создан через состояние bootstrap-runner",
            )
            self.deploy_910_phase(
                "base",
                "Базовая настройка LXC 910 через Ansible",
                "Базовая настройка 910 завершена",
            )
            self.handoff_infra("recover" if self.mode == "recover" else "apply")
            self.deploy_910_phase(
                "provision",
                "Полная настройка LXC 910 через Ansible",
                "Полная настройка 910 завершена",
            )

        self.verify_infra_ready()
        self.finalize_runner()
        self.check_ready()

    def execute(self) -> None:
        # Любой режим приходит сюда уже после подготовки временного 990
        # публичным bootstrap-pve.py.
        self.require_host()
        self.verify_runner_contract()
        self.info(f"Закрытый bootstrap {VERSION}, режим: {self.mode}")

        if self.mode in {"apply", "recover"}:
            self.apply()
        elif self.mode == "check":
            self.verify_infra_ready()
            self.finalize_runner()
            self.check_ready()
        elif self.mode == "remove":
            self.remove_infra()
        elif self.mode == "purge":
            self.remove_infra()
            shutil.rmtree(self.host_bootstrap_dir, ignore_errors=True)
            self.ok("Постоянный GitHub Deploy Key удалён")
        else:
            self.fail(f"неизвестный режим: {self.mode}")


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
