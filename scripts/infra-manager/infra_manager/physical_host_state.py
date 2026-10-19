"""Адреса постоянных подтверждений физических серверов, отдельно от блокировок.

Старое содержимое из locks/ сохраняется при обновлении ролью Ansible.
До завершения этого переноса управление останавливается, а не считает
уже настроенный NAS новым и не повторяет парольный SSH-вход.
"""

from __future__ import annotations

import re
from pathlib import Path

from .common import InfraManagerError
from .settings import PATHS

_NAME = re.compile(r"^[a-z][a-z0-9-]*$")


def checked_record_path(
    name: str, *, storage: bool = False, data_root: Path | None = None,
) -> Path:
    """Выбрать только подтверждённую новую запись или отказать при старой."""

    if not _NAME.fullmatch(name):
        raise InfraManagerError(f"Недопустимое имя физической машины: {name!r}")
    root = data_root if data_root is not None else PATHS.data_dir
    filename = f"{name}-storage.json" if storage else f"{name}.json"
    old_dir = root / "locks" / "physical-hosts"
    new_dir = root / "physical-hosts"
    previous = old_dir / filename
    current = new_dir / filename
    if new_dir.is_symlink():
        # На управляющем госте это ожидаемая ссылка на bind mount PVE.
        # В infra-runtime тот же каталог подключён Docker напрямую.
        expected = Path("/mnt/persistent-state/physical-hosts")
        if not expected.is_dir() or new_dir.resolve() != expected.resolve():
            raise InfraManagerError(
                "Каталог подтверждений указывает не на постоянное состояние PVE"
            )
    if old_dir.is_symlink():
        raise InfraManagerError("Неожиданная ссылка вместо прежних подтверждений")
    if not new_dir.is_dir():
        raise InfraManagerError(
            "Отсутствует постоянный каталог physical-hosts; "
            "сначала обновите управляющий гость и проверьте подключение состояния"
        )
    if current.is_symlink() or previous.is_symlink():
        raise InfraManagerError("Подмена файла подтверждений символьной ссылкой запрещена")
    if previous.exists():
        if not current.is_file():
            raise InfraManagerError(
                f"Старая запись {filename} ещё не перенесена. "
                "Обновите управляющий гость штатным deploy; повторное первоначальное доверие запрещено"
            )
        try:
            if previous.read_bytes() != current.read_bytes():
                raise InfraManagerError(
                    f"Записи {filename} в старой и новой папке не совпадают; "
                    "нужна ручная проверка, автоматическая замена запрещена"
                )
        except OSError as exc:
            raise InfraManagerError(f"Не удалось сверить подтверждение {filename}") from exc
    return current
