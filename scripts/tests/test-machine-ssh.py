#!/usr/bin/env python3
"""Контрактные проверки централизованного межмашинного SSH."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))

from infra_manager.access import MachineSshEdge, load_access_policy
from infra_manager.machine_ssh import (
    AUTHORIZED_PRINCIPALS_CONFIG,
    MACHINE_CERT,
    MACHINE_CLIENT_CONFIG,
    MACHINE_KEY,
    PREPARE_SOURCE_CODE,
    PREPARE_TARGET_CODE,
    _host_pattern,
)


def main() -> None:
    policy = load_access_policy(ROOT)

    assert policy.machine_identity_vmids == frozenset({311, 410, 910})
    assert policy.machine_principal(410) == "guest-410"
    assert policy.machine_principal(910) == "guest-910"

    # 410 имеет доступ только к управляемой области, но не к самому 910.
    assert 910 not in policy.targets_for(410)
    assert MachineSshEdge(410, 910) not in policy.machine_ssh_edges

    # 311 наследует pve_management=true и имеет provision.yaml.
    assert 311 in policy.targets_for(410)
    assert policy.authorized_principals(311) == (
        "guest-410",
        "guest-910",
    )

    # 910 управляет Linux-гостями проекта отдельной машинной identity.
    assert 410 in policy.targets_for(910)
    assert 311 in policy.targets_for(910)
    assert 910 not in policy.targets_for(910)

    # На 910 machine-principal 410 не должен разрешаться.
    assert "guest-410" not in policy.authorized_principals(910)

    assert _host_pattern("192.168.0.0/16") == "192.168.*"

    assert str(MACHINE_KEY) == "/etc/proxmox-guest/ssh/machine_ed25519"
    assert str(MACHINE_CERT).endswith("machine_ed25519-cert.pub")
    assert str(MACHINE_CLIENT_CONFIG).endswith(
        "/ssh_config.d/40-project-machine.conf"
    )
    assert str(AUTHORIZED_PRINCIPALS_CONFIG).endswith(
        "/sshd_config.d/42-project-machine-principals.conf"
    )

    # Закрытый ключ создаётся на исходном госте и не передаётся в 910.
    assert "ssh-keygen" in PREPARE_SOURCE_CODE
    assert "machine_ed25519" in PREPARE_SOURCE_CODE
    assert "print(public_key" in PREPARE_SOURCE_CODE

    # Целевая сторона использует AuthorizedPrincipalsFile, а не authorized_keys.
    assert "AuthorizedPrincipalsFile" in PREPARE_TARGET_CODE
    assert "/etc/ssh/authorized_principals/root" in PREPARE_TARGET_CODE
    assert "authorized_keys" not in PREPARE_TARGET_CODE

    print("Machine SSH contract tests passed.")


if __name__ == "__main__":
    main()
