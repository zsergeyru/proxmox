"""Python-реализация настройки infra-manager.

Пакет уже выполняет подготовку и обновление LXC 910 через тонкий setup.sh.
Semaphore API и status пока остаются отдельными shell-сценариями и будут
переноситься следующими этапами без изменения внешнего bootstrap-контракта.
"""

from __future__ import annotations

__all__ = ["InfraManagerError"]

from .common import InfraManagerError
