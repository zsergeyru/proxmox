"""Общие пути закрытого первоначального загрузчика на физическом PVE.

Назначение:
- собрать каталоги и имена файлов PVE, временного 990 и управляющего гостя;
- сохранить прежние открытые атрибуты BootstrapHost для совместимости.

Порядок: setup_host_paths() вызывается до поиска управляющей роли;
setup_infra_paths() — после определения её номера. Изменение каталогов
не должно переносить постоянное состояние внутрь воспроизводимого rootfs.
"""

from __future__ import annotations

import os
from pathlib import Path


class BootstrapPathsMixin:
    """Группы путей для первоначального загрузчика."""

    def setup_host_paths(self) -> None:
        """Настроить пути физического PVE, доступа и постоянных данных."""
        self.host_lock_file = Path("/run/proxmox-bootstrap-runner.lock")
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


    def setup_infra_paths(self) -> None:
        """Настроить внутренние пути временного и управляющего гостей."""
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
