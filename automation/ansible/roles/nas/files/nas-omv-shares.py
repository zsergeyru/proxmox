#!/usr/bin/env python3
"""Неразрушающая регистрация общих папок NAS средствами OpenMediaVault.

Пользователей не создаёт и не меняет. Чужие одноимённые папки и общие
ресурсы не переписывает. Никогда не редактирует /etc/samba/smb.conf.
"""

from __future__ import annotations

import argparse
import json
import re
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
        # Только постоянные имена операций, без stdout/stderr и аргументов:
        # сообщение попадёт в консоль оператора через безопасный callback.
        stages = {
            ("omv-confdbadm", "read", "conf.system.sharedfolder"): "read-folders",
            ("omv-confdbadm", "read", "conf.service.smb.share"): "read-shares",
            ("omv-confdbadm", "read", "conf.service.smb"): "read-settings",
            ("omv-confdbadm", "update", "conf.system.sharedfolder"): "create-folder",
            ("omv-confdbadm", "update", "conf.service.smb.share"): "create-share",
            ("omv-confdbadm", "update", "conf.service.smb"): "enable-smb",
            ("omv-salt", "deploy", "run"): "apply-samba",
        }
        stage = stages.get(tuple(args[:3]), "unknown")
        raise ShareError(f"NAS_SMB_STEP={stage} RC={result.returncode}")
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


def validated_folders(value: object) -> tuple[tuple[str, str, str], ...]:
    """Read only declared relative SMB folders; reject aliases and duplicates."""
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ShareError("Не задан однозначный список папок в provision.yaml")
    declared: list[tuple[str, str, str]] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "volume", "directory"}:
            raise ShareError("Неизвестная структура объявления SMB")
        name, volume, directory = row["name"], row["volume"], row["directory"]
        if (
            not all(isinstance(x, str) for x in (name, volume, directory))
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", name)
            or volume not in ("shared", "data")
            or not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", directory)
        ):
            raise ShareError("Недопустимое имя или файловый том SMB")
        declared.append((name, volume, directory))
    if (len({row[0] for row in declared}) != len(declared)
            or len({row[1:] for row in declared}) != len(declared)):
        raise ShareError("Повторяются имена или расположения SMB")
    return tuple(declared)


def validated_access(value: object, plan: tuple[tuple[str, str, str], ...]) -> None:
    """Allow only named-user authenticated read/write on declared folders."""
    names = {name for name, _role, _directory in plan}
    if (not isinstance(value, dict) or set(value) != {"mode", "shares"}
            or value["mode"] != "named_users"
            or not isinstance(value["shares"], dict)
            or set(value["shares"]) != names
            or any(rights != {"access": "read_write"}
                   for rights in value["shares"].values())):
        raise ShareError("SMB: требуется named_users/read_write для всех папок")


def plan_shares(
    shared: str, data: str,
    existing_folders: list[dict[str, object]],
    existing_smb: list[dict[str, object]],
    folders_plan: tuple[tuple[str, str, str], ...],
    access: dict,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Согласовать новые записи до любых изменений OMV."""

    validated_access(access, folders_plan)
    mapping = {"shared": shared, "data": data}
    missing_folders: list[dict[str, object]] = []
    missing_smb: list[dict[str, object]] = []
    # Сначала собрать полное соответствие, отклоняя конфликты.
    by_name: dict[str, dict[str, object]] = {}
    for name, role, directory in folders_plan:
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
                # OMV 8 ожидает объект со списком privilege, не JSON-массив.
                "privileges": {"privilege": []},
            }
            missing_folders.append(folder)
        by_name[name] = folder

    for name, _role, _directory in folders_plan:
        folder_ref = str(by_name[name]["uuid"])
        smb_items = [
            item for item in existing_smb
            if item.get("sharedfolderref") == folder_ref
        ]
        if len(smb_items) > 1:
            raise ShareError(f"Несколько SMB-ресурсов используют папку {name}")
        if smb_items:
            item = smb_items[0]
            if (item.get("guest") != "no"
                    or item.get("readonly") is not False
                    or item.get("enable") is not True):
                if not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    str(item.get("uuid", "")),
                ):
                    raise ShareError(f"Нет подтверждённого UUID ресурса: {name}")
                # Preserve all unrelated OMV options; update only access.
                missing_smb.append({
                    **item, "guest": "no", "readonly": False, "enable": True,
                })
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


def apply(shared: str, data: str, shared_mount: str, data_mount: str,
          declarations: object, access: object) -> None:
    """Сверить фактические каталоги и изменить только отсутствующие записи."""

    folders_plan = validated_folders(declarations)
    validated_access(access, folders_plan)
    roots = {"shared": shared_mount, "data": data_mount}
    for _name, role, directory in folders_plan:
        root = Path(roots[role])
        if not root.is_mount() or root.is_symlink():
            raise ShareError(f"Том {root} не подключён: регистрация SMB остановлена")
        folder = root / directory
        if not folder.is_dir() or folder.is_symlink():
            raise ShareError(f"Нет подготовленного каталога: {folder}")

    folders = records("conf.system.sharedfolder")
    smb = records("conf.service.smb.share")
    missing_folders, missing_smb = plan_shares(
        shared, data, folders, smb, folders_plan, access,
    )

    global_settings = json.loads(
        command("omv-confdbadm", "read", "conf.service.smb")
    )
    if not isinstance(global_settings, dict):
        raise ShareError("Не найдены общие настройки SMB OpenMediaVault")

    enabled_before = bool(global_settings.get("enable"))
    if not enabled_before:
        # В OMV conf.service.smb включает вложенный список shares.share.
        # Обновление старого снимка ПОСЛЕ создания записей стерло бы их.
        # Сначала включаем службу, затем создаём недостающие SMB-ресурсы.
        global_settings["enable"] = True
        command(
            "omv-confdbadm", "update", "conf.service.smb",
            json.dumps(global_settings, ensure_ascii=False),
        )

    for item in missing_folders:
        # Произвольный UUID для новой записи OMV трактует как ссылку на
        # существующую запись. Отсутствующий UUID получает специальный
        # OMV_CONFIGOBJECT_NEW_UUID по умолчанию; база назначает реальный UUID.
        command(
            "omv-confdbadm", "update", "conf.system.sharedfolder",
            json.dumps({k: v for k, v in item.items() if k != "uuid"},
                       ensure_ascii=False),
        )

    # Между созданием папок и SMB ОБЯЗАТЕЛЬНО читаем действительные UUID,
    # выданные OMV. Если запуск ранее оборвался, повтор не создаёт дубликаты.
    if missing_folders:
        folders = records("conf.system.sharedfolder")
    _, missing_smb = plan_shares(
        shared, data, folders, smb, folders_plan, access,
    )
    known_uuids = {str(item.get("uuid")) for item in smb}
    for item in missing_smb:
        # An existing managed share must be UPDATED by its genuine OMV UUID.
        # New shares omit UUID and receive one from the OMV database.
        is_existing = str(item["uuid"]) in known_uuids
        payload = item if is_existing else {
            k: v for k, v in item.items() if k != "uuid"
        }
        command(
            "omv-confdbadm", "update", "conf.service.smb.share",
            json.dumps(payload, ensure_ascii=False),
        )
    if missing_folders or missing_smb or not enabled_before:
        command("omv-salt", "deploy", "run", "samba")
        print("SMB_CHANGED: согласованы ресурсы OMV")
    print("[ОК] Папки SMB: вход по учётным записям, чтение и запись")


def main() -> int:
    """Разобрать параметры управляющей программы и вернуть безопасную ошибку."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--shares-json", required=True)
    parser.add_argument("--access-json", required=True)
    parser.add_argument("--shared-ref", required=True)
    parser.add_argument("--data-ref", required=True)
    parser.add_argument("--shared-mount", required=True)
    parser.add_argument("--data-mount", required=True)
    args = parser.parse_args()
    try:
        apply(
            args.shared_ref, args.data_ref, args.shared_mount, args.data_mount,
            json.loads(args.shares_json), json.loads(args.access_json),
        )
    except (OSError, ValueError, ShareError) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
