"""Штатные операции над физическими серверами без создания объектов PVE."""

from __future__ import annotations

from pathlib import Path

from .common import InfraManagerError, console
from .physical_hosts import list_physical_hosts, load_physical_host


SUPPORTED_HOST_OPERATIONS = frozenset({"status", "inspect", "deploy", "repair"})


def list_hosts(repo_root: Path) -> int:
    """Показать перечень физических машин и готовность настроек связи."""

    hosts = list_physical_hosts(repo_root)
    if not hosts:
        console.warning("Машинные описания физических серверов отсутствуют")
        return 0
    for host in hosts:
        address = host.address or "адрес не указан"
        console.info(f"{host.name}: физический сервер, {address}")
    return 0


def run_physical_host_operation(repo_root: Path, operation: str, name: str) -> int:
    """Проверить описание цели; не выполнять необъявленных разрушительных действий."""

    if operation not in SUPPORTED_HOST_OPERATIONS:
        raise InfraManagerError(f"Недопустимая операция физического сервера: {operation}")
    host = load_physical_host(repo_root, name)
    console.info(f"Физический сервер: {host.name}")
    if host.address is None:
        raise InfraManagerError(
            f"{host.directory / 'host.yaml'}: укажите подтверждённый сетевой адрес NAS; "
            "автоматический поиск соседних машин запрещён"
        )
    if host.ssh_host_fingerprint is None:
        raise InfraManagerError(
            f"{host.directory / 'host.yaml'}: не указан подтверждённый SSH-отпечаток "
            "физического сервера; нельзя доверять найденному ключу автоматически"
        )

    # До готовности доверенного доступа и алгоритма защиты дисков ни одна
    # изменяющая операция не имитирует успешное развёртывание.
    raise InfraManagerError(
        f"Операция {operation} для {name} ещё не реализована: "
        "пока доступна проверка машинного описания; диски не изменены"
    )
