"""Fail-closed быстрый deploy управляющего гостя: web и избранные Python-модули.

Манифест Git-файлов записывается только после успешной полной активации
и обновляется после проверенного быстрого обновления. Никаких внешних
аргументов с командами, произвольными файлами и путями нет.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .common import InfraManagerError, console, read_env_file, run
from .operation_lock import project_checkout_lock
from .operation_store import OperationStore
from .settings import PATHS

BASELINE = "fast-deploy-baseline.json"
MAX_BASELINE = 1024 * 1024
MAX_FILE = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")
WEB = "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web/"
PY = "scripts/infra-manager/infra_manager/"
WEB_FILES = frozenset({"Dockerfile", ".dockerignore", "app.py", "admin.py", "diagnostics.js"})
PY_FILES = frozenset({"admin_socket.py", "backup_overview.py", "pve.py"})
PASSIVE = (".github/", "scripts/tests/")
VERSIONS = PATHS.compose_dir / ".versions.env"


class FastRollbackUnconfirmed(InfraManagerError):
    """Откат не доказан: запретить автоматический повторный deploy."""


@dataclass(frozen=True)
class Plan:
    mode: str
    revision: str
    web: tuple[str, ...] = ()
    python: tuple[str, ...] = ()
    reason: str = ""


def _git(repo: Path, *args: str) -> str:
    result = run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise InfraManagerError("Не удалось получить проверенное состояние Git")
    return result.stdout


def _inventory(repo: Path) -> tuple[str, dict[str, str]]:
    rev = _git(repo, "rev-parse", "--verify", "HEAD").strip()
    if not SHA.fullmatch(rev):
        raise InfraManagerError("Неверный Git SHA")
    files = {}
    for entry in _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD").split("\0"):
        if not entry:
            continue
        try:
            header, name = entry.split("\t", 1)
            mode, kind, digest = header.split(" ")
        except ValueError as exc:
            raise InfraManagerError("Повреждён Git tree") from exc
        if (kind not in {"blob", "commit"} or not SHA.fullmatch(digest) or not name
                or "\n" in name or name in files):
            raise InfraManagerError("Некорректный Git tree")
        files[name] = mode + ":" + digest
        if len(files) > 8000:
            raise InfraManagerError("Слишком большой Git tree")
    if not files:
        raise InfraManagerError("Пустой Git tree")
    return rev, files


def _read(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_uid != 0:
            raise InfraManagerError("Недоверенный файл: " + path.name)
        return os.read(descriptor, limit + 1)
    finally:
        os.close(descriptor)


def _atomic(path: Path, content: bytes, mode: int = 0o600) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir() or path.is_symlink():
        raise InfraManagerError("Недоверенный целевой каталог")
    temporary = path.parent / ("." + path.name + "." + uuid.uuid4().hex)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _baseline(store: OperationStore) -> dict:
    value = json.loads(_read(store.settings / BASELINE, MAX_BASELINE))
    if (not isinstance(value, dict) or set(value) != {"schema", "revision", "files"}
            or value["schema"] != 1 or not SHA.fullmatch(str(value["revision"]))
            or not isinstance(value["files"], dict) or not 0 < len(value["files"]) <= 8000):
        raise InfraManagerError("Повреждённый baseline")
    if any(not isinstance(k, str) or not isinstance(v, str)
           or not re.fullmatch(r"[0-9]{6}:[0-9a-f]{40}", v)
           for k, v in value["files"].items()):
        raise InfraManagerError("Повреждены контрольные суммы baseline")
    return value


def attest(repo: Path = PATHS.repo_root, *, store: OperationStore | None = None,
           versions: Path | None = None) -> None:
    if os.geteuid() != 0:
        raise InfraManagerError("Только root может подтвердить установленную версию")
    rev, files = _inventory(repo)
    if read_env_file(versions or VERSIONS).get("INFRA_GIT_REVISION") != rev:
        raise InfraManagerError("Git и установленный release не совпадают")
    data = (json.dumps({"schema": 1, "revision": rev, "files": files},
                       sort_keys=True) + "\n").encode()
    if len(data) > MAX_BASELINE:
        raise InfraManagerError("Превышен лимит контрольных сумм")
    _atomic((store or OperationStore()).settings / BASELINE, data)


def plan(repo: Path = PATHS.repo_root, *, store: OperationStore | None = None,
         versions: Path | None = None) -> Plan:
    rev, new = _inventory(repo)
    try:
        old = _baseline(store or OperationStore())
        if read_env_file(versions or VERSIONS).get("INFRA_GIT_REVISION") != old["revision"]:
            return Plan("full", rev, reason="Текущая версия не подтверждена")
    except (InfraManagerError, ValueError, OSError, TypeError):
        return Plan("full", rev, reason="Нет безопасной базовой версии")
    web, python = [], []
    for filename in sorted(set(old["files"]) | set(new)):
        before, after = old["files"].get(filename), new.get(filename)
        if before == after:
            continue
        if (not after or not after.startswith("100644:")
                or (before is not None and not before.startswith("100644:"))):
            return Plan("full", rev, reason="Удаление или смена режима: " + filename)
        if filename.startswith(WEB) and filename[len(WEB):] in WEB_FILES:
            web.append(filename)
        elif filename.startswith(PY) and filename[len(PY):] in PY_FILES:
            python.append(filename)
        elif filename.startswith(PASSIVE) or filename.endswith(".md"):
            continue
        else:
            return Plan("full", rev, reason="Критическая конфигурация: " + filename)
    return Plan("fast", rev, tuple(web), tuple(python))


def _digest(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode() + b"\0" + content,
    ).hexdigest()


def _destination(name: str) -> Path:
    if name.startswith(WEB) and name[len(WEB):] in WEB_FILES:
        return PATHS.compose_dir / "web" / name[len(WEB):]
    if name.startswith(PY) and name[len(PY):] in PY_FILES:
        return PATHS.python_install_root / "infra_manager" / name[len(PY):]
    raise InfraManagerError("Неизвестный файл быстрого обновления")


def _command(argv: list[str], title: str) -> str:
    started = time.monotonic()
    outcome = run(argv, capture_output=True, check=False)
    console.timing(title, time.monotonic() - started, failed=outcome.returncode != 0)
    if outcome.returncode:
        raise InfraManagerError(title + ": код " + str(outcome.returncode))
    return outcome.stdout.strip()


def _compose(*args: str, build: bool = False) -> None:
    compose = "docker-compose.yml" if build else "docker-compose-standalone.yml"
    _command(["docker", "compose", "--env-file", str(VERSIONS),
              "-f", str(PATHS.compose_dir / compose), *args], "Выборочный Docker web")


def apply(selected: Plan, verify: Callable[[], bool], *,
          repo: Path = PATHS.repo_root, store: OperationStore | None = None) -> None:
    if os.geteuid() != 0 or selected.mode != "fast":
        raise InfraManagerError("Быстрый deploy запрещён")
    state = store or OperationStore()
    backups: list[tuple[Path, bytes | None]] = []
    previous_image = None
    web_changed = bool(selected.web)
    python_changed = bool(selected.python)
    with project_checkout_lock(exclusive=True):
        if plan(repo, store=state) != selected:
            raise InfraManagerError("Git изменился во время выбора плана")
        _, files = _inventory(repo)
        baseline = _baseline(state)["files"]
        staged = []
        # Прежде всего проверяем ВСЕ источники и старые файлы, без записей.
        for filename in (*selected.web, *selected.python):
            destination = _destination(filename)
            source = repo / filename
            if source.is_symlink() or destination.is_symlink():
                raise InfraManagerError("Символьные ссылки запрещены")
            new_data = _read(source, MAX_FILE)
            if _digest(new_data) != files[filename].split(":")[1]:
                raise InfraManagerError("Изменён исходник после Git checkout: " + filename)
            old_data = (_read(destination, MAX_FILE) if destination.exists() else None)
            if filename in baseline:
                if old_data is None or _digest(old_data) != baseline[filename].split(":")[1]:
                    raise InfraManagerError("Установленный файл отличается от Git: " + filename)
            elif old_data is not None:
                raise InfraManagerError("Добавленный Git-файл уже существует: " + filename)
            staged.append((destination, old_data, new_data))
        old_env = _read(VERSIONS, 32768)
        text = old_env.decode("utf-8")
        if not re.search(r"(?m)^INFRA_GIT_REVISION=[0-9a-f]{40}$", text):
            raise InfraManagerError("Отсутствует установленный Git SHA")
        text = re.sub(r"(?m)^INFRA_GIT_REVISION=[0-9a-f]{40}$",
                      "INFRA_GIT_REVISION=" + selected.revision, text)
        if web_changed:
            web_rev = _git(repo, "rev-parse", "HEAD:" + WEB.rstrip("/")).strip()
            if not SHA.fullmatch(web_rev):
                raise InfraManagerError("Нет версии web")
            text, count = re.subn(r"(?m)^INFRA_WEB_CONTENT_REVISION=[0-9a-f]{40}$",
                                  "INFRA_WEB_CONTENT_REVISION=" + web_rev, text)
            if count != 1:
                raise InfraManagerError("Нет версии web в Compose")
            previous_image = _command(
                ["docker", "image", "inspect", "--format", "{{.Id}}", "infra-web:v1"],
                "Проверка текущего образа web",
            )
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", previous_image):
                raise InfraManagerError("Нет проверенного предыдущего web-образа")
        try:
            for destination, old_data, new_data in staged:
                _atomic(destination, new_data, 0o644)
                backups.append((destination, old_data))
            if text.encode() != old_env:
                _atomic(VERSIONS, text.encode(), 0o644)
                backups.append((VERSIONS, old_env))
            if python_changed:
                _command(["systemctl", "restart", "infra-manager-portal-gateway.service"],
                         "Перезапуск системного посредника")
                _command(["systemctl", "is-active", "--quiet",
                          "infra-manager-portal-gateway.service"],
                         "Проверка системного посредника")
            if web_changed:
                _compose("build", "web", build=True)
                _compose("up", "-d", "--no-deps", "--no-build", "web")
            if not verify():
                raise InfraManagerError("Выборочное обновление не прошло status 910")
            attest(repo, store=state)
        except (InfraManagerError, OSError, ValueError, UnicodeError, KeyboardInterrupt):
            console.warning("Быстрый deploy не принят: возврат файлов и контейнера")
            failed = False
            for destination, old_data in reversed(backups):
                try:
                    if old_data is None:
                        destination.unlink(missing_ok=True)
                    else:
                        _atomic(destination, old_data, 0o644)
                except (OSError, InfraManagerError):
                    failed = True
            if web_changed and previous_image:
                try:
                    _command(["docker", "image", "tag", previous_image, "infra-web:v1"],
                             "Возврат прежнего web-образа")
                    _compose("up", "-d", "--no-deps", "--no-build", "--force-recreate", "web")
                except InfraManagerError:
                    failed = True
            if python_changed:
                try:
                    _command(["systemctl", "restart", "infra-manager-portal-gateway.service"],
                             "Возврат системного посредника")
                except InfraManagerError:
                    failed = True
            if failed:
                raise FastRollbackUnconfirmed("Откат быстрого deploy НЕ подтверждён; требуется диагностика 910") from None
            raise


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("attest",))
    args = parser.parse_args()
    if args.action == "attest":
        attest()
        print("[ОК] Установленная Git-версия подтверждена для будущих быстрых обновлений")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
