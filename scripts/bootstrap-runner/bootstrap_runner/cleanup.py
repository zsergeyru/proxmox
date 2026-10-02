"""Часть оркестрации bootstrap-runner."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .errors import BootstrapError

class BootstrapCleanupMixin:
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
        """Удалить временный API token 990."""
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
        self.remove_runner_ssh_access_from_infra()
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

    def remove_openbao_host_support(self) -> None:
        """Убрать хостовый сценарий OpenBao, сохранив unseal-ключ."""
        self.run(
            "systemctl",
            "disable",
            "--now",
            "infra-manager-openbao-unseal.timer",
            "infra-manager-openbao-unseal.service",
            check=False,
            quiet=True,
        )
        self.host_openbao_unseal_timer.unlink(missing_ok=True)
        self.host_openbao_unseal_service.unlink(missing_ok=True)
        self.host_openbao_unseal_command.unlink(missing_ok=True)
        self.host_openbao_config.unlink(missing_ok=True)
        self.run("systemctl", "daemon-reload", check=False, quiet=True)
        self.ok("Сценарий разблокировки OpenBao удалён; ключ на PVE сохранён")

    def remove_infra(self) -> None:
        # Сначала убираем временный контур. Сам infra-manager удаляем только после
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
            self.ok(f"LXC {self.infra_ctid} удалён")
        else:
            self.remove_named_token("root@pam", "infra-manager")
            self.ok(f"LXC {self.infra_ctid} уже отсутствует")

        self.remove_openbao_host_support()
        self.remove_host_root_ssh_authorization()
        self.ok("Root SSH-доступ infra-manager к PVE удалён")

