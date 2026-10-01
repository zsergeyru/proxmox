#!/usr/bin/env python3
"""Контрактные проверки централизованного межмашинного SSH."""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

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

    assert policy.machine_identity_vmids == frozenset({410, 910})
    assert policy.project_repository_read_vmids == frozenset({410, 910})
    assert not policy.project_repository_read_allowed(311)
    assert policy.project_repository_read_allowed(410)
    assert not policy.project_repository_read_allowed(109)
    assert policy.machine_principal(410) == "guest-410"
    assert policy.machine_principal(910) == "guest-910"

    # Пока управляемые гости отсутствуют, 410 не получает исходящих SSH-целей.
    assert policy.targets_for(410) == ()
    assert MachineSshEdge(410, 910) not in policy.machine_ssh_edges

    # 910 имеет доступ только к фактически развёрнутому 410.
    assert policy.targets_for(910) == (410,)
    assert policy.authorized_principals(410) == ("guest-910",)
    assert 910 not in policy.targets_for(910)

    # На 910 machine-principal 410 не должен разрешаться.
    assert "guest-410" not in policy.authorized_principals(910)

    # Удаление правила из access.yaml должно отзывать principal на цели.
    with tempfile.TemporaryDirectory() as temporary:
        temp_root = Path(temporary)
        shutil.copytree(
            ROOT / "infrastructure/guests",
            temp_root / "infrastructure/guests",
        )
        security_dir = temp_root / "infrastructure/security"
        security_dir.mkdir(parents=True)
        access_data = yaml.safe_load(
            (ROOT / "infrastructure/security/access.yaml").read_text(
                encoding="utf-8"
            )
        )
        access_data["rules"] = [
            rule
            for rule in access_data["rules"]
            if rule.get("id") != "infra-manager-linux-guests"
        ]
        (security_dir / "access.yaml").write_text(
            yaml.safe_dump(
                access_data,
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        revoked = load_access_policy(temp_root)
        assert 410 not in revoked.targets_for(910)
        assert revoked.project_repository_read_allowed(410)
        assert "guest-910" not in revoked.authorized_principals(410)

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
    assert "/etc/ssh/authorized_principals" in PREPARE_TARGET_CODE
    assert 'principal_dir / "root"' in PREPARE_TARGET_CODE
    assert "authorized_keys" not in PREPARE_TARGET_CODE
    assert 'content = "\\n".join(["root", *sorted(set(principals))])' in PREPARE_TARGET_CODE
    assert "old == text" in PREPARE_TARGET_CODE
    assert "os.replace(temporary, path)" in PREPARE_TARGET_CODE

    print("Machine SSH contract tests passed.")


if __name__ == "__main__":
    main()
