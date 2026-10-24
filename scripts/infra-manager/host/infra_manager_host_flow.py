"""Small host preparation orchestrator kept outside the minimal PVE proxy."""

from __future__ import annotations

from infra_manager_nas_bootstrap import initial_host_access
from infra_manager_nas_storage import confirm_nas_storage
from infra_manager_host_credentials import prompt_physical_credentials


def prepare_physical_host(name: str, forward, error_type: type[Exception]) -> int:
    """Authenticate the physical host, verify disks, then ask declared secrets."""
    for step in (initial_host_access, confirm_nas_storage, prompt_physical_credentials):
        result = step(name, forward, error_type)
        if result:
            return result
    return 0
