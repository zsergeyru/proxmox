"""Очистка временных ресурсов и удаление управляемых контейнеров.

Удаляет временный токен и объект 990 только после проверки его меток
владения. В режимах remove/recover может разрушить rootfs управляющего
контейнера, предварительно проверяя его принадлежность проекту.
Постоянные каталоги PVE намеренно не удаляются; вызов не заменяет
резервное копирование и восстановление сохранённых данных.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .errors import BootstrapError


class BootstrapCleanupMixin:
    """Операции очистки для общего координатора."""
    def pveum_json(self, *args: str) -> list[dict]:
        """Получить список объектов PVE, преобразовав JSON; некорректный ответ останавливает очистку."""
        result = self.run("pveum", *args, "--output-format", "json", capture=True)
        try:
            value = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise BootstrapError("PVE вернул некорректный JSON") from exc
        return value if isinstance(value, list) else []

    def token_exists(self, user: str, token_name: str) -> bool:
        """Проверить наличие токена данного пользователя по точному имени."""
        return any(
            row.get("tokenid") == token_name
            for row in self.pveum_json("user", "token", "list", user)
        )

    def remove_token_acls(self, token_id: str) -> None:
        """Попытаться снять назначения прав токена; ошибки отдельных удалений не прерывают обход."""
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
        """Снять назначения прав и удалить выбранный токен, если он существует."""
        token_id = f"{user}!{token_name}"
        self.remove_token_acls(token_id)
        if self.token_exists(user, token_name):
            self.run("pveum", "user", "token", "remove", user, token_name)

    def temporary_token_exists(self) -> bool:
        """Проверить, оставлен ли полный временный токен первоначальной установки."""
        return self.token_exists("root@pam", "bootstrap-runner")

    def remove_private_access(self) -> None:
        """Удалить временный API token 990."""
        self.remove_named_token("root@pam", "bootstrap-runner")
        if self.temporary_token_exists():
            self.fail("временный PVE API token 990 не удалён")
    def remove_downloaded_template(self) -> None:
        """Освободить шаблон, записанный в маркере, и удалить маркер после обработки."""
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
        """Удалить временные доступы, токен и LXC после успешной проверки.
        
        До разрушительных действий подтверждается принадлежность 990. Отдельно
        удаляются начальные разрешения в управляющем госте и временное состояние
        OpenTofu. Постоянные каталоги PVE не затрагиваются; сбой завершения
        останавливает итоговую проверку и должен разбираться оператором.
        """
        # Успешный bootstrap не должен оставлять состояние, секреты, token или 990.
        self.assert_owned_runner()
        self.remove_bootstrap_ssh_access_from_infra()
        self.ct_exec(
            "rm",
            "-rf",
            "/etc/bootstrap-runner/secrets",
            "/etc/bootstrap-runner/bootstrap-ssh",
            "/etc/bootstrap-runner/pve-host",
            "/var/lib/bootstrap-runner/opentofu/state",
        )
        for temporary_path in (
            "/etc/bootstrap-runner/secrets",
            "/etc/bootstrap-runner/bootstrap-ssh",
            "/etc/bootstrap-runner/pve-host",
        ):
            if self.ct_exec("test", "!", "-e", temporary_path, check=False).returncode:
                self.fail(f"временные данные 990 не удалены: {temporary_path}")
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
        """Удалить принадлежащий проекту временный контейнер и токен либо лишь оставшийся токен."""
        if self.ct_exists():
            self.assert_owned_runner()
            self.remove_private_access()
            if self.pct_status(self.ctid) == "running":
                self.pct("stop", str(self.ctid))
            self.run("pct", "destroy", str(self.ctid), "--purge", "1", quiet=True)
        else:
            self.remove_named_token("root@pam", "bootstrap-runner")
        self.remove_downloaded_template()

    def remove_infra_rootfs_for_recovery(self) -> None:
        """Удалить только воспроизводимый объект управляющего гостя перед recovery.

До снятия protection проверяется метка владения: чужой объект удалять
запрещено. Запускается только после проверки сохранённых данных и
подготовки 990. При отказе после снятия защиты возможно частичное состояние.
"""
        if not self.infra_exists():
            return
        if not self.infra_config_is_expected():
            self.fail(
                f"VMID {self.infra_ctid} не имеет строгой метки владения infra-manager"
            )

        self.pct("set", str(self.infra_ctid), "--protection", "0", quiet=True)
        if self.pct_status(self.infra_ctid) == "running":
            self.pct("stop", str(self.infra_ctid))
        self.run(
            "pct",
            "destroy",
            str(self.infra_ctid),
            "--purge",
            "1",
            quiet=True,
        )
        if self.infra_exists():
            self.fail(
                f"LXC {self.infra_ctid} не удалён перед аварийным пересозданием"
            )
        self.ok(
            f"Воспроизводимый rootfs {self.infra_ctid} удалён; "
            "постоянное состояние сохранено на PVE"
        )

    def remove_openbao_host_support(self) -> None:
        """Убрать хостовый сценарий OpenBao, сохранив unseal-ключ."""
        self.host_openbao_unseal_command.unlink(missing_ok=True)
        shutil.rmtree(self.host_openbao_library, ignore_errors=True)
        self.host_openbao_config.unlink(missing_ok=True)
        self.ok("Сценарий разблокировки OpenBao удалён; ключ на PVE сохранён")

    def remove_infra(self) -> None:
        """Удалить управляемый контейнер и его хостовые средства, сохранив данные.
        
        Сначала удалить временный 990, затем строго проверить метки
        управляющего контейнера. После снятия protection удаляется только его
        объект PVE; постоянное состояние и ключ разблокировки остаются на узле.
        При ошибке операция может быть завершена лишь частично.
        """
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

