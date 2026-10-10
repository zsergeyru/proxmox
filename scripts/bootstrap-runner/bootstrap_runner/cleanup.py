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
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise BootstrapError("PVE вернул некорректный JSON") from exc
        if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
            raise BootstrapError("PVE вернул неожиданный формат списка объектов")
        return value

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
        if ref:
            result = self.run("pvesm", "free", ref, check=False, capture=True)
            if result.returncode:
                # Не обходить слой хранения PVE прямым unlink. Маркер нужен
                # для повторной штатной попытки или ручной диагностики.
                self.info(
                    f"ПРЕДУПРЕЖДЕНИЕ: временный LXC-шаблон {ref} не удалён "
                    "через pvesm free; маркер сохранён"
                )
                return
            self.info(f"Удалён временно скачанный LXC-шаблон: {ref.split(':', 1)[-1]}")
        self.host_template_marker.unlink(missing_ok=True)

    def finalize_runner(self) -> None:
        """Завершить успешную установку, отозвав доступы и удалив временный 990.

        После подтверждённого перехода на SSH CA удаляется одноразовое
        SSH-разрешение из гостя. Нет смысла удалять файлы внутри 990
        отдельно: его проверенный rootfs уничтожается целиком.
        """
        if self.ct_exists():
            self.assert_owned_runner()
        self.remove_bootstrap_ssh_access_from_infra()
        self.remove_private_access()
        self.remove_runner_if_present()
        self.ok("Временный контур 990 полностью удалён")

    def remove_runner_if_present(self) -> None:
        """Удалить только свой одноразовый 990 и освободить временный шаблон.

        Токен отзывается отдельно вызывающим кодом: очистка контейнера
        выполняется даже при ошибке отзыва токена. Прочие PVE-гости
        и все постоянные каталоги остаются без изменений.
        """
        if self.ct_exists():
            self.assert_owned_runner()
            if self.pct_status(self.ctid) == "running":
                self.pct("stop", str(self.ctid))
            self.run("pct", "destroy", str(self.ctid), "--purge", "1", quiet=True)
            if self.ct_exists():
                self.fail(f"Одноразовый LXC {self.ctid} не удалён")
        self.remove_downloaded_template()

    def remove_infra_rootfs_for_recovery(self) -> None:
        """Удалить только свой воспроизводимый rootfs и сохранить данные PVE.

        Защита снимается лишь на время удаления. Если операция stop/destroy
        завершилась ошибкой и объект остался, protection восстанавливается.
        """
        if not self.infra_exists():
            return
        if not self.infra_config_is_expected():
            self.fail(
                f"VMID {self.infra_ctid} не имеет строгой метки владения infra-manager"
            )
        protected = "protection: 1" in self.pct_config(self.infra_ctid).splitlines()
        try:
            if protected:
                self.pct("set", str(self.infra_ctid), "--protection", "0", quiet=True)
            if self.pct_status(self.infra_ctid) == "running":
                self.pct("stop", str(self.infra_ctid))
            self.run(
                "pct", "destroy", str(self.infra_ctid), "--purge", "1", quiet=True
            )
            if self.infra_exists():
                self.fail(
                    f"LXC {self.infra_ctid} не удалён перед аварийным пересозданием"
                )
        finally:
            if protected and self.infra_exists():
                self.pct("set", str(self.infra_ctid), "--protection", "1")
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
        self.remove_private_access()
        self.remove_runner_if_present()
        if self.infra_exists():
            if not self.infra_config_is_expected():
                self.fail(f"VMID {self.infra_ctid} занят чужим объектом")
            self.remove_named_token("root@pam", "infra-manager")
            self.remove_infra_rootfs_for_recovery()
            self.ok(f"LXC {self.infra_ctid} удалён")
        else:
            self.remove_named_token("root@pam", "infra-manager")
            self.ok(f"LXC {self.infra_ctid} уже отсутствует")

        self.remove_openbao_host_support()
        self.remove_host_root_ssh_authorization()
        self.ok("Root SSH-доступ infra-manager к PVE удалён")

