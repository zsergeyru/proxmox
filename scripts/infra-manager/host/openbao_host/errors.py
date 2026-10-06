"""Ошибки PVE-команды управления OpenBao."""


class OpenBaoHostError(RuntimeError):
    """Ошибка управления OpenBao на доверенном PVE-хосте."""


class OpenBaoRaftInactiveError(OpenBaoHostError):
    """Разблокированный Raft-узел не смог стать активным."""
