"""Dispatch physical-host operations through allowlisted repository driver modules."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from .common import InfraManagerError
from .physical_hosts import PhysicalHost


def _load_driver(repo_root: Path, driver: str, *, validation_only: bool = False):
    """Load audited code from a driver name, not an arbitrary YAML file path."""
    if not isinstance(driver, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", driver):
        raise InfraManagerError("Некорректный идентификатор драйвера")
    path = repo_root / "scripts" / "infra-manager" / "drivers" / f"{driver}.py"
    # Isolated YAML validation tests may build a temporary manifest tree.
    # Only validation may use the already installed/audited driver module.
    if not path.exists() and validation_only:
        path = Path(__file__).resolve().parents[1] / "drivers" / f"{driver}.py"
    if path.is_symlink() or not path.is_file():
        raise InfraManagerError(f"Драйвер {driver!r} для физической машины не установлен")
    spec = importlib.util.spec_from_file_location(
        f"physical_host_driver_{driver.replace('-', '_')}", path,
    )
    if spec is None or spec.loader is None:
        raise InfraManagerError("Не удалось прочитать драйвер физического сервера")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_driver_storage(repo_root: Path, driver: str, storage: object) -> None:
    """Ask the selected driver to verify its own disk-role constraints."""
    module = _load_driver(repo_root, driver, validation_only=True)
    validator = getattr(module, "validate_storage", None)
    if not callable(validator):
        raise InfraManagerError("У драйвера нет обязательной проверки хранилища")
    validator(storage)


def run_host_driver(repo_root: Path, operation: str, host: PhysicalHost) -> int:
    """Execute a physical-host operation in the validated driver module."""
    module = _load_driver(repo_root, host.driver)
    executor = getattr(module, "execute", None)
    if not callable(executor):
        raise InfraManagerError("Драйвер не предоставил операцию execute")
    return executor(repo_root, operation, host)
