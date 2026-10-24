"""Dispatch physical-host operations through allowlisted repository driver modules."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .common import InfraManagerError
from .physical_hosts import PhysicalHost


def run_host_driver(repo_root: Path, operation: str, host: PhysicalHost) -> int:
    """Select a driver by the validated host.yaml management.driver identifier."""
    driver = host.driver
    if not isinstance(driver, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", driver):
        raise InfraManagerError("Некорректный идентификатор драйвера")
    path = repo_root / "scripts" / "infra-manager" / "drivers" / f"{driver}.py"
    if path.is_symlink() or not path.is_file():
        raise InfraManagerError(f"Драйвер {driver!r} для физической машины не установлен")
    spec = importlib.util.spec_from_file_location(
        f"physical_host_driver_{driver.replace('-', '_')}", path,
    )
    if spec is None or spec.loader is None:
        raise InfraManagerError("Не удалось прочитать драйвер физического сервера")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    executor = getattr(module, "execute", None)
    if not callable(executor):
        raise InfraManagerError("Драйвер не предоставил операцию execute")
    return executor(repo_root, operation, host)
