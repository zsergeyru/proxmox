#!/usr/bin/env python3
"""Подготовка постоянных томов LXC и их каталогов на PVE.

Сценарий получает описание гостя через stdin, выполняется только на PVE и
никогда не форматирует существующий логический том.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class StorageError(RuntimeError):
    """Постоянное хранилище не соответствует описанию гостя."""


STATE_DIR = Path("/etc/infra-manager/guest-storage")
HOOK_SOURCE = Path("/var/lib/vz/snippets/infra-manager-guest-storage.py")
HOOK_ID = "local:snippets/infra-manager-guest-storage.py"
PENDING_TAG = "infra_manager_pending"


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Выполнить системную команду; при ошибке сохранить причину."""
    result = subprocess.run(args, text=True, capture_output=True, check=False)
    if check and result.returncode:
        reason = result.stderr.strip() or result.stdout.strip() or f"код {result.returncode}"
        raise StorageError(f"{' '.join(args[:2])}: {reason}")
    return result


def validate(vmid: int, config: dict) -> tuple[str, str, dict[str, dict], list[dict]]:
    """Проверить описание томов и подключений до записи на диск."""
    if type(vmid) is not int or not 100 <= vmid <= 9999:
        raise StorageError("Некорректный VMID")
    if not isinstance(config, dict):
        raise StorageError("Некорректное описание хранилища")
    vg, pool = config.get("volume_group"), config.get("thin_pool")
    for name in (vg, pool):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_+-]+", name):
            raise StorageError("Недопустимое имя группы томов или пула")

    raw_volumes = config.get("volumes")
    if not isinstance(raw_volumes, list) or not raw_volumes:
        raise StorageError("Не описаны постоянные тома")
    volumes: dict[str, dict] = {}
    paths: set[str] = set()
    for volume in raw_volumes:
        if not isinstance(volume, dict):
            raise StorageError("Некорректное описание тома")
        name, size, mountpoint = (
            volume.get("name"), volume.get("size_gb"), volume.get("mountpoint")
        )
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_+-]*", name):
            raise StorageError("Недопустимое имя тома")
        if type(size) is not int or not 1 <= size <= 10000:
            raise StorageError("Некорректный размер тома")
        if (
            not isinstance(mountpoint, str)
            or not mountpoint.startswith("/mnt/bindmounts/")
            or not Path(mountpoint).is_absolute()
            or ".." in Path(mountpoint).parts
            or "//" in mountpoint
            or str(Path(mountpoint)) != mountpoint
        ):
            raise StorageError("Недопустимая точка подключения тома PVE")
        if name in volumes or mountpoint in paths:
            raise StorageError("Повторяющийся том или путь на PVE")
        volumes[name] = volume
        paths.add(mountpoint)
    for first in paths:
        for second in paths:
            if first != second and Path(first) in Path(second).parents:
                raise StorageError("Вложенные точки подключения томов запрещены")

    mounts = config.get("mounts")
    if not isinstance(mounts, list) or not mounts:
        raise StorageError("Не описаны подключения гостя")
    slots: set[str] = set()
    directories: set[tuple[str, str]] = set()
    targets: set[str] = set()
    for item in mounts:
        if not isinstance(item, dict):
            raise StorageError("Некорректная запись подключения")
        slot, volume_name, directory, target = (
            item.get("slot"), item.get("volume"),
            item.get("directory"), item.get("target")
        )
        if not isinstance(slot, str) or not re.fullmatch(r"mp[0-9]", slot) or slot in slots:
            raise StorageError("Недопустимый или повторяющийся номер подключения")
        if not isinstance(volume_name, str) or volume_name not in volumes:
            raise StorageError("Подключение ссылается на неизвестный том")
        if (
            not isinstance(directory, str)
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", directory)
            or (volume_name, directory) in directories
        ):
            raise StorageError("Недопустимый или повторяющийся каталог тома")
        if (
            not isinstance(target, str)
            or not target.startswith("/")
            or target == "/"
            or ".." in Path(target).parts
            or "//" in target
            or str(Path(target)) != target
            or target in targets
        ):
            raise StorageError("Недопустимый или повторяющийся каталог гостя")
        if type(item.get("read_only")) is not bool:
            raise StorageError("Не задан режим доступа к каталогу")
        slots.add(slot)
        directories.add((volume_name, directory))
        targets.add(target)
    return vg, pool, volumes, mounts


def source_path(volumes: dict[str, dict], item: dict) -> Path:
    """Путь папки гостя внутри выбранного тома на PVE."""
    return Path(volumes[item["volume"]]["mountpoint"]) / item["directory"]


def existing_mount_config(vmid: int, volumes: dict[str, dict], mounts: list[dict]) -> list[dict]:
    """Разрешить сменить ro только у того же каталога LXC, не заменяя источник."""
    config = command("pct", "config", str(vmid)).stdout
    if "unprivileged: 1" not in config.splitlines():
        raise StorageError(f"LXC {vmid} должен быть непривилегированным")
    lines = dict(line.split(": ", 1) for line in config.splitlines() if ": " in line)
    pending: list[dict] = []
    for item in mounts:
        old = lines.get(item["slot"])
        if old is None:
            pending.append(item)
            continue
        parts = old.split(",")
        attributes = dict(part.split("=", 1) for part in parts[1:] if "=" in part)
        readonly = attributes.get("ro", "0") == "1"
        if (
            parts[0] != str(source_path(volumes, item))
            or attributes.get("mp") != item["target"]
        ):
            raise StorageError(f"{item['slot']} уже занят другим подключением: {old}")
        if readonly != item["read_only"]:
            # Меняем только флаг ro существующего подключения; неизвестные
            # дополнительные параметры не должны теряться при pct set.
            if set(attributes) - {"mp", "ro"}:
                raise StorageError(
                    f"{item['slot']}: смена режима запрещена при дополнительных "
                    f"параметрах подключения: {old}"
                )
            pending.append(item)
    return pending


def ensure_volume(vg: str, pool: str, volume: dict) -> tuple[Path, str]:
    """Проверить том или создать новый с ext4 без перезаписи данных.

    Существующий том не уменьшается и не форматируется. Созданный здесь
    новый том получает временную метку; повторное форматирование при
    незавершённой установке допускается только при наличии этой метки.
    После mkfs выполняется проверка без исправления файловой системы.
    При любой неопределённости выполнение останавливается.
    """
    device = Path(f"/dev/{vg}/{volume['name']}")
    # Список имён позволяет отличить отсутствие тома от ошибки LVM.
    names = command("lvs", "--noheadings", "-o", "lv_name", vg).stdout.split()
    created = volume["name"] not in names
    if created:
        command(
            "lvcreate", "-V", f"{volume['size_gb']}G",
            "-T", f"{vg}/{pool}", "-n", volume["name"],
            "--addtag", PENDING_TAG,
        )
    else:
        size_result = command(
            "lvs", "--noheadings", "--units", "b", "--nosuffix",
            "-o", "lv_size", str(device),
        ).stdout.strip()
        pool_result = command(
            "lvs", "--noheadings", "-o", "pool_lv", str(device)
        ).stdout.strip()
        try:
            actual_size = int(float(size_result))
        except ValueError as exc:
            raise StorageError(f"Не удалось определить размер тома {device}") from exc
        if pool_result != pool or actual_size + 1024**2 < volume["size_gb"] * 1024**3:
            raise StorageError(
                f"Том {device} уже существует, но размер меньше требуемого "
                "или он находится в другом пуле. Автоматическое изменение запрещено"
            )
        # Разрешаем вручную увеличить том: повторный deploy не должен
        # возвращать его к первоначальному размеру или блокировать обновление.

    filesystem = command(
        "blkid", "-s", "TYPE", "-o", "value", str(device), check=False
    )
    formatted_now = False
    if filesystem.returncode or not filesystem.stdout.strip():
        # Повторять форматирование можно лишь для LV, который был создан
        # нашим сценарием и пока помечен как незавершённый.
        tags = command(
            "lvs", "--noheadings", "-o", "lv_tags", str(device)
        ).stdout.strip().split(",")
        if PENDING_TAG not in [tag.strip() for tag in tags]:
            raise StorageError(
                f"Том {device} без файловой системы. "
                "Для существующих томов форматирование запрещено"
            )
        command("mkfs.ext4", "-m", "0", str(device))
        formatted_now = True
        filesystem = command("blkid", "-s", "TYPE", "-o", "value", str(device))
    if filesystem.returncode or filesystem.stdout.strip() != "ext4":
        raise StorageError(
            f"Том {device} не имеет ext4. Форматирование существующего тома запрещено"
        )
    # При прерывании mkfs существует риск неполной файловой системы:
    # проверка без исправлений предотвращает автоматическое повреждение.
    if formatted_now:
        command("e2fsck", "-fn", str(device))
    tags = command(
        "lvs", "--noheadings", "-o", "lv_tags", str(device)
    ).stdout.strip().split(",")
    if PENDING_TAG in [tag.strip() for tag in tags]:
        command("lvchange", "--deltag", PENDING_TAG, str(device))

    uuid = command(
        "blkid", "-s", "UUID", "-o", "value", str(device)
    ).stdout.strip()
    if not re.fullmatch(r"[a-fA-F0-9-]+", uuid):
        raise StorageError(f"Некорректный UUID тома {device}")
    return device, uuid


def ensure_host_mount(device: Path, uuid: str, volume: dict) -> Path:
    """Подключить том на PVE; не скрывать уже лежащие в папке данные."""
    path = Path(volume["mountpoint"])
    if path.is_symlink():
        raise StorageError(f"Вместо каталога обнаружена ссылка: {path}")
    path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir():
        raise StorageError(f"Не каталог: {path}")

    mounted = command(
        "findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path), check=False
    )
    if mounted.returncode:
        if any(path.iterdir()):
            raise StorageError(f"Нельзя скрыть существующие файлы: {path}")
    elif os.path.realpath(mounted.stdout.strip()) != os.path.realpath(device):
        raise StorageError(
            f"На {path} подключён другой диск: {mounted.stdout.strip()}"
        )

    fstab = Path("/etc/fstab")
    content = fstab.read_text(encoding="utf-8")
    entries = [
        line.split() for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    entry = f"UUID={uuid} {path} ext4 defaults,nofail,x-systemd.before=pve-guests.service 0 2"
    matches = [parts for parts in entries if len(parts) >= 2 and parts[1] == str(path)]
    if matches:
        if len(matches) != 1 or matches[0][:3] != [f"UUID={uuid}", str(path), "ext4"]:
            raise StorageError(f"В fstab уже есть другое подключение {path}")
    else:
        with fstab.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(entry + "\n")

    if mounted.returncode:
        command("mount", str(path))
        mounted = command(
            "findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path)
        )
    if os.path.realpath(mounted.stdout.strip()) != os.path.realpath(device):
        raise StorageError(f"Том {device} не подключён к {path}")
    return path


def verify_mounted_volumes(vg: str, volumes: dict[str, dict], mounts: list[dict]) -> None:
    """Не дать LXC запуститься на пустых папках системного раздела PVE."""
    for name, volume in volumes.items():
        device = Path(f"/dev/{vg}/{name}")
        path = Path(volume["mountpoint"])
        mounted = command(
            "findmnt", "-rn", "-o", "SOURCE", "--mountpoint", str(path),
            check=False,
        )
        if mounted.returncode or os.path.realpath(mounted.stdout.strip()) != os.path.realpath(device):
            raise StorageError(f"Не подключён том {device} по {path}; запуск гостя запрещён")
        if command("blkid", "-s", "TYPE", "-o", "value", str(device)).stdout.strip() != "ext4":
            raise StorageError(f"Неожиданная файловая система {device}")
    for item in mounts:
        source = source_path(volumes, item)
        if source.is_symlink() or not source.is_dir():
            raise StorageError(f"Нет постоянного каталога {source}; запуск запрещён")


def verify_guest_start(vmid: int) -> None:
    """Проверка pre-start выполняется при каждом включении контейнера."""
    manifest = STATE_DIR / f"{vmid}.json"
    if not manifest.is_file():
        raise StorageError(f"Для LXC {vmid} не найдено описание хранилища {manifest}")
    vg, _pool, volumes, mounts = validate(
        vmid, json.loads(manifest.read_text(encoding="utf-8"))
    )
    verify_mounted_volumes(vg, volumes, mounts)


def _save_startup_config(vmid: int, config: dict) -> None:
    """Сохранить проверенный перечень томов до включения защиты запуска."""
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=STATE_DIR,
        prefix=f".{vmid}-", delete=False
    ) as temporary:
        path = Path(temporary.name)
        json.dump(config, temporary, ensure_ascii=False)
        temporary.write("\n")
    try:
        path.chmod(0o600)
        os.replace(path, STATE_DIR / f"{vmid}.json")
    finally:
        path.unlink(missing_ok=True)


def ensure_hook_storage() -> None:
    """Проверить локальное хранилище PVE и разрешить файлы сценариев."""
    # В Proxmox нет команды pvesm config. Конфигурацию хранилищ читает
    # штатная команда pvesh через API самого узла.
    result = command(
        "pvesh", "get", "/storage/local", "--output-format", "json"
    )
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StorageError("PVE вернул некорректное описание хранилища local") from exc
    if not isinstance(config, dict) or (
        config.get("storage") != "local"
        or config.get("type") != "dir"
        or config.get("path") != "/var/lib/vz"
    ):
        raise StorageError("Хранилище local не соответствует ожидаемому каталогу PVE")

    raw_content = config.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        raise StorageError("PVE не сообщил виды содержимого хранилища local")
    content = [item.strip() for item in raw_content.split(",") if item.strip()]
    if "snippets" not in content:
        # Сохранить все прежние типы файлов; не заменять настройки local.
        command(
            "pvesm", "set", "local", "--content",
            ",".join([*content, "snippets"]),
        )


def ensure_startup_hook(vmid: int, config: dict) -> None:
    """Установить защиту pre-start, не заменяя чужие PVE hookscript."""
    existing = command("pct", "config", str(vmid)).stdout
    hooks = [
        line.partition(": ")[2].strip()
        for line in existing.splitlines() if line.startswith("hookscript: ")
    ]
    if hooks and hooks != [HOOK_ID]:
        raise StorageError(f"LXC {vmid}: уже задан другой сценарий запуска {hooks}")
    ensure_hook_storage()
    HOOK_SOURCE.parent.mkdir(parents=True, exist_ok=True)
    # Обновление через атомарную замену: при обновлении сценарий запуска
    # никогда не увидит наполовину записанный Python-файл.
    with tempfile.NamedTemporaryFile(
        dir=HOOK_SOURCE.parent, prefix=".guest-storage-", delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
    try:
        shutil.copyfile(Path(__file__), temporary_path)
        temporary_path.chmod(0o755)
        os.replace(temporary_path, HOOK_SOURCE)
    finally:
        temporary_path.unlink(missing_ok=True)
    _save_startup_config(vmid, config)
    if not hooks:
        command("pct", "set", str(vmid), "--hookscript", HOOK_ID)


def ensure_originals_writable(originals: Path) -> None:
    """Разрешить root непривилегированного 340 удалять файлы из всех папок.

    ACL на существующих каталогах обеспечивает удаление уже загруженных
    файлов; ACL по умолчанию наследуется вновь создаваемыми папками.
    Сами файлы и их содержимое не изменяются.
    """
    if shutil.which("setfacl") is None:
        # На чистом PVE пакет acl может отсутствовать. Устанавливаем его
        # штатным deploy только при необходимости изменить права оригиналов.
        if shutil.which("apt-get") is None:
            raise StorageError("На PVE недоступны setfacl и apt-get")
        command("apt-get", "update")
        command("apt-get", "install", "-y", "--no-install-recommends", "acl")
        if shutil.which("setfacl") is None:
            raise StorageError("Пакет acl установлен, но команда setfacl недоступна")
    for directory, _subdirs, _files in os.walk(originals, followlinks=False):
        command(
            "setfacl", "-m", "u:100000:rwx,d:u:100000:rwx", directory,
        )


def ensure_directories(volumes: dict[str, dict], mounts: list[dict]) -> None:
    """Создать только подкаталоги; существующее содержимое не трогать."""
    for item in mounts:
        path = source_path(volumes, item)
        if path.is_symlink():
            raise StorageError(f"Вместо каталога обнаружена ссылка: {path}")
        path.mkdir(exist_ok=True)
        if not path.is_dir():
            raise StorageError(f"Не каталог: {path}")
        if item["read_only"]:
            path.chmod(0o755)
        else:
            # Непривилегированный LXC: root гостя соответствует uid/gid 100000.
            os.chown(path, 100000, 100000)
            path.chmod(0o750)

        if item["directory"] == "data":
            originals = path / "originals"
            if originals.is_symlink():
                raise StorageError(f"Вместо каталога обнаружена ссылка: {originals}")
            originals.mkdir(exist_ok=True)
            if item["read_only"]:
                originals.chmod(0o755)
            else:
                ensure_originals_writable(originals)


def configure_lxc(
    vmid: int,
    volumes: dict[str, dict],
    mounts: list[dict],
    pending: list[dict],
    config: dict,
) -> None:
    """Настроить подключения LXC с временным снятием защиты.

    При необходимости контейнер сначала останавливается. Защита
    восстанавливается в finally; при ошибке подключения нельзя
    запускать контейнер с неполным набором постоянных томов.
    После успешной настройки каждое подключение проверяется внутри LXC.
    """
    active = command("pct", "status", str(vmid)).stdout.strip().endswith("running")
    if pending and active:
        command("pct", "shutdown", str(vmid), "--timeout", "60")

    current = command("pct", "config", str(vmid)).stdout.splitlines()
    protected = "protection: 1" in current
    has_hook = f"hookscript: {HOOK_ID}" in current
    # Proxmox не разрешает pct set --mpN при protection=1.
    # Защита нужна и после установки, поэтому её нельзя снимать навсегда.
    restore_protection = protected and (bool(pending) or not has_hook)
    if restore_protection:
        command("pct", "set", str(vmid), "--protection", "0")
    try:
        # При ошибке настройки LXC остаётся остановленным: не запускать
        # контейнер с неполным набором постоянных подключений.
        for item in pending:
            spec = (
                f"{source_path(volumes, item)},mp={item['target']},"
                f"ro={int(item['read_only'])}"
            )
            command("pct", "set", str(vmid), f"--{item['slot']}", spec)
        ensure_startup_hook(vmid, config)
    finally:
        if restore_protection:
            # Выполняется и при ошибке pct set или установки сценария запуска.
            command("pct", "set", str(vmid), "--protection", "1")

    # Предыдущая неудачная попытка могла оставить LXC остановленным.
    # Команда deploy должна вернуть его в рабочее состояние перед Ansible.
    if not command("pct", "status", str(vmid)).stdout.strip().endswith("running"):
        command("pct", "start", str(vmid))

    for item in mounts:
        test = command(
            "pct", "exec", str(vmid), "--",
            "mountpoint", "-q", item["target"], check=False,
        )
        if test.returncode:
            raise StorageError(
                f"LXC {vmid}: {item['target']} не подключён после настройки"
            )


def main() -> int:
    """Подготовить постоянные тома по проверенному описанию гостя.

    Проверки выполняются до изменения LVM и подключений. Повторный запуск
    переиспользует существующие тома; частично выполненная настройка
    требует повторной проверки состояния, а не удаления данных.
    """
    vmid = int(sys.argv[1])
    config = json.load(sys.stdin)
    vg, pool, volumes, mounts = validate(vmid, config)
    pending = existing_mount_config(vmid, volumes, mounts)
    for volume in volumes.values():
        device, uuid = ensure_volume(vg, pool, volume)
        ensure_host_mount(device, uuid, volume)
    ensure_directories(volumes, mounts)
    configure_lxc(vmid, volumes, mounts, pending, config)
    print(f"[ОК] LXC {vmid}: тома подключены, каталоги готовы")
    return 0


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[2] in {"pre-start", "post-start", "pre-stop", "post-stop"}:
            if sys.argv[2] == "pre-start":
                verify_guest_start(int(sys.argv[1]))
            raise SystemExit(0)
        raise SystemExit(main())
    except (StorageError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"[ОШИБКА] Подготовка постоянного хранилища: {exc}", file=sys.stderr)
        raise SystemExit(1)
