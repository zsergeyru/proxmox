"""Python-реализация настройки infra-manager.

Пакет выполняет подготовку LXC 910, настройку проекта Semaphore, status и
проверку PVE API. Совместимые shell-файлы сохранены только как тонкие wrappers,
чтобы внешний bootstrap-контракт и установленные команды не менялись.
"""

from __future__ import annotations

__all__ = ["InfraManagerError"]

from .common import InfraManagerError
