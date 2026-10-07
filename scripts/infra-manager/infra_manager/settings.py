"""Единые неизменяемые настройки и пути infra-manager."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .guest_catalog import find_guest_by_role


INFRA_MANAGER_ROLE = "infra-manager"


def _default_repo_root() -> Path:
    """Найти рабочую копию проекта для исходного и установленного кода."""

    source_root = Path(__file__).resolve().parents[3]
    if (source_root / "infrastructure" / "guests").is_dir():
        return source_root
    return Path("/var/lib/infra-manager/bootstrap-repo")


@dataclass(frozen=True)
class Paths:
    """Пути файлов и каталогов infra-manager."""

    repo_root: Path = field(default_factory=_default_repo_root)
    config_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("INFRA_MANAGER_CONFIG_DIR", "/etc/infra-manager")
        )
    )
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("INFRA_MANAGER_DATA_DIR", "/var/lib/infra-manager")
        )
    )
    compose_dir: Path = Path("/opt/infra-manager/compose")
    python_install_root: Path = Path("/usr/local/lib/infra-manager")

    @property
    def asset_dir(self) -> Path:
        identity = find_guest_by_role(
            self.repo_root,
            INFRA_MANAGER_ROLE,
        )
        return identity.directory / "rootfs/opt/infra-manager/compose"

    @property
    def runtime_secret_dir(self) -> Path:
        return Path("/run/infra-manager/secrets")

    @property
    def openbao_materialized_marker(self) -> Path:
        return self.runtime_secret_dir / ".openbao-materialized"

    @property
    def ca_dir(self) -> Path:
        return self.config_dir / "ca"

    @property
    def ca_bundle(self) -> Path:
        return self.ca_dir / "ca-bundle.crt"

    @property
    def ssh_client_ca_public_key(self) -> Path:
        return self.ca_dir / "ssh-client-ca.pub"

    @property
    def ssh_host_ca_public_key(self) -> Path:
        return self.ca_dir / "ssh-host-ca.pub"

    @property
    def openbao_tls_dir(self) -> Path:
        return self.config_dir / "openbao" / "tls"

    @property
    def openbao_tls_ca(self) -> Path:
        return self.openbao_tls_dir / "ca.crt"

    @property
    def pve_host_dir(self) -> Path:
        return Path(
            os.environ.get(
                "INFRA_PVE_HOST_DIR",
                "/mnt/pve-access/pve-host",
            )
        )

    @property
    def pve_host_private_key(self) -> Path:
        return self.pve_host_dir / "root_ed25519"

    @property
    def pve_host_known_hosts(self) -> Path:
        return self.pve_host_dir / "known_hosts"

    @property
    def semaphore_dir(self) -> Path:
        return self.data_dir / "semaphore"

    @property
    def opentofu_dir(self) -> Path:
        return self.data_dir / "opentofu"

    @property
    def opentofu_state_dir(self) -> Path:
        return self.opentofu_dir / "state"

    @property
    def opentofu_input(self) -> Path:
        return self.opentofu_dir / "guests.json"

    @property
    def status_file(self) -> Path:
        return self.config_dir / "status.yaml"

    @property
    def server_env(self) -> Path:
        return self.runtime_secret_dir / "semaphore-server.env"

    @property
    def pve_api_env(self) -> Path:
        return self.runtime_secret_dir / "pve-api.env"

    @property
    def admin_password_file(self) -> Path:
        return self.runtime_secret_dir / "initial-admin-password"

    @property
    def semaphore_api_token_file(self) -> Path:
        return self.runtime_secret_dir / "semaphore-api-token"

    @property
    def github_key(self) -> Path:
        return self.runtime_secret_dir / "github_proxmox_repo_ed25519"

    @property
    def github_key_copy(self) -> Path:
        return self.github_key

    @property
    def semaphore_project_id_file(self) -> Path:
        return self.data_dir / "semaphore-project-id"

    @property
    def status_command(self) -> Path:
        return Path("/usr/local/sbin/infra-manager-status")


@dataclass(frozen=True)
class Settings:
    """Имена, версии и значения по умолчанию infra-manager."""

    default_project_branch: str = "main"
    semaphore_url: str = "http://127.0.0.1:3000"
    portal_gateway_port: int = 3002
    project_name: str = "Proxmox Infrastructure"
    project_repo: str = "git@github.com:zsergeyru/proxmox.git"
    opentofu_env_name: str = "OpenTofu PVE"
    infra_manager_env_name: str = "Infra Manager"
    managed_pool: str = "managed"
    semaphore_version: str = "v2.18.30"
    runtime_version: str = "v1"
    opentofu_version: str = "1.12.6"
    packer_version: str = "1.15.4"
    infra_manager_role: str = INFRA_MANAGER_ROLE

    def project_branch(self) -> str:
        """Вернуть выбранную Git-ветку проекта."""
        return os.environ.get(
            "INFRA_PROJECT_BRANCH",
            self.default_project_branch,
        )


PATHS = Paths()
SETTINGS = Settings()
