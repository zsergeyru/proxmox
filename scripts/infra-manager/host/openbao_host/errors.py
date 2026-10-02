"""Ошибки PVE-команды управления OpenBao."""


class OpenBaoHostError(RuntimeError):
    """Ошибка управления OpenBao на доверенном PVE-хосте."""
