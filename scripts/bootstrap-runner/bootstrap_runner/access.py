"""Первоначальные SSH-доступы и временные полномочия PVE для 990.

Проверяет принадлежность временного контейнера, подготавливает SSH-доступ
к физическому узлу и выдаёт кратковременный токен PVE без разделения прав.
Секрет передаётся в защищённый каталог 990, а не в постоянное состояние.
Выданный токен необходимо отзывать при завершении и контролируемом отказе.
Обычные права пользователей и повседневный SSH-доступ этот модуль не ведёт.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .errors import BootstrapError


class BootstrapAccessMixin:
    """Операции начальных доступов для общего координатора."""
    def _write_host_authorized_keys(self, lines: list[str]) -> None:
        """Атомарно заменить root authorized_keys, сохранив остальные записи.

        Сначала подготавливается закрытый файл рядом с действующим, затем
        он переименовывается одной операцией. Символьные ссылки запрещены.
        """
        target = self.host_root_authorized_keys
        if target.is_symlink() or target.parent.is_symlink():
            self.fail(f"Небезопасный SSH-файл PVE: {target}")
        fd, filename = tempfile.mkstemp(prefix=".authorized_keys.", dir=target.parent)
        temporary = Path(filename)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write("\n".join(lines) + ("\n" if lines else ""))
                file.flush()
                os.fsync(file.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def ensure_host_root_ssh_access(self) -> None:
        """Подготовить отдельный root SSH-ключ для управляющего контура."""
        self.host_bootstrap_dir.mkdir(parents=True, exist_ok=True)
        self.host_access_pve_host_dir.mkdir(parents=True, exist_ok=True)
        self._set_mode_owner(
            self.host_access_pve_host_dir,
            0o700,
            101001,
            100000,
        )
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
        if root_ssh_dir.is_symlink() or self.host_root_authorized_keys.is_symlink():
            self.fail("Символьная ссылка в root SSH-доступе PVE запрещена")
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
        self._write_host_authorized_keys(lines)

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
        self._set_mode_owner(self.host_pve_root_key, 0o600, 101001, 100000)
        self._set_mode_owner(public_key, 0o644, 101001, 100000)
        self._set_mode_owner(
            self.host_pve_root_known_hosts,
            0o644,
            101001,
            100000,
        )
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
        self._write_host_authorized_keys(lines)

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
        """Проверить постоянный root SSH-доступ из read-only каталога PVE."""
        self.verify_infra_object()
        required = (
            (self.host_pve_root_key, self.infra_pve_host_dir / "root_ed25519"),
            (
                self.host_pve_root_known_hosts,
                self.infra_pve_host_dir / "known_hosts",
            ),
        )
        for host_path, guest_path in required:
            if not host_path.is_file() or host_path.stat().st_size == 0:
                self.fail(f"на PVE отсутствует постоянный файл доступа: {host_path}")
            if not self.infra_test("-s", guest_path):
                self.fail(f"{self.infra_ctid} не видит постоянный файл доступа: {guest_path}")
        self.ok(f"Root SSH-доступ PVE доступен в {self.infra_ctid} только для чтения")

    def assert_owned_runner(self) -> None:
        """Не допустить действий над чужим LXC: сверить имя, теги и единственную метку владельца."""
        # VMID недостаточно для доказательства владения: проверяем также
        # hostname, tags и description, чтобы не затронуть чужой LXC 990.
        if not self.ct_exists():
            self.fail(f"LXC {self.ctid} отсутствует")
        config = self.pct_config(self.ctid)
        if f"hostname: {self.ct_hostname}\n" not in f"{config}\n":
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")
        tags_line = next(
            (line for line in config.splitlines() if line.startswith("tags: ")), ""
        )
        tags = set(tags_line.removeprefix("tags: ").split(";"))
        if "bootstrap-runner" not in tags:
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")
        description = next(
            (line.removeprefix("description: ") for line in config.splitlines()
             if line.startswith("description: ")), ""
        )
        # Разбор всех marker=value не позволяет принять чужой суффикс
        # bootstrap или подменить владельца вторым значением managed-by.
        markers = re.findall(r"(?<![A-Za-z0-9_-])([A-Za-z_-]+)=([A-Za-z0-9_-]+)", description)
        owners = [value for key, value in markers if key == "managed-by"]
        if owners != ["proxmox-bootstrap"]:
            self.fail(f"LXC {self.ctid} не принадлежит bootstrap")
        # Не уничтожать 990 с вручную добавленными томами/устройствами.
        for line in config.splitlines():
            option = line.split(":", 1)[0]
            if re.fullmatch(r"(?:mp|unused|dev)\d+", option) or option == "hookscript":
                self.fail(
                    f"LXC {self.ctid} содержит постороннее подключение {option}; "
                    "автоматическое удаление запрещено"
                )

    def verify_runner_contract(self) -> None:
        """Проверить работающий 990, принадлежность проекту и обязательные файлы проекта."""
        self.assert_owned_runner()
        if self.pct_status(self.ctid) != "running":
            self.fail(f"LXC {self.ctid} должен быть запущен")
        checks = (
            (self.project_dir / ".git", "-d", "закрытый репозиторий"),
            (
                self.project_dir / "scripts/bootstrap-runner/prepare-runtime.sh",
                "-s",
                "prepare-runtime.sh",
            ),
            (
                self.project_dir / "scripts/bootstrap-runner/run-runtime.sh",
                "-s",
                "run-runtime.sh",
            ),
        )
        for path, test_flag, label in checks:
            result = self.ct_exec("test", test_flag, str(path), check=False)
            if result.returncode:
                self.fail(f"в LXC {self.ctid} отсутствует {label}")

    def pve_node_address(self) -> tuple[str, str]:
        """Вернуть имя PVE-узла и его IPv4 для управляющих контейнеров."""
        node = self.run("hostname", "-s", capture=True).stdout.strip()
        addresses = self.run(
            "getent",
            "ahostsv4",
            node,
            capture=True,
        ).stdout.splitlines()
        if not node or not addresses:
            self.fail("не удалось определить имя или IPv4 PVE-узла")
        return node, addresses[0].split()[0]

    def create_full_pve_token(self, token_name: str) -> tuple[str, str]:
        """Создать токен root@pam с полными полномочиями и вернуть его секрет.

        Токен позволяет действия уровня администратора PVE. При ошибочном
        ответе PVE токен удаляется; вызывающий код обязан удалить его после
        использования и не выводить секрет в журнал.
        """
        token_id = f"root@pam!{token_name}"
        created = self.run(
            "pveum",
            "user",
            "token",
            "add",
            "root@pam",
            token_name,
            "--privsep",
            "0",
            "--output-format",
            "json",
            capture=True,
        )
        try:
            payload = json.loads(created.stdout or "{}")
        except json.JSONDecodeError as exc:
            self.remove_named_token("root@pam", token_name)
            raise BootstrapError(
                f"PVE вернул некорректный JSON при создании {token_id}"
            ) from exc

        secret = str(payload.get("value") or "")
        if not secret:
            self.remove_named_token("root@pam", token_name)
            self.fail(f"PVE создал {token_id} без доступного secret")
        return token_id, secret

    def prepare_runner_pve_access(self) -> None:
        """Перевыпустить полный временный токен и передать его внутрь 990.

        Сначала отзывается предыдущий токен с тем же именем; новый секрет
        передаётся через временный файл с режимом 0600. При сбое передачи токен
        отзывается, после успешного завершения его удаляет общий этап очистки.
        """
        token_name = "bootstrap-runner"
        self.remove_named_token("root@pam", token_name)
        token_id, secret = self.create_full_pve_token(token_name)
        node, host_ip = self.pve_node_address()

        secret_dir = Path("/etc/bootstrap-runner/secrets")
        ca_dir = Path("/etc/bootstrap-runner/ca")
        self.ct_exec("install", "-d", "-m", "0700", str(secret_dir))
        self.ct_exec("install", "-d", "-m", "0755", str(ca_dir))

        fd, tmp_name = tempfile.mkstemp(
            prefix="bootstrap-runner-pve-api.",
            dir="/run",
        )
        os.close(fd)
        tmp = Path(tmp_name)
        try:
            tmp.write_text(
                f"PVE_API_URL=https://{node}:8006\n"
                f"PVE_API_TOKEN_ID={token_id}\n"
                f"PVE_API_TOKEN_SECRET={secret}\n"
                f"TF_VAR_pve_endpoint=https://{node}:8006\n"
                f"TF_VAR_pve_api_token={token_id}={secret}\n",
                encoding="utf-8",
            )
            tmp.chmod(0o600)
            self.pct(
                "push",
                str(self.ctid),
                str(tmp),
                str(secret_dir / "pve-api.env"),
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
                "/etc/pve/pve-root-ca.pem",
                str(ca_dir / "ca-bundle.crt"),
                "--user",
                "0",
                "--group",
                "0",
                "--perms",
                "0644",
            )
        except Exception:
            self.remove_named_token("root@pam", token_name)
            raise
        finally:
            tmp.unlink(missing_ok=True)

        hosts_script = (
            'node=$1; ip=$2; '
            'grep -vE "[[:space:]]${node}([[:space:]]|$)" /etc/hosts '
            '> /etc/hosts.bootstrap || true; '
            'printf "%s %s\\n" "$ip" "$node" >> /etc/hosts.bootstrap; '
            'cat /etc/hosts.bootstrap > /etc/hosts; '
            'rm -f /etc/hosts.bootstrap'
        )
        self.ct_exec(
            "sh",
            "-c",
            hosts_script,
            "sh",
            node,
            host_ip,
        )
        self.ok("Временный полный PVE API-доступ 990 подготовлен")

