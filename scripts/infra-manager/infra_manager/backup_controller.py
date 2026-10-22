"""Расписание и зашифрованные копии под управлением управляющего LXC.

PVE выполняет только проверку подключений и выдачу защищённого снимка.
restic и запись через SFTP выполняются исключительно внутри управляющего гостя.
"""

from __future__ import annotations

import argparse
import hashlib
import fcntl
from datetime import UTC, datetime
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from .common import InfraManagerError
from .pve_host import _ssh_command
from .operation_store import OperationStore, _atomic_json
from .settings import PATHS
from .volume_backup import load_jobs, due as volume_due

POLICY = PATHS.repo_root / "infrastructure/hosts/pve/backup.yaml"
HOST_HELPER = "/usr/local/lib/infra-manager/backup/backup-export.py"
DESTINATIONS = {
    "fast-ssd": ("/mnt/pve/backup-fast", "ext4", "infra-core"),
    "nas-backup": ("/mnt/pve/nas-backup", "nfs4", "pve-infra-core"),
}


def policy() -> tuple[dict, dict[str, str]]:
    """Загрузить только единое описание backup.yaml, не локальные настройки."""
    if POLICY.is_symlink() or not POLICY.is_file():
        raise InfraManagerError("Нет описания backup.yaml из рабочей копии проекта")
    raw = yaml.safe_load(POLICY.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise InfraManagerError("Неверная версия backup.yaml")
    if raw.get("host") != "pve" or raw.get("job") != "infra-core":
        raise InfraManagerError("Неверный контур резервирования")
    destinations = raw.get("destinations")
    if not isinstance(destinations, list) or len(destinations) != 2:
        raise InfraManagerError("backup.yaml должен описывать SSD и NAS")
    verified = {}
    for dest in destinations:
        if not isinstance(dest, dict):
            raise InfraManagerError("Некорректное назначение резервирования")
        name = dest.get("id")
        if name not in DESTINATIONS or name in verified:
            raise InfraManagerError("Неизвестное или дублирующееся назначение")
        path, filesystem, repository = DESTINATIONS[name]
        if (dest.get("mountpoint"), dest.get("filesystem"),
                dest.get("repository")) != (path, filesystem, repository):
            raise InfraManagerError("Изменение защищённого назначения запрещено")
        identity = dest.get("uuid") or dest.get("source")
        if name == "fast-ssd" and not identity:
            # Идентичность физического диска не закрепляется в Git навечно.
            # Принятая через штатный PVE API запись хранится в постоянном
            # состоянии 910 и сверяется с реальным PVE перед каждой копией.
            from .pve_backup_ssd import _read_state, _state_path
            saved = _read_state(_state_path())
            identity = saved["uuid"] if saved else None
        if isinstance(identity, str) and identity:
            verified[name] = identity
    if not verified:
        raise InfraManagerError(
            "Нет подтверждённого SSD или NAS; резервирование ожидает настройки"
        )
    # Проверить новое объявление заранее, чтобы неизвестный том не запускался.
    load_jobs(PATHS.repo_root, raw)
    return raw, verified


PVE_CHECK_ERRORS = {
    "unseal OpenBao": "PVE: не подготовлен unseal OpenBao",
    "аварийный GitHub-ключ": "PVE: не подготовлен аварийный GitHub-ключ",
    "AppRole снимка OpenBao": "PVE: не подготовлен AppRole снимка OpenBao",
    "помощник Raft-снимка": "PVE: не подготовлен помощник Raft-снимка",
    "идентичность PVE-гостя 910": "PVE: не подтверждена идентичность гостя 910",
}


def _pve_check_error(stderr: bytes) -> str:
    """Разрешать в журнал только статические описания. Сырой stderr не показывать."""
    if len(stderr) > 512:
        return "PVE отверг проверку источников резервирования"
    message = stderr.decode("utf-8", errors="replace").strip()
    prefix = "[ОШИБКА] Архив не выдан: Не подготовлен PVE-only компонент: "
    if message.startswith(prefix):
        return PVE_CHECK_ERRORS.get(
            message[len(prefix):], "PVE отверг проверку PVE-only состояния"
        )
    if message == "[ОШИБКА] Архив не выдан: Неполное PVE-only состояние для резервирования":
        return "PVE: не хватает обязательного PVE-only компонента резервирования"
    return "PVE отверг проверку источников или носителей резервирования"


def _remote(node: str, action: str, payload: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    """Ограниченный вызов PVE helper без произвольного удалённого shell."""
    # Двоичный ответ (в том числе ключ в /run) не проходит через
    # текстовый CommandRunner, который предназначен для обычных заданий.
    process = subprocess.Popen(
        _ssh_command(node, ("python3", HOST_HELPER, action)),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE if action == "check" else subprocess.DEVNULL,
    )
    try:
        output, stderr = process.communicate(payload, timeout=90)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise InfraManagerError("PVE helper превысил время ответа") from exc
    if process.returncode:
        if action == "check":
            raise InfraManagerError(_pve_check_error(stderr or b""))
        raise InfraManagerError(f"PVE не подтвердил действие резервирования: {action}")
    return subprocess.CompletedProcess(process.args, process.returncode, output)


def _source_node() -> str:
    """Получить имя PVE из уже доверенной операторской конфигурации."""
    from .operator import _pve_node

    node = _pve_node()
    if not node or any(ch.isspace() for ch in node):
        raise InfraManagerError("Не удалось определить узел PVE")
    return node


def _credentials(node: str, directory: Path, verified: dict[str, str],
                 *, create: bool) -> Path:
    """Получить ключ по доверенному SSH только после проверки двух носителей."""
    action = "password" if create else "password-existing"
    secret = _remote(node, action, json.dumps(verified).encode()).stdout
    if not 32 <= len(secret) <= 256 or b"\x00" in secret:
        raise InfraManagerError("Не получен безопасный пароль резервирования")
    name = directory / "restic-password"
    with name.open("xb") as stream:
        stream.write(secret)
    name.chmod(0o600)
    return name


RESTIC_STAGES = {
    "init": "создание репозитория",
    "snapshots": "проверка доступа к репозиторию",
    "backup": "запись архива",
    "check": "проверка целостности архива",
}


def _diagnostic_tail(stream, limit: int = 16384) -> bytes:
    """Прочитать хвост временного stderr, не публикуя его содержимое."""
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - limit))
    return stream.read(limit)


def _restic_cause(stderr: bytes) -> str:
    """Выводить только фиксированные причины, а не секреты из stderr."""
    value = stderr.decode("utf-8", errors="replace").lower()
    if "no space left on device" in value:
        return "на носителе закончилось место"
    if "read-only file system" in value:
        return "файловая система доступна только для чтения"
    if "cache" in value and ("denied" in value or "permission" in value):
        return "недоступен кеш restic"
    if "permission denied" in value or "access denied" in value:
        return "нет прав на файл или каталог"
    if "wrong password" in value or "password is incorrect" in value:
        return "не подходит пароль существующего хранилища"
    if "repository is already locked" in value or "unable to create lock" in value:
        return "хранилище заблокировано другой операцией"
    if ("connection reset" in value or "connection refused" in value
            or "broken pipe" in value or "ssh: " in value
            or "sftp: " in value or "unexpected eof" in value):
        return "ошибка SSH/SFTP-соединения"
    if "unable to read" in value or "failed to read" in value:
        return "не удалось прочитать входные данные"
    return "неопределённая ошибка restic (требуется дополнительная проверка)"


EXPORT_FAILURES = {
    "Согласованный снимок OpenBao Raft не получен": "PVE не смог создать снимок OpenBao Raft",
    "Не удалось собрать согласованный архив PVE": "PVE не смог собрать TAR-архив",
    "Потоковый снимок VM 100 не завершился": "PVE не смог завершить снимок VM 100",
    "Гость VM 100 недоступен": "VM 100 недоступна на PVE",
    "Выполняются изменения инфраструктуры": "PVE заблокировал снимок во время изменений",
    "Самообновление infra-manager ещё выполняется или требует проверки":
        "PVE заблокировал снимок во время самообновления 910",
    "Отсутствует обязательный источник резервирования":
        "на PVE отсутствует обязательный источник архива",
}


def _export_cause(stderr: bytes, returncode: int) -> str:
    """Распознавать только сообщения PVE helper, игнорируя произвольный SSH stderr."""
    prefix = "[ОШИБКА] Архив не выдан: "
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            known = EXPORT_FAILURES.get(line[len(prefix):])
            if known:
                return known
    if returncode == 255:
        return "SSH-соединение с PVE прервано"
    return "PVE не подтвердил полный поток снимка"


def _restic(node: str, destination: str, key: Path, args: list[str],
            *, source=None, volume_id: str | None = None) -> None:
    """Запустить restic в 910; в журнал вывести только код и безопасную категорию."""
    mountpoint, _filesystem, repository = DESTINATIONS[destination]
    path = f"volumes/{volume_id}" if volume_id is not None else repository
    backend = f"sftp:root@{node}:{mountpoint}/{path}"
    ssh_args = [
        "-i", str(PATHS.pve_host_private_key),
        "-o", f"UserKnownHostsFile={PATHS.pve_host_known_hosts}",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "BatchMode=yes",
        "-o", "IdentitiesOnly=yes",
    ]
    cmd = [
        "restic", "-o", f"sftp.args={shlex.join(ssh_args)}",
        "-r", backend, *args,
    ]
    env = dict(os.environ, RESTIC_PASSWORD_FILE=str(key))
    # Временный файл предотвращает deadlock при большом stderr.
    # Сырой stderr не уходит в systemd-журнал, файл закрывается при выходе.
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        process = subprocess.Popen(
            cmd, stdin=source, stdout=subprocess.DEVNULL,
            stderr=diagnostics, env=env,
        )
        try:
            code = process.wait(timeout=7200)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait()
            raise InfraManagerError("Операция restic превысила время выполнения") from exc
        if code:
            stage = RESTIC_STAGES.get(args[0] if args else "", "операция restic")
            cause = _restic_cause(_diagnostic_tail(diagnostics))
            raise InfraManagerError(
                f"restic: {stage} на {destination} завершилась ошибкой "
                f"(код {code}; причина: {cause})"
            )


def _stream_snapshot(node: str, verified: dict[str, str],
                     key: Path, destination: str, *, vm100: bool = False) -> None:
    """Проверить коды Restic и источника: нельзя считать оборванный поток успехом."""
    payload = json.dumps(verified, separators=(",", ":")).encode()
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        export = subprocess.Popen(
            _ssh_command(node, ("python3", HOST_HELPER,
                                "vm100" if vm100 else "export")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics,
        )
        try:
            assert export.stdin is not None
            try:
                export.stdin.write(payload)
                export.stdin.close()
            except BrokenPipeError as exc:
                raise InfraManagerError("PVE преждевременно закрыл поток снимка") from exc
            assert export.stdout is not None
            restic_failure = None
            try:
                try:
                    _restic(
                        node, destination, key,
                        ["backup", "--stdin", "--stdin-filename",
                         "haos-100.vma.zst" if vm100 else "infra-core.tar",
                         "--tag", "haos" if vm100 else "infra-core",
                         "--tag", "vm-100" if vm100 else "pve"],
                        source=export.stdout,
                    )
                except InfraManagerError as exc:
                    restic_failure = exc
            finally:
                export.stdout.close()
            try:
                export_code = export.wait(timeout=2500)
            except subprocess.TimeoutExpired as exc:
                raise InfraManagerError("PVE превысил время выдачи потока снимка") from exc
            if export_code:
                # SIGPIPE после отказа Restic — следствие, не первопричина.
                if restic_failure is not None and export_code in (-13, 141):
                    raise restic_failure
                cause = _export_cause(_diagnostic_tail(diagnostics), export_code)
                raise InfraManagerError(
                    f"Экспорт PVE завершился с ошибкой (код {export_code}; причина: {cause})"
                ) from restic_failure
            if restic_failure is not None:
                raise restic_failure
        finally:
            if export.poll() is None:
                export.kill()
                export.wait()



# Никогда не публикуем произвольный stderr PVE: только заранее известные
# названия этапов. Сообщения из PVE-helper могут содержать закрытые пути.
VOLUME_EXPORT_FAILURES = {
    "Не удалось проверить состояние гостя": "Не удалось проверить состояние LXC",
    "Не удалось согласованно остановить гостя": "Не удалось остановить LXC перед снимком",
    "После снимка не удалось восстановить работу LXC": "Не удалось возобновить LXC после снимка",
    "Создание временного LVM snapshot не удалось": "LVM не создал временный snapshot",
    "LVM не смог активировать временный thin snapshot":
        "Не удалось активировать LVM thin snapshot (lvchange -ay -K)",
    "Временный LVM snapshot не активирован":
        "Временный LVM thin snapshot остаётся неактивным",
    "Блочное устройство временного LVM snapshot не появилось":
        "Не появилось блочное устройство временного LVM thin snapshot",
    "Снимок тома не удалось подключить только для чтения":
        "Не удалось подключить LVM snapshot только для чтения",
    "Не удалось подтвердить временный LVM snapshot":
        "PVE не подтвердил наличие временного thin snapshot",
    "Временный LVM snapshot не подтверждён как доступный для записи":
        "PVE не подтвердил writable thin snapshot выбранного тома",
    "Временный снимок не подтверждён в режиме read-only":
        "PVE не подтвердил чтение из временного snapshot",
    "Снимок тома не удалось подключить только для чтения: нет места для журнала ext4 в LVM snapshot":
        "LVM thin-pool: не хватает места на воспроизведение журнала ext4",
    "Снимок тома не удалось подключить только для чтения: ext4 требует восстановления журнала или orphan-файлов":
        "ext4 отказал в восстановлении журнала временного снимка",
    "Снимок тома не удалось подключить только для чтения: временная точка подключения занята":
        "Временная точка подключения snapshot занята",
    "Снимок тома не удалось подключить только для чтения: PVE отклонил монтирование снимка":
        "Недостаточно прав для подключения временного snapshot",
    "Снимок тома не удалось подключить только для чтения: не удаётся прочитать суперблок ext4":
        "Не удалось прочитать суперблок временного snapshot",
    "Снимок тома не удалось подключить только для чтения: несовместимый тип файловой системы снимка":
        "Временный snapshot не опознан как ext4",
    "Снимок тома не удалось подключить только для чтения: неопределённая ошибка монтирования снимка":
        "Неизвестная ошибка mount временного snapshot",
    "Поток резервируемого тома оборвался": "PVE прервал TAR-поток тома",
    "Не удалось отключить временный снимок":
        "Не удалось размонтировать временный LVM snapshot",
    "Не удалось удалить временный LVM snapshot":
        "Не удалось удалить временный LVM snapshot",
    "Неизвестный потребитель резервируемого тома":
        "Обнаружен неописанный LXC-потребитель тома",
    "Не удалось получить перечень LXC": "PVE не смог получить список LXC",
    "Не удалось проверить подключения LXC":
        "PVE не смог проверить подключения LXC",
    "Том не подтверждён по точке подключения":
        "Точка подключения рабочего тома не подтверждена",
    "Рабочий LVM-том не найден": "Не найден рабочий LVM-том",
    "Том не принадлежит pve/data": "Том находится вне pve/data",
    "Источник файловой системы не соответствует LVM":
        "Реальный источник тома отличается от описанного",
    "LVM thin-pool близок к заполнению, снимок запрещён":
        "Недостаточно безопасного резерва в LVM thin-pool",
    "Нет достоверных данных о LVM thin-pool":
        "Не удалось определить заполнение LVM thin-pool",
    "Выполняются изменения инфраструктуры":
        "LVM snapshot заблокирован изменением инфраструктуры",
    "Самообновление infra-manager ещё выполняется или требует проверки":
        "LVM snapshot заблокирован незавершённым обновлением 910",
    "Нет общей блокировки изменений":
        "На PVE отсутствует общая блокировка изменений",
}


def _volume_export_cause(stderr: bytes, returncode: int) -> str:
    prefix = "[ОШИБКА] Архив не выдан: "
    for line in stderr.decode("utf-8", errors="replace").splitlines():
        if line.startswith(prefix):
            return VOLUME_EXPORT_FAILURES.get(
                line[len(prefix):], "PVE отказал в создании снимка тома"
            )
    if returncode == 255:
        return "SSH-соединение с PVE прервано"
    return "PVE не завершил передачу данных тома"


def _stream_volume(node: str, verified: dict[str, str], key: Path,
                   destination: str, volume: dict) -> None:
    """Передать только подтверждённый том: LXC возобновляются до записи Restic."""
    payload = json.dumps(
        {"destinations": {destination: verified[destination]}, "volume": volume},
        separators=(",", ":"),
    ).encode()
    with tempfile.TemporaryFile(mode="w+b") as diagnostics:
        export = subprocess.Popen(
            _ssh_command(node, ("python3", HOST_HELPER, "volume")),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=diagnostics,
        )
        try:
            assert export.stdin is not None
            try:
                export.stdin.write(payload)
                export.stdin.close()
            except (BrokenPipeError, OSError) as exc:
                raise InfraManagerError(
                    "PVE преждевременно закрыл поток задания тома"
                ) from exc
            assert export.stdout is not None
            failure = None
            try:
                try:
                    _restic(
                        node, destination, key,
                        ["backup", "--stdin", "--stdin-filename",
                         volume["id"] + ".tar", "--tag",
                         "volume:" + volume["id"]],
                        source=export.stdout, volume_id=volume["id"],
                    )
                except InfraManagerError as exc:
                    failure = exc
            finally:
                export.stdout.close()
            try:
                code = export.wait(timeout=2500)
            except subprocess.TimeoutExpired as exc:
                raise InfraManagerError("PVE превысил время выдачи снимка тома") from exc
            if code:
                cause = _volume_export_cause(_diagnostic_tail(diagnostics), code)
                raise InfraManagerError(
                    f"Том {volume['id']}: экспорт PVE завершился с ошибкой "
                    f"(код {code}; причина: {cause})"
                )
            if failure is not None:
                raise failure
        finally:
            if export.poll() is None:
                export.kill()
                export.wait()


def _backup_volumes(node: str, verified: dict[str, str], key: Path,
                    store: OperationStore, jobs: list[dict]) -> dict[str, str]:
    """Независимые суточные задания; фиксировать успех каждого носителя."""
    issues = {}
    for volume in jobs:
        name = volume["id"]
        targets = list(verified)
        if not targets or not volume_due(store, name, targets):
            continue
        try:
            raw_state = _remote(node, "volume-repositories", json.dumps({
                "destinations": verified, "volume": volume,
            }, separators=(",", ":")).encode()).stdout
            state = json.loads(raw_state)
            if not isinstance(state, dict) or set(state) != set(verified):
                raise InfraManagerError("PVE не подтвердил репозитории тома")
        except (InfraManagerError, ValueError) as exc:
            issues[name] = str(exc)
            continue
        from .operation_store import _read
        record_path = store.settings / ("backup-volume-" + name + ".json")
        try:
            prior = _read(record_path)
            history = prior.get("destination_dates", {})
            history = {k: v for k, v in history.items()
                       if k in DESTINATIONS and isinstance(v, str)}
        except (InfraManagerError, OSError, ValueError, AttributeError):
            history = {}
        failures = {}
        for target in targets:
            try:
                if state[target] not in {"missing", "existing"}:
                    raise InfraManagerError("Вместо Restic-репозитория неизвестные данные")
                if state[target] == "missing":
                    _remote(node, "volume-prepare", json.dumps({
                        "destinations": {target: verified[target]}, "volume": volume,
                    }, separators=(",", ":")).encode())
                    _restic(node, target, key, ["init"], volume_id=name)
                _restic(node, target, key, ["snapshots", "--json"], volume_id=name)
                _stream_volume(node, verified, key, target, volume)
                _restic(node, target, key, ["check", "--read-data-subset=1%"],
                        volume_id=name)
                history[target] = datetime.now(UTC).date().isoformat()
            except InfraManagerError as exc:
                failures[target] = str(exc)
        stamp = datetime.now(UTC).isoformat()
        _atomic_json(record_path, {
            "schema_version": 1, "volume_id": name,
            "created_utc": stamp if any(
                x == datetime.now(UTC).date().isoformat() for x in history.values()
            ) else None,
            "destination_dates": history,
            "destinations": sorted(history),
            "failed_destinations": sorted(failures),
            "last_errors": {target: reason[:400]
                            for target, reason in sorted(failures.items())},
            "verified": "restic-check-read-data-subset-1-percent",
            "restore_tested": False,
        })
        print(f"[ИНФО] Том {name}: успешные назначения "
              + (", ".join(sorted(set(history) - set(failures))) or "нет"))
        if failures:
            # Все ошибки преобразованы исполнителями в безопасные категории;
            # показываем конкретный этап без сырого stderr или секретов.
            issues[name] = "; ".join(
                target + ": " + reason for target, reason in sorted(failures.items())
            )
    return issues


def _vm100_due(store: OperationStore, identity: str) -> bool:
    """Ежедневная копия HAOS не дублируется при каждом запуске таймера."""
    saved = store.settings / "backup-vm100.json"
    if saved.is_symlink() or not saved.is_file():
        return True
    try:
        record = json.loads(saved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return not (
        isinstance(record, dict)
        and record.get("date") == datetime.now(UTC).date().isoformat()
        and record.get("media_fingerprint") == identity
        and record.get("verified") is True
    )


def execute(*, run: bool = False) -> None:
    """Резервировать на всех фактически доступных и проверенных носителях.

    Ни одна ошибка SSD/NAS не направляет архив на корневую файловую систему
    PVE и не отменяет попытку использовать второе независимое назначение.
    """
    description, declared = policy()
    volume_jobs = load_jobs(PATHS.repo_root, description)
    if shutil.which("restic") is None:
        raise InfraManagerError("restic не установлен внутри управляющего гостя")
    node = _source_node()
    verified: dict[str, str] = {}
    problems: dict[str, str] = {}
    if "fast-ssd" in declared:
        from .pve import PveClient
        from .pve_backup_ssd import reconcile_backup_ssd
        try:
            uuid = reconcile_backup_ssd(PveClient(), node, interactive=False)
            if uuid != declared["fast-ssd"]:
                raise InfraManagerError("Идентичность SSD не совпадает с подтверждённой")
        except InfraManagerError as exc:
            problems["fast-ssd"] = str(exc)
    for name, identity in declared.items():
        if name in problems:
            continue
        try:
            _remote(node, "check", json.dumps({name: identity}).encode())
        except InfraManagerError as exc:
            problems[name] = str(exc)
        else:
            verified[name] = identity
    if not verified:
        raise InfraManagerError(
            "Нет доступного и проверенного места резервирования: "
            + "; ".join(f"{name}: {reason}" for name, reason in problems.items())
        )
    with tempfile.TemporaryDirectory(prefix="infra-backup-", dir="/run") as path:
        directory = Path(path)
        directory.chmod(0o700)
        state = json.loads(_remote(
            node, "repositories", json.dumps(verified).encode()
        ).stdout)
        if not isinstance(state, dict) or set(state) != set(verified):
            raise InfraManagerError("PVE не подтвердил состояние выбранных restic-хранилищ")
        # Запрещено перегенерировать пароль поверх существующего архива.
        create_password = run and all(value == "missing" for value in state.values())
        key = _credentials(node, directory, verified, create=create_password)
        for name in verified:
            if state[name] == "existing":
                _restic(node, name, key, ["snapshots", "--json"])
            elif state[name] != "missing":
                raise InfraManagerError("На носителе обнаружены неизвестные данные restic")
        if not run:
            if any(value != "existing" for value in state.values()):
                raise InfraManagerError("Создание restic-хранилища возможно только при резервировании")
            print("[ОК] Подтверждённые хранилища restic доступны")
            if len(verified) != len(DESTINATIONS):
                print("[ПРЕДУПРЕЖДЕНИЕ] Второе независимое назначение недоступно")
            return
        for name in verified:
            if state[name] == "missing":
                try:
                    _restic(node, name, key, ["init"])
                except InfraManagerError as exc:
                    problems[name] = str(exc)
        core_saved: dict[str, str] = {}
        for name, identity in verified.items():
            if name in problems:
                continue
            try:
                _restic(node, name, key, ["snapshots", "--json"])
                _stream_snapshot(node, {name: identity}, key, name)
                _restic(node, name, key, ["check", "--read-data-subset=1%"])
                core_saved[name] = identity
            except InfraManagerError as exc:
                problems[name] = str(exc)
        if not core_saved:
            # Независимые данные не теряем из-за сбоя Raft/PVE-config.
            if run and volume_jobs:
                volume_problems = _backup_volumes(
                    node, verified, key, OperationStore(), volume_jobs,
                )
                problems.update({"volume:" + name: reason
                                 for name, reason in volume_problems.items()})
            raise InfraManagerError(
                "Все задания архивирования PVE завершились ошибками: "
                + "; ".join(problems.values())
            )
        store = OperationStore()
        created = datetime.now(UTC)
        _atomic_json(store.settings / "backup-last-core.json", {
            "schema_version": 1,
            "created_utc": created.isoformat(),
            "destinations": list(core_saved),
            "verified": "restic-check-read-data-subset-1-percent",
            "includes_vm100": False,
            "restore_tested": False,
        })
        # Суточная копия VM 100 требуется при изменении состава носителей
        # или старении последней успешной проверенной копии.
        identity = hashlib.sha256(
            json.dumps(core_saved, sort_keys=True).encode()
        ).hexdigest()
        vm_saved = dict(core_saved)
        if _vm100_due(store, identity):
            vm_saved = {}
            for name, value in core_saved.items():
                try:
                    _stream_snapshot(node, {name: value}, key, name, vm100=True)
                    _restic(node, name, key, ["check", "--read-data-subset=1%"])
                    vm_saved[name] = value
                except InfraManagerError as exc:
                    problems[name] = str(exc)
            if vm_saved:
                verified_hash = hashlib.sha256(
                    json.dumps(vm_saved, sort_keys=True).encode()
                ).hexdigest()
                _atomic_json(store.settings / "backup-vm100.json", {
                    "date": datetime.now(UTC).date().isoformat(),
                    "destinations": list(vm_saved),
                    "media_fingerprint": verified_hash,
                    "verified": True,
                })
        if vm_saved:
            _atomic_json(store.settings / "backup-last-full.json", {
                "schema_version": 1,
                "created_utc": created.isoformat(),
                "destinations": list(vm_saved),
                "includes_vm100": True,
                "restore_tested": False,
            })
        if run and volume_jobs:
            volume_problems = _backup_volumes(node, verified, key, store, volume_jobs)
            problems.update({"volume:" + name: reason
                             for name, reason in volume_problems.items()})
        if len(vm_saved) < len(DESTINATIONS):
            print("[ПРЕДУПРЕЖДЕНИЕ] Вторая независимая копия отсутствует")
        if problems:
            raise InfraManagerError(
                "Некоторые назначения резервирования недоступны: "
                + "; ".join(f"{name}: {reason}" for name, reason in problems.items())
            )
        print("[ОК] Архивы PVE/910 и актуальная копия VM 100 "
              "сохранены на подтверждённых носителях; восстановление не испытано")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InfraManagerError("Нужны полномочия системной службы управляющего гостя")
        store = OperationStore()
        lock = store.settings / "backup.lock"
        descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InfraManagerError("Резервирование уже выполняется") from exc
            execute(run=args.run)
        finally:
            os.close(descriptor)
        return 0
    except (InfraManagerError, OSError, ValueError, yaml.YAMLError) as exc:
        print(f"[ОЖИДАНИЕ] Резервирование: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
