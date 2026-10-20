"""Безопасная подготовка физического SSD PVE штатным Proxmox API.

Только оператор `deploy 910` предлагает одноразовое подтверждение перед
первой разметкой. На PVE не устанавливаются программы, скрипты или службы.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

from .common import InfraManagerError, console, run
from .pve import PveClient
from .pve_host import _ssh_command
from .settings import PATHS
from .storage_catalog import load_physical_storage

MOUNT = "/mnt/pve/backup-fast"
NAME = "backup-fast"
STATE_NAME = "pve-backup-ssd.json"
SERIAL_PATTERN = re.compile(r"[A-Za-z0-9._-]{6,128}")


@dataclass(frozen=True)
class Disk:
    devpath: str
    serial: str
    model: str
    size: int


def _inventory(client: PveClient, node: str) -> list[dict]:
    records = client.data(f"/nodes/{node}/disks/list", query={"include-partitions": 1})
    if not isinstance(records, list):
        raise InfraManagerError("PVE не предоставил перечень физических дисков")
    return [item for item in records if isinstance(item, dict)]


def _eligible(item: dict, min_bytes: int) -> Disk | None:
    """Пустой SSD, не используемый PVE, не раздел и не системный NVMe."""
    path, serial, model = item.get("devpath"), item.get("serial"), item.get("model")
    size = item.get("size")
    if (not isinstance(path, str) or not re.fullmatch(r"/dev/(sd[a-z]+|nvme\d+n\d+)", path)
            or not isinstance(serial, str) or not SERIAL_PATTERN.fullmatch(serial)
            or not isinstance(model, str) or not model.strip()
            or not isinstance(size, int) or isinstance(size, bool) or size < min_bytes
            or item.get("used") not in (None, "")
            or item.get("parent") not in (None, "")
            or item.get("mounted") not in (False, 0, None)
            or item.get("gpt") not in (False, 0, None)
            or item.get("type") not in ("ssd", "nvme")):
        return None
    return Disk(path, serial, model.strip(), size)


def _empty_block_device(node: str, disk: Disk) -> None:
    """Перепроверить PVE блочное устройство до любого изменения GPT.

    Чтение только штатными lsblk/wipefs. Даже если PVE API считает диск
    свободным, любой раздел, сигнатура, mount или ошибка проверки — запрет.
    """
    result = run(
        _ssh_command(node, (
            "lsblk", "--json", "--bytes", "--paths",
            "--output", "PATH,TYPE,SIZE,FSTYPE,MOUNTPOINTS,RO",
            disk.devpath,
        )),
        capture_output=True, check=False,
    )
    if result.returncode:
        raise InfraManagerError("Не удалось проверить состав разделов SSD на PVE")
    try:
        devices = json.loads(result.stdout)["blockdevices"]
    except (ValueError, KeyError, TypeError) as exc:
        raise InfraManagerError("Неизвестный ответ lsblk для SSD") from exc
    if not isinstance(devices, list) or len(devices) != 1:
        raise InfraManagerError("SSD не является единственным блочным устройством")
    item = devices[0]
    if (item.get("path") != disk.devpath or item.get("type") != "disk"
            or item.get("size") != disk.size or item.get("ro") not in (0, False)
            or item.get("fstype") not in (None, "")
            or item.get("mountpoints") not in (None, [], [None])
            or item.get("children")):
        raise InfraManagerError(
            "На физическом SSD есть разделы, сигнатуры или использование; "
            "изменение разметки запрещено"
        )
    signatures = run(
        _ssh_command(node, ("wipefs", "--no-act", disk.devpath)),
        capture_output=True, check=False,
    )
    if signatures.returncode or signatures.stdout.strip():
        raise InfraManagerError(
            "SSD имеет сигнатуры либо их невозможно проверить; "
            "первичное форматирование запрещено"
        )


def _mount_info(node: str) -> tuple[str, str]:
    """Проверить фактический UUID и тип, не доверяя имени каталога."""
    result = run(
        _ssh_command(node, (
            "findmnt", "-n", "-M", MOUNT, "-o", "UUID,FSTYPE",
        )),
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise InfraManagerError("Резервный SSD ещё не смонтирован на PVE")
    parts = result.stdout.strip().split()
    if len(parts) != 2 or parts[1] != "ext4" or not re.fullmatch(r"[0-9a-fA-F-]{36}", parts[0]):
        raise InfraManagerError("Не удалось подтвердить UUID/ext4 SSD быстрых копий")
    return parts[0], parts[1]


def _directory(client: PveClient, node: str) -> dict | None:
    records = client.data(f"/nodes/{node}/disks/directory")
    if not isinstance(records, list):
        raise InfraManagerError("PVE не подтвердил список управляемых каталогов")
    found = [item for item in records if isinstance(item, dict) and item.get("path") == MOUNT]
    if len(found) > 1:
        raise InfraManagerError("PVE сообщил о дублирующемся подключении backup-fast")
    return found[0] if found else None


def _mount_existing_unit(node: str, directory: dict) -> None:
    """Запустить уже созданный Proxmox mount-unit, не устанавливая службу."""
    unit = r"mnt-pve-backup\x2dfast.mount"
    unit_file = directory.get("unitfile")
    if (not isinstance(unit_file, str)
            or unit_file != f"/etc/systemd/system/{unit}"):
        raise InfraManagerError(
            "Не подтверждён штатный Proxmox mount-unit backup-fast"
        )
    result = run(_ssh_command(node, ("systemctl", "start", unit)),
                 capture_output=True, check=False)
    if result.returncode:
        raise InfraManagerError("Не удалось восстановить штатное монтирование SSD")


def _state_path() -> Path:
    return Path("/mnt/persistent-state/physical-hosts") / STATE_NAME


def _read_state(path: Path) -> dict | None:
    if path.is_symlink():
        raise InfraManagerError("Недоверенная ссылка вместо состояния SSD")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfraManagerError("Нарушено защищённое состояние идентичности SSD") from exc
    if (not isinstance(value, dict)
            or set(value) != {"schema_version", "serial", "uuid", "mountpoint"}
            or value["schema_version"] != 1 or value["mountpoint"] != MOUNT
            or not isinstance(value["serial"], str)
            or not SERIAL_PATTERN.fullmatch(value["serial"])
            or not isinstance(value["uuid"], str)
            or not re.fullmatch(r"[0-9a-fA-F-]{36}", value["uuid"])):
        raise InfraManagerError("Неверная запись подтверждённого SSD")
    return value


def _save_state(path: Path, record: dict) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise InfraManagerError("Недоступно постоянное состояние SSD")
    if path.is_symlink():
        raise InfraManagerError("Состояние SSD нельзя заменять через ссылку")
    fd, name = tempfile.mkstemp(prefix=".pve-backup-ssd-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


APPROVAL_NAME = "pve-backup-ssd-approval.json"
APPROVAL_TTL = 3600


def _approval_path() -> Path:
    return _state_path().with_name(APPROVAL_NAME)


def _plan(disk: Disk) -> dict:
    details = {
        "devpath": disk.devpath,
        "serial": disk.serial,
        "model": disk.model,
        "size_bytes": disk.size,
        "filesystem": "ext4",
        "mountpoint": MOUNT,
    }
    plan_id = hashlib.sha256(json.dumps(
        details, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"plan_id": plan_id, **details}


def inspect_backup_ssd(client: PveClient, node: str) -> dict:
    """Только чтение: та же строгая проверка, что перед первым mkfs."""
    saved = _read_state(_state_path())
    if saved is not None:
        return {
            "state": "configured",
            "mountpoint": MOUNT,
            "uuid": saved["uuid"],
            "serial": saved["serial"],
        }
    if _directory(client, node) is not None:
        return {"state": "blocked", "reason": "SSD уже подключён без доверенной записи"}
    storage = load_physical_storage(PATHS.repo_root, "pve")
    devices = [item for item in storage.devices if item.get("id") == "backup_ssd"
               and item.get("role") == "backup"]
    if len(devices) != 1:
        raise InfraManagerError("Нет единственного backup_ssd в storage.yaml")
    size = int(devices[0]["capacity_gb"] * 10**9 * .90)
    candidates = [d for item in _inventory(client, node)
                  if (d := _eligible(item, size))]
    if len(candidates) != 1:
        return {"state": "blocked", "reason": "Нет единственного свободного SSD"}
    disk = candidates[0]
    _empty_block_device(node, disk)
    return {"state": "needs-confirmation", **_plan(disk)}


def approve_backup_ssd(client: PveClient, node: str, *,
                       plan_id: str, serial: str) -> None:
    """Веб-подтверждение: только запись одноразового разрешения, без mkfs."""
    if not isinstance(plan_id, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_id):
        raise InfraManagerError("Некорректный номер плана SSD")
    if not isinstance(serial, str) or not SERIAL_PATTERN.fullmatch(serial):
        raise InfraManagerError("Некорректный номер физического диска")
    current = inspect_backup_ssd(client, node)
    if (current.get("state") != "needs-confirmation"
            or current["plan_id"] != plan_id
            or current["serial"] != serial):
        raise InfraManagerError("Устройство изменилось; старое разрешение не действует")
    _save_state(_approval_path(), {
        "schema_version": 1,
        "plan_id": plan_id,
        "serial": serial,
        "expires": int(time.time()) + APPROVAL_TTL,
    })


def _consume_approval(disk: Disk) -> bool:
    """Одноразовый допуск после ПОВТОРНОЙ проверки устройства и wipefs.

    При неизвестном содержимом не удаляем разрешение, но не делаем запись.
    До вызова PVE API подтверждение удаляется; повторный deploy не может
    безоговорочно выполнить вторую разметку после обрыва соединения.
    """
    path = _approval_path()
    if path.is_symlink():
        raise InfraManagerError("Недоверенная ссылка вместо разрешения SSD")
    if not path.exists():
        return False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfraManagerError("Повреждена запись разрешения SSD") from exc
    expected = {
        "schema_version": 1, "plan_id": _plan(disk)["plan_id"],
        "serial": disk.serial,
    }
    if not isinstance(value, dict) or any(value.get(k) != v for k,v in expected.items()):
        raise InfraManagerError("Разрешение относится к другому физическому SSD")
    expiry = value.get("expires")
    if type(expiry) is not int or not int(time.time()) < expiry <= time.time() + APPROVAL_TTL:
        raise InfraManagerError("Срок действия разрешения на разметку SSD истёк")
    path.unlink()
    return True


def reconcile_backup_ssd(client: PveClient, node: str, *, interactive: bool = False,
                         confirmation=None) -> str | None:
    """Вернуть проверенный UUID либо сообщить о необходимости первичной настройки.

    Не форматировать без нового точного подтверждения на доверенном CLI.
    При замене диска подтверждение старого экземпляра не подходит новому.
    """
    storage = load_physical_storage(PATHS.repo_root, "pve")
    backup = [disk for disk in storage.devices if disk.get("id") == "backup_ssd"]
    if len(backup) != 1 or backup[0].get("role") != "backup":
        raise InfraManagerError("Нет единственного backup_ssd в storage.yaml")
    min_bytes = int(backup[0]["capacity_gb"] * 10**9 * .90)
    state_path = _state_path()
    existing = _read_state(state_path)
    directory = _directory(client, node)
    inventory = _inventory(client, node)
    if existing:
        # Наличие каталога /mnt/pve/... без systemd mount и верного UUID
        # не подтверждает SSD, даже если PVE API показывает storage.
        if directory is None:
            raise InfraManagerError("Запомненный SSD не подключён в Proxmox")
        try:
            uuid, _ = _mount_info(node)
        except InfraManagerError:
            # После перезагрузки systemd mount-unit может быть остановлен.
            # Разрешено запустить только уже зарегистрированную единицу PVE.
            _mount_existing_unit(node, directory)
            uuid, _ = _mount_info(node)
        if uuid != existing["uuid"]:
            raise InfraManagerError("UUID SSD изменился; требуется подтверждение замены")
        matches = [i for i in inventory if i.get("serial") == existing["serial"]
                   and i.get("parent") in (None, "")]
        if len(matches) != 1:
            raise InfraManagerError("Прежний SSD не найден по подтверждённому серийному номеру")
        return uuid

    if directory is not None:
        raise InfraManagerError(
            "SSD подключён, но нет доверенной записи идентичности; "
            "автоматическое принятие чужого хранилища запрещено"
        )
    candidates = [disk for item in inventory if (disk := _eligible(item, min_bytes))]
    if len(candidates) != 1:
        console.warning(
            "SSD backup-fast ещё не подготовлен: "
            "не удалось однозначно определить один пустой физический накопитель"
        )
        return None
    disk = candidates[0]
    console.info(
        f"План SSD: {disk.model}, {disk.size // 10**9} ГБ, "
        f"серийный номер {disk.serial}; GPT + ext4, {MOUNT}. "
        "Создание уничтожит прежние данные на выбранном диске."
    )
    # Даже план предлагается только после независимой проверки разделов:
    # used=None от PVE API сам по себе не гарантирует пустой диск.
    _empty_block_device(node, disk)
    approved = False
    if interactive:
        answer = (confirmation or input)(
            "Для разрешения первичной разметки введите серийный номер SSD полностью: "
        ).strip()
        approved = answer == disk.serial
    if not approved and not _approval_path().exists():
        console.warning(
            "Первичная разметка SSD ожидает подтверждения через infra-web "
            "или доверенную CLI PVE"
        )
        return None
    if not approved and _approval_path().is_symlink():
        raise InfraManagerError("Недоверенная ссылка вместо разрешения SSD")
    # Повторная проверка непосредственно до операции: запрет TOCTOU и замены.
    candidates_now = [d for item in _inventory(client, node)
                      if (d := _eligible(item, min_bytes))]
    if candidates_now != [disk] or _directory(client, node) is not None:
        raise InfraManagerError("Состояние дисков изменилось после подтверждения; запись запрещена")
    _empty_block_device(node, disk)
    if not approved:
        approved = _consume_approval(disk)
    if not approved:
        console.warning("Подготовка SSD пропущена: подтверждение не совпало")
        return None
    client.run_task(
        "POST", f"/nodes/{node}/disks/directory", node=node,
        form={"device": disk.devpath, "name": NAME, "filesystem": "ext4", "add_storage": 0},
        timeout=1200,
    )
    if _directory(client, node) is None:
        raise InfraManagerError("PVE не подтвердил подключение подготовленного SSD")
    uuid, _ = _mount_info(node)
    _save_state(state_path, {
        "schema_version": 1, "serial": disk.serial,
        "uuid": uuid, "mountpoint": MOUNT,
    })
    console.ok("SSD backup-fast подготовлен штатным Proxmox API и проверен по UUID")
    return uuid


def deploy_backup_ssd_preflight() -> None:
    """Ограниченная стадия 910, не блокирующая исправный управляющий контур."""
    from .operator import _pve_node
    try:
        can_ask = sys.stdin.isatty() or os.environ.get("INFRA_MANAGER_INTERACTIVE") == "1"
        reconcile_backup_ssd(PveClient(), _pve_node(), interactive=can_ask)
    except (InfraManagerError, EOFError) as exc:
        console.warning(f"Резервный SSD: {exc}. Основной deploy 910 продолжится")
