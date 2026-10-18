#!/usr/bin/env python3
"""Неразрушающая регистрация общих папок NAS средствами OpenMediaVault.

Пользователей не создаёт и не меняет. Чужие одноимённые папки и общие
ресурсы не переписывает. Никогда не редактирует /etc/samba/smb.conf.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path


class ShareError(RuntimeError):
    """Невозможно применить описание SMB без риска задеть чужие данные."""


def command(*args: str) -> str:
    """Выполнить штатный инструмент OMV без интерпретатора оболочки."""

    result = subprocess.run(
        list(args), check=False, text=True, capture_output=True
    )
    if result.returncode:
        raise ShareError(
            f"Команда {args[0]} завершилась с кодом {result.returncode}"
        )
    return result.stdout


def records(model: str) -> list[dict[str, object]]:
    """Прочитать перечень записей из штатной базы OMV."""

    try:
        value = json.loads(command("omv-confdbadm", "read", model))
    except json.JSONDecodeError as exc:
        raise ShareError(f"Некорректный формат модели OMV: {model}") from exc
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not all(
        isinstance(item, dict) for item in value
    ):
        raise ShareError(f"Неизвестная структура модели OMV: {model}")
    return value


def _new_uuid() -> str:
    """Получить стандартный уникальный номер нового объекта OMV."""

    return str(uuid.uuid4())


FOLDERS = (
    ("shared", "shared", "shared"),
    ("movies", "shared", "movies"),
    ("music", "shared", "music"),
    ("data", "data", "data"),
)


def plan_shares(
    shared: str, data: str,
    existing_folders: list[dict[str, object]],
    existing_smb: list[dict[str, object]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Согласовать новые записи до любых изменений OMV."""

    mapping = {"shared": shared, "data": data}
    missing_folders: list[dict[str, object]] = []
    missing_smb: list[dict[str, object]] = []
    # Сначала собрать полное соответствие, отклоняя конфликты.
    by_name: dict[str, dict[str, object]] = {}
    for name, role, directory in FOLDERS:
        ref = mapping[role]
        if not ref:
            raise ShareError(f"Для роли {role} не назначен том OMV")
        folders = [
            item for item in existing_folders if item.get("name") == name
        ]
        if len(folders) > 1:
            raise ShareError(f"Дублируется общая папка OMV: {name}")
        if folders:
            folder = folders[0]
            if (
                folder.get("mntentref") != ref
                or str(folder.get("reldirpath", "")).strip("/") != directory
            ):
                raise ShareError(
                    f"Папка {name} уже существует с другим томом или путём"
                )
        else:
            # Нельзя создавать дубликат чужого каталога с другим именем.
            conflict = [
                item for item in existing_folders
                if item.get("mntentref") == ref
                and str(item.get("reldirpath", "")).strip("/") == directory
            ]
            if conflict:
                raise ShareError(f"Путь {directory} на томе {role} уже занят OMV")
            folder = {
                "uuid": _new_uuid(),
                "name": name,
                "comment": "Под управлением Infra Manager",
                "mntentref": ref,
                "reldirpath": directory + "/",
                "privileges": [],
            }
            missing_folders.append(folder)
        by_name[name] = folder

    for name, _role, _directory in FOLDERS:
        folder_ref = str(by_name[name]["uuid"])
        smb_items = [
            item for item in existing_smb
            if item.get("sharedfolderref") == folder_ref
        ]
        if len(smb_items) > 1:
            raise ShareError(f"Несколько SMB-ресурсов используют папку {name}")
        if smb_items:
            item = smb_items[0]
            if item.get("guest") != "no":
                raise ShareError(
                    f"У существующего ресурса {name} открыт гостевой доступ"
                )
            continue
        missing_smb.append({
            "uuid": _new_uuid(),
            "enable": True,
            "sharedfolderref": folder_ref,
            "comment": "Домашний NAS",
            "guest": "no",
            "readonly": False,
            "browseable": True,
            "recyclebin": False,
            "recyclemaxsize": 0,
            "recyclemaxage": 0,
            "hidedotfiles": True,
            "inheritacls": False,
            "inheritpermissions": True,
            "easupport": True,
            "storedosattributes": True,
            "hostsallow": "",
            "hostsdeny": "",
            "audit": False,
            "macoscompat": False,
            "transportencryption": False,
            "followsymlinks": False,
            "widelinks": False,
            "extraoptions": "",
        })
    return missing_folders, missing_smb


def apply(shared: str, data: str, shared_mount: str, data_mount: str) -> None:
    """Сверить фактические каталоги и изменить только отсутствующие записи."""

    for root, names in (
        (shared_mount, ("shared", "movies", "music")),
        (data_mount, ("data",)),
    ):
        if not Path(root).is_mount():
            raise ShareError(f"Том {root} не подключён: регистрация SMB остановлена")
        for name in names:
            folder = Path(root) / name
            if not folder.is_dir() or folder.is_symlink():
                raise ShareError(f"Нет подготовленного каталога: {folder}")

    folders = records("conf.system.sharedfolder")
    smb = records("conf.service.smb.share")
    missing_folders, missing_smb = plan_shares(shared, data, folders, smb)

    global_settings = json.loads(
        command("omv-confdbadm", "read", "conf.service.smb")
    )
    if not isinstance(global_settings, dict):
        raise ShareError("Не найдены общие настройки SMB OpenMediaVault")

    for item in missing_folders:
        command(
            "omv-confdbadm", "update", "conf.system.sharedfolder",
            json.dumps(item, ensure_ascii=False),
        )
    for item in missing_smb:
        command(
            "omv-confdbadm", "update", "conf.service.smb.share",
            json.dumps(item, ensure_ascii=False),
        )
    if not global_settings.get("enable"):
        global_settings["enable"] = True
        command(
            "omv-confdbadm", "update", "conf.service.smb",
            json.dumps(global_settings, ensure_ascii=False),
        )
    if missing_folders or missing_smb or not global_settings.get("enable"):
        command("omv-salt", "deploy", "run", "samba")
    print("[ОК] Папки SMB зарегистрированы в OpenMediaVault без гостевого доступа")


def main() -> int:
    """Разобрать доверенные параметры 910 и вернуть безопасную ошибку."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--shared-ref", required=True)
    parser.add_argument("--data-ref", required=True)
    parser.add_argument("--shared-mount", required=True)
    parser.add_argument("--data-mount", required=True)
    args = parser.parse_args()
    try:
        apply(
            args.shared_ref, args.data_ref, args.shared_mount, args.data_mount
        )
    except (OSError, ValueError, ShareError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
