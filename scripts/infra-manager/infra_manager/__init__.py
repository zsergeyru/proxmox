"""Python-реализация настройки infra-manager.

Пакет выполняет подготовку LXC 910, настройку проекта Semaphore, status и
проверку PVE API. Shell-файлы сохранены только там, где они нужны внешнему
bootstrap-контракту или как стабильные установленные команды.
"""

from __future__ import annotations

__all__ = ["InfraManagerError"]

from .common import InfraManagerError
