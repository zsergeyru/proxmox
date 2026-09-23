"""Python-основа infra-manager.

Пакет пока не участвует в рабочем bootstrap 910. Он вводится поэтапно,
чтобы переносить существующую shell-логику без изменения внешнего контракта.
"""

from __future__ import annotations

__all__ = ["InfraManagerError"]

from .common import InfraManagerError
