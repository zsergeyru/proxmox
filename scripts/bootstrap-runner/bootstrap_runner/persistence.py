"""Проверка постоянного состояния и подключений управляющего гостя.

Хранит маркер installing/ready в закрытой области PVE, проверяет данные
до аварийного пересоздания rootfs и подготавливает постоянные каталоги.
Не удаляет OpenBao, Semaphore или OpenTofu state. Частично заполненное
состояние запрещает автоматическую повторную первоначальную установку.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .errors import BootstrapError


class BootstrapPersistenceMixin:
    """Проверка постоянных данных и состояния первоначальной установки.

    Маркер сохраняется на PVE отдельно от rootfs гостя. Отсутствие маркера
    при существующем хранилище не разрешает новую установку: такие данные
    считаются ранее созданным состоянием и требуют проверки восстановления.
    """

    def _installation_marker_path(self) -> Path:
        """Вернуть путь к маркеру установки в закрытой области PVE."""
        return self.host_pve_only_dir / "bootstrap-installation.json"

    def _read_installation_marker(self) -> str | None:
        """Проверить маркер и вернуть installing/ready либо None."""
        marker = self._installation_marker_path()
        if marker.is_symlink():
            self.fail(f"Небезопасная символьная ссылка вместо маркера: {marker}")
        if not marker.exists():
            return None
        if not marker.is_file():
            self.fail(f"Маркер установки не является файлом: {marker}")
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BootstrapError(f"Повреждён маркер установки {marker}: {exc}") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("role") != self.infra_role
            or payload.get("vmid") != self.infra_ctid
            or payload.get("state") not in {"installing", "ready"}
        ):
            self.fail(f"Маркер установки {marker} не соответствует управляющему гостю")
        return payload["state"]

    def installation_state(self, *, guest_exists: bool) -> str:
        """Классифицировать состояние без изменения файлов и гостя.

        new: никогда не создававшаяся установка;
        unfinished: прерванная установка, подтверждённая маркером PVE;
        existing: управляющий контейнер уже существует;
        recovery: контейнер утрачен, но постоянные данные сохраняются.
        """
        marker_state = self._read_installation_marker()
        if marker_state == "installing":
            return "unfinished"
        if guest_exists:
            return "existing"
        if marker_state == "ready":
            return "recovery"
        if self.host_persistent_root.exists() or self.host_persistent_root.is_symlink():
            return "recovery"
        return "new"

    def write_installation_marker(self, state: str) -> None:
        """Атомарно сохранить состояние на PVE с доступом только root."""
        if state not in {"installing", "ready"}:
            self.fail(f"Неизвестное состояние установки: {state}")
        marker = self._installation_marker_path()
        if self.host_persistent_root.is_symlink() or self.host_pve_only_dir.is_symlink():
            self.fail("Каталоги постоянного состояния не должны быть символьными ссылками")
        if not self.host_pve_only_dir.is_dir() or marker.is_symlink():
            self.fail("Небезопасный или отсутствующий каталог маркера установки")
        fd, tmp_name = tempfile.mkstemp(prefix=".bootstrap-install-", dir=self.host_pve_only_dir)
        temporary = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(
                    {
                        "schema_version": 1,
                        "role": self.infra_role,
                        "vmid": self.infra_ctid,
                        "state": state,
                    },
                    file,
                    ensure_ascii=False,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)

    def verify_unfinished_installation(self, *, guest_exists: bool) -> None:
        """Разрешить повтор только до появления невоспроизводимых данных.

        После начала работы OpenBao, Semaphore или других служб в state/
        автоматическое удаление rootfs запрещено. В этом случае оператор
        должен сначала подтвердить восстановление сохранённого состояния.
        """
        if self._read_installation_marker() != "installing":
            self.fail("Повтор новой установки требует собственного маркера installing")
        if self.host_persistent_root.is_symlink() or self.host_state_dir.is_symlink():
            self.fail("Небезопасная символьная ссылка в постоянном состоянии")
        if not self.host_state_dir.is_dir():
            self.fail("Отсутствует каталог постоянного состояния; повтор запрещён")
        try:
            occupied = next((p for p in self.host_state_dir.rglob("*") if p.is_file() or p.is_symlink()), None)
        except OSError as exc:
            raise BootstrapError(f"Не удалось проверить постоянные данные: {exc}") from exc
        if occupied is not None or self.host_openbao_unseal_key.exists():
            self.fail(
                "Незавершённая установка уже содержит постоянные данные; "
                "автоматическое пересоздание запрещено. Проверьте состояние "
                "и используйте явное восстановление только при готовых резервных данных."
            )
        if guest_exists and not self.infra_config_is_expected():
            self.fail(f"VMID {self.infra_ctid} занят чужим объектом")
        self.ok("Незавершённая установка подтверждена; постоянных данных ещё нет")

    def managed_guests_exist(self) -> bool:
        """Проверить состав пула managed; ошибку чтения существующего пула считать отказом."""
        result = self.run(
            "pvesh",
            "get",
            "/pools/managed",
            "--output-format",
            "json",
            check=False,
            capture=True,
        )
        if result.returncode != 0:
            pools = self.pveum_json("pool", "list")
            if not any(row.get("poolid") == "managed" for row in pools):
                return False
            self.fail(
                "Не удалось проверить состав PVE pool managed перед recovery"
            )
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise BootstrapError(
                "PVE вернул некорректный JSON для pool managed"
            ) from exc
        members = payload.get("members") if isinstance(payload, dict) else None
        if members is None:
            return False
        if not isinstance(members, list):
            self.fail("PVE pool managed имеет некорректный members")
        return bool(members)

    @staticmethod
    def _directory_has_files(path: Path) -> bool:
        """Найти хотя бы один непустой файл в каталоге состояния."""
        if not path.is_dir():
            return False
        try:
            return any(item.is_file() and item.stat().st_size > 0 for item in path.rglob("*"))
        except OSError:
            return False

    def verify_recovery_state(self) -> None:
        """Проверить данные восстановления до удаления существующего rootfs.

        Требуются ключ разблокировки, копия ключа GitHub, непустое хранилище
        OpenBao и база Semaphore. При наличии объектов managed необходим
        сохранённый OpenTofu state. Проверяется наличие файлов, не целостность
        внутренних баз — проверка не заменяет отдельный резерв и его испытание.
        """
        required_dirs = (
            self.host_pve_only_dir,
            self.host_recovery_dir,
            self.host_state_dir,
            self.host_state_openbao_dir,
            self.host_state_openbao_raft_dir,
        )
        missing_dirs = [str(path) for path in required_dirs if not path.is_dir()]
        if missing_dirs:
            self.fail(
                "Recovery запрещён: отсутствуют обязательные постоянные каталоги: "
                + ", ".join(missing_dirs)
            )

        required_files = (
            self.host_openbao_unseal_key,
            self.host_recovery_github_key,
        )
        missing_files = [
            str(path)
            for path in required_files
            if not path.is_file() or path.stat().st_size == 0
        ]
        if missing_files:
            self.fail(
                "Recovery запрещён: отсутствуют обязательные данные доступа: "
                + ", ".join(missing_files)
            )

        if not self._directory_has_files(self.host_state_openbao_raft_dir):
            self.fail(
                "Recovery запрещён: Raft-состояние OpenBao отсутствует или пусто"
            )

        if self.managed_guests_exist() and (
            not self.host_state_opentofu_file.is_file()
            or self.host_state_opentofu_file.stat().st_size == 0
        ):
            self.fail(
                "Recovery запрещён: в pool managed есть объекты, "
                "но постоянный OpenTofu state отсутствует"
            )

        if not self.host_state_semaphore_db.is_file() or (
            self.host_state_semaphore_db.stat().st_size == 0
        ):
            self.fail(
                "Recovery запрещён: отсутствует постоянная база Semaphore"
            )

        self.ok(f"Аварийное состояние {self.infra_ctid} проверено до восстановления")

    def prepare_new_persistent_layout(self) -> None:
        """Подготовить постоянную область для нового infra-manager без миграции."""
        self.host_persistent_root.mkdir(parents=True, exist_ok=True)
        self.host_pve_only_dir.mkdir(parents=True, exist_ok=True)
        self.host_access_dir.mkdir(parents=True, exist_ok=True)
        self.host_state_dir.mkdir(parents=True, exist_ok=True)

        # infra-manager — непривилегированный LXC с обычным отображением uid:
        # root -> 100000, uid 1001 -> 101001.
        self._set_mode_owner(self.host_persistent_root, 0o755, 0, 0)
        self._set_mode_owner(self.host_pve_only_dir, 0o700, 0, 0)
        self._set_mode_owner(self.host_access_dir, 0o755, 0, 0)
        self._set_mode_owner(self.host_state_dir, 0o700, 100000, 100000)

        for path, mode, uid, gid in (
            (self.host_access_pve_host_dir, 0o700, 101001, 100000),
            (self.host_access_ca_dir, 0o755, 100000, 100000),
            (self.host_openbao_unseal_key.parent, 0o700, 0, 0),
            (self.host_recovery_dir, 0o700, 0, 0),
        ):
            path.mkdir(parents=True, exist_ok=True)
            self._set_mode_owner(path, mode, uid, gid)

        if not self.host_recovery_github_key.exists():
            self._copy_access_file(
                self.host_github_key,
                self.host_recovery_github_key,
                mode=0o600,
                uid=0,
                gid=0,
            )

        ca_source = Path("/etc/pve/pve-root-ca.pem")
        self._copy_access_file(
            ca_source,
            self.host_pve_ca,
            mode=0o644,
            uid=100000,
            gid=100000,
        )
        self.ok(f"Постоянные каталоги нового {self.infra_ctid} подготовлены на PVE")

    def _infra_mount_matches(
        self,
        config: str,
        name: str,
        source: Path,
        target: Path,
        *,
        read_only: bool,
    ) -> bool:
        """Сверить источник, назначение и режим чтения одной точки подключения PVE."""
        line = next(
            (row for row in config.splitlines() if row.startswith(f"{name}: ")),
            "",
        )
        if not line:
            return False
        value = line.split(": ", 1)[1]
        parts = value.split(",")
        if not parts or parts[0] != str(source):
            return False
        options = {
            key: val
            for item in parts[1:]
            if "=" in item
            for key, val in [item.split("=", 1)]
        }
        if options.get("mp") != str(target):
            return False
        return (options.get("ro") == "1") if read_only else ("ro" not in options)

    def verify_persistent_layout(self) -> None:
        """Проверить уже подготовленную схему, ничего не копируя."""
        required_dirs = (
            self.host_pve_only_dir,
            self.host_recovery_dir,
            self.host_access_dir,
            self.host_state_dir,
            self.host_access_pve_host_dir,
            self.host_access_ca_dir,
        )
        missing = [str(path) for path in required_dirs if not path.is_dir()]
        if missing:
            self.fail(
                f"{self.infra_ctid} ещё использует старую схему постоянных данных; "
                "автоматическая миграция запрещена. Выполните ручную миграцию. "
                f"Отсутствуют: {', '.join(missing)}"
            )

        config = self.pct_config(self.infra_ctid)
        access_ok = self._infra_mount_matches(
            config,
            "mp0",
            self.host_access_dir,
            self.infra_access_dir,
            read_only=True,
        )
        state_ok = self._infra_mount_matches(
            config,
            "mp1",
            self.host_state_dir,
            self.infra_state_dir,
            read_only=False,
        )
        if not access_ok or not state_ok:
            self.fail(
                f"LXC {self.infra_ctid} не подключён к постоянным каталогам PVE. "
                "Автоматическая миграция существующих данных запрещена; "
                "выполните её вручную."
            )

        required_files = (
            self.host_recovery_github_key,
            self.host_pve_root_key,
            self.host_pve_root_known_hosts,
            self.host_pve_ca,
        )
        missing_files = [
            str(path)
            for path in required_files
            if not path.is_file() or path.stat().st_size == 0
        ]
        if missing_files:
            self.fail(
                "В постоянном каталоге PVE отсутствуют данные доступа: "
                + ", ".join(missing_files)
            )

    def attach_persistent_layout(self) -> None:
        """Подключить подготовленные каталоги к новому infra-manager."""
        if not self.infra_exists():
            self.fail(f"нельзя подключить постоянные каталоги: LXC {self.infra_ctid} отсутствует")
        if not self.infra_config_is_expected():
            self.fail(f"VMID {self.infra_ctid} занят чужим объектом")

        config = self.pct_config(self.infra_ctid)
        protection_enabled = any(
            line.strip() == "protection: 1"
            for line in config.splitlines()
        )

        was_running = self.pct_status(self.infra_ctid) == "running"
        if was_running:
            self.pct("stop", str(self.infra_ctid))

        try:
            if protection_enabled:
                self.pct(
                    "set",
                    str(self.infra_ctid),
                    "--protection",
                    "0",
                )

            try:
                self.pct(
                    "set",
                    str(self.infra_ctid),
                    "--mp0",
                    f"{self.host_access_dir},mp={self.infra_access_dir},ro=1",
                    "--mp1",
                    f"{self.host_state_dir},mp={self.infra_state_dir}",
                )
            finally:
                if protection_enabled:
                    self.pct(
                        "set",
                        str(self.infra_ctid),
                        "--protection",
                        "1",
                    )
        except BootstrapError:
            if was_running and self.pct_status(self.infra_ctid) != "running":
                self.pct("start", str(self.infra_ctid), check=False)
            raise

        self.pct("start", str(self.infra_ctid))
        self.verify_persistent_layout()
        self.ok(f"Постоянные каталоги PVE подключены к {self.infra_ctid}")

