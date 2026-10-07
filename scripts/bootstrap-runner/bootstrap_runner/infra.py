"""Часть оркестрации bootstrap-runner."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .constants import GITHUB_ED25519_KNOWN_HOST


class BootstrapInfraMixin:
    def prepare_runtime(self) -> None:
        self.log("Подготовка среды bootstrap-runner")
        self.ct_exec(
            "bash",
            str(self.project_dir / "scripts/bootstrap-runner/prepare-runtime.sh"),
            str(self.project_dir),
            quiet=True,
        )
        self.ok("Среда bootstrap-runner готова")

    def remove_bootstrap_ssh_access_from_infra(self) -> None:
        """Удалить из infra-manager временный SSH-ключ первоначального контура."""
        if not self.infra_exists():
            return
        if not self.infra_config_is_expected():
            self.fail(
                f"VMID {self.infra_ctid} не имеет строгой метки владения infra-manager"
            )
        marker = f" {self.infra_bootstrap_key_comment}"
        script = (
            "set -eu; "
            "marker=$1; "
            "file=/root/.ssh/authorized_keys; "
            "[ -e \"$file\" ] || exit 0; "
            "tmp=/root/.ssh/authorized_keys.bootstrap-runner; "
            "grep -vF \"$marker\" \"$file\" > \"$tmp\" || true; "
            "chmod 0600 \"$tmp\"; "
            "mv \"$tmp\" \"$file\""
        )
        self.infra_exec("sh", "-c", script, "sh", marker)
        self.ok(f"Временный SSH-доступ 990 к {self.infra_ctid} удалён")

    def deploy_infra_phase(self, phase: str, title: str, success: str) -> None:
        self.log(title)

        # Временный 990 использует отдельную внутреннюю bootstrap-точку входа.
        # Публичный deploy-guest не содержит bootstrap-фаз 910.
        deploy_args = [
            "env", f"INFRA_PROJECT_BRANCH={self.project_branch}",
            "bash", str(self.project_dir / "scripts/bootstrap-runner/deploy-infra-manager.sh"),
            phase, str(self.infra_ctid), str(self.project_dir),
        ]
        self.ct_exec(
            *deploy_args,
            quiet=True,
            progress=phase in {"base", "provision", "existing"},
        )
        self.ok(success)

    def infra_config_is_expected(self) -> bool:
        # Для любых разрушительных действий одного hostname недостаточно.
        # Строгая метка в description подтверждает владение infra-manager проектом.
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

    def stage_infra_bootstrap_file(
        self,
        source: Path,
        target: Path,
    ) -> None:
        """Передать secret в tmpfs infra-manager только на время bootstrap."""
        if not source.is_file() or source.stat().st_size == 0:
            self.fail(f"отсутствует bootstrap secret: {source}")
        self.infra_exec(
            "install",
            "-d",
            "-m",
            "0700",
            str(self.infra_bootstrap_secret_dir),
        )
        self.pct(
            "push",
            str(self.infra_ctid),
            str(source),
            str(target),
            "--user",
            "0",
            "--group",
            "0",
            "--perms",
            "0600",
        )

    def prepare_infra_pve_access(self, access_mode: str = "apply") -> None:
        """Проверить PVE token и временно передать его infra-manager при необходимости."""
        self.verify_infra_object()
        token_name = "infra-manager"

        rows = self.pveum_json("user", "token", "list", "root@pam")
        token_row = next(
            (row for row in rows if row.get("tokenid") == token_name),
            None,
        )
        old_privsep = (
            token_row is not None
            and token_row.get("privsep") not in (0, False, "0")
        )

        if token_row is not None and (access_mode == "recover" or old_privsep):
            self.remove_named_token("root@pam", token_name)
            token_row = None

        if token_row is None:
            token_id, secret = self.create_full_pve_token(token_name)
            node, _ = self.pve_node_address()
            fd, tmp_name = tempfile.mkstemp(
                prefix="infra-manager-pve-api.",
                dir="/run",
            )
            os.close(fd)
            tmp = Path(tmp_name)
            try:
                tmp.write_text(
                    f"PVE_API_URL=https://{node}:8006\n"
                    f"PVE_API_TOKEN_ID={token_id}\n"
                    f"PVE_API_TOKEN_SECRET={secret}\n",
                    encoding="utf-8",
                )
                tmp.chmod(0o600)
                self.stage_infra_bootstrap_file(
                    tmp,
                    self.infra_pve_api_env,
                )
            finally:
                tmp.unlink(missing_ok=True)

        pools = self.pveum_json("pool", "list")
        if not any(row.get("poolid") == "managed" for row in pools):
            self.run(
                "pveum",
                "pool",
                "add",
                "managed",
                "--comment",
                f"Guests managed from {self.infra_ctid} infra-manager",
            )

        ca_source = Path("/etc/pve/pve-root-ca.pem")
        self._copy_access_file(
            ca_source,
            self.host_pve_ca,
            mode=0o644,
            uid=100000,
            gid=100000,
        )
        if not self.infra_test("-s", self.infra_pve_ca):
            self.fail(f"{self.infra_ctid} не видит PVE CA из постоянного каталога PVE")

        node, host_ip = self.pve_node_address()
        hosts_script = (
            'node=$1; ip=$2; '
            'grep -vE "[[:space:]]${node}([[:space:]]|$)" /etc/hosts '
            '> /etc/hosts.bootstrap || true; '
            'printf "%s %s\\n" "$ip" "$node" >> /etc/hosts.bootstrap; '
            'cat /etc/hosts.bootstrap > /etc/hosts; '
            'rm -f /etc/hosts.bootstrap'
        )
        self.infra_exec(
            "sh",
            "-c",
            hosts_script,
            "sh",
            node,
            host_ip,
        )

        self.ok(f"PVE API-доступ {self.infra_ctid} подготовлен без постоянной файловой копии")

    def push_to_infra(self, source: Path, target: Path, mode: str) -> None:
        """Передать файл в infra-manager от root с явно заданными правами."""
        push_args = [
            "push", str(self.infra_ctid), str(source), str(target),
            "--user", "0",
            "--group", "0",
            "--perms", mode,
        ]
        self.pct(*push_args)

    def prepare_infra_project_access(self) -> None:
        """Временно передать recovery Git key для checkout проекта."""
        self.verify_infra_object()
        self.stage_infra_bootstrap_file(
            self.host_recovery_github_key,
            self.infra_github_key,
        )

        self.infra_exec("install", "-d", "-m", "0700", "/root/.ssh")

        fd, tmp_name = tempfile.mkstemp(
            prefix="infra-manager-known-hosts.",
            dir="/run",
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(f"{GITHUB_ED25519_KNOWN_HOST}\n")
            self.push_to_infra(tmp, self.infra_github_known_hosts, "0644")
        finally:
            tmp.unlink(missing_ok=True)

        config = f"""Host github.com
    HostName github.com
    User git
    IdentityFile {self.infra_github_key}
    IdentitiesOnly yes
    UserKnownHostsFile {self.infra_github_known_hosts}
    StrictHostKeyChecking yes
    BatchMode yes
    ConnectTimeout 10
"""
        self._write_infra_file(self.infra_github_config, config, "0600")
        self.ok(f"Временный GitHub-доступ {self.infra_ctid} подготовлен из PVE-only recovery")

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
            # На чистом infra-manager создаём рабочую копию сразу нужной ветки.
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

        self.ok(f"Закрытый проект передан в {self.infra_ctid}")

    def verify_infra_handoff(self) -> None:
        # Перед полной настройкой Ansible требуем весь минимальный набор доверия:
        # учётные данные PVE, CA, Deploy Key и рабочую копию проекта.
        self.verify_infra_object()
        pve_credential_ready = (
            self.infra_test("-s", self.infra_runtime_pve_api_env)
            or self.infra_test("-s", self.infra_pve_api_env)
        )
        if not pve_credential_ready:
            self.fail(
                f"в {self.infra_ctid} отсутствует PVE API credential из OpenBao "
                "или временного bootstrap"
            )
        required = (
            (self.infra_pve_ca, "PVE CA"),
            (self.infra_pve_host_dir / "root_ed25519", "PVE root SSH key"),
            (self.infra_pve_host_dir / "known_hosts", "PVE SSH known_hosts"),
            (self.infra_github_key, "GitHub Deploy Key"),
        )
        for path, label in required:
            if not self.infra_test("-s", path):
                self.fail(f"в {self.infra_ctid} отсутствует {label}")
        if not self.infra_test("-d", self.infra_project_dir / ".git"):
            self.fail(f"в {self.infra_ctid} отсутствует рабочая копия проекта")
        self.ok(f"Данные для настройки {self.infra_ctid} переданы")

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
        if not (
            self.infra_test("-s", self.infra_runtime_pve_api_env)
            or self.infra_test("-s", self.infra_pve_api_env)
        ):
            self.fail(
                f"в существующем {self.infra_ctid} отсутствует рабочий или bootstrap "
                "PVE API credential"
            )

    def initialize_infra_openbao(self) -> None:
        """Завершить bootstrap переносом runtime secrets в OpenBao."""
        node, _ = self.pve_node_address()
        job = (
            self.infra_project_dir
            / "scripts"
            / "infra-manager"
            / "jobs"
            / "initialize-openbao.py"
        )
        if not self.infra_test("-s", job):
            self.fail(f"в {self.infra_ctid} отсутствует задача инициализации OpenBao: {job}")

        self.infra_exec(
            "env",
            f"TF_VAR_pve_endpoint=https://{node}:8006",
            f"INFRA_PROJECT_BRANCH={self.project_branch}",
            "python3",
            str(job),
            quiet=True,
        )
        self.infra_exec(
            "rm",
            "-rf",
            str(self.infra_bootstrap_secret_dir),
        )
        if self.infra_test("-e", self.infra_bootstrap_secret_dir):
            self.fail(f"временные bootstrap secrets {self.infra_ctid} не удалены")
        self.ok("Рабочие secrets перенесены в OpenBao; bootstrap-копии удалены")

    def verify_infra_ready(self, *, quiet: bool = False) -> None:
        self.verify_infra_object()
        if not self.infra_test("-x", "/usr/local/sbin/infra-manager-status"):
            self.fail(f"в {self.infra_ctid} отсутствует infra-manager-status")

        args = [
            "env",
            f"INFRA_PROJECT_BRANCH={self.project_branch}",
            f"INFRA_MANAGER_COLOR={'1' if self.color else '0'}",
            "/usr/local/sbin/infra-manager-status",
            "--full",
        ]
        if quiet:
            args.append("--quiet")
        self.infra_exec(*args, quiet=quiet)

