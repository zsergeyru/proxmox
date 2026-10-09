#!/usr/bin/env python3
"""Контрактные проверки единого infrastructure/security/access.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
ACCESS = ROOT / "infrastructure/security/access.yaml"
SCHEMA = ROOT / "infrastructure/schemas/access.schema.yaml"


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def rules_for(data: dict, subject: str) -> list[dict]:
    return [
        rule
        for rule in data["rules"]
        if isinstance(rule, dict) and rule.get("subject") == subject
    ]


def require_rule(
    data: dict,
    *,
    subject: str,
    service: str,
    resource: str,
    target: str,
    permission: str,
) -> None:
    matches = [
        rule
        for rule in rules_for(data, subject)
        if rule.get("service") == service
        and rule.get("resource") == resource
        and target in rule.get("targets", [])
        and permission in rule.get("access", [])
    ]
    assert matches, (
        subject,
        service,
        resource,
        target,
        permission,
    )


def main() -> None:
    data = load_yaml(ACCESS)
    schema = load_yaml(SCHEMA)
    errors = list(Draft202012Validator(schema).iter_errors(data))
    assert not errors, [error.message for error in errors]

    ids = [rule["id"] for rule in data["rules"]]
    assert len(ids) == len(set(ids))

    # 910 — доверенный инфраструктурный исполнитель.
    for permission in ("read", "create", "clone", "configure", "power", "delete"):
        require_rule(
            data,
            subject="guest:910",
            service="pve-api",
            resource="guests",
            target="/vms",
            permission=permission,
        )
    require_rule(
        data,
        subject="guest:910",
        service="ssh",
        resource="host",
        target="host:pve",
        permission="connect-root",
    )
    require_rule(
        data,
        subject="guest:910",
        service="github",
        resource="repository",
        target="zsergeyru/proxmox",
        permission="read",
    )
    require_rule(
        data,
        subject="guest:910",
        service="ssh",
        resource="identity",
        target="self",
        permission="issue",
    )
    require_rule(
        data,
        subject="guest:910",
        service="ssh",
        resource="guest",
        target="guest:410",
        permission="connect-root",
    )

    # 410 не получает прямой административный доступ к PVE.
    assert not any(
        rule.get("service") == "pve-api"
        for rule in rules_for(data, "guest:410")
    )
    assert not any(
        rule.get("service") == "ssh"
        and "host:pve" in rule.get("targets", [])
        for rule in rules_for(data, "guest:410")
    )
    for target in (
        "opentofu-plan",
        "deploy-managed-guest",
        "verify-managed-guest",
    ):
        require_rule(
            data,
            subject="guest:410",
            service="semaphore",
            resource="infrastructure-task",
            target=target,
            permission="execute",
        )
    assert not any(
        rule.get("service") == "ssh"
        and rule.get("resource") == "guest"
        for rule in rules_for(data, "guest:410")
    )
    for target in (
        "guests", "status:all", "test:managed", "deploy:managed",
        "sync:managed", "repair:managed",
    ):
        require_rule(
            data, subject="guest:410", service="pve-operator",
            resource="guest-operation", target=target, permission="execute",
        )
    assert not rules_for(data, "guest:311")

    # SSH identity и Git read определяются только access.yaml.
    for vmid in (410,):
        subject = f"guest:{vmid}"
        require_rule(
            data,
            subject=subject,
            service="ssh",
            resource="identity",
            target="self",
            permission="issue",
        )
        require_rule(
            data,
            subject=subject,
            service="github",
            resource="repository",
            target="zsergeyru/proxmox",
            permission="read",
        )

    # PVE-only часть OpenBao описана логически, без путей хранения секретов.
    require_rule(
        data,
        subject="host:pve",
        service="openbao",
        resource="instance",
        target="openbao",
        permission="unseal",
    )
    require_rule(
        data,
        subject="host:pve",
        service="openbao",
        resource="credential",
        target="pve-api-infra-manager",
        permission="materialize",
    )
    require_rule(
        data,
        subject="host:pve",
        service="openbao",
        resource="ssh-ca",
        target="managed-host",
        permission="sign",
    )

    rendered = ACCESS.read_text(encoding="utf-8").lower()
    for forbidden in (
        "/run/",
        "/mnt/",
        "/etc/",
        "infra-secrets/",
        "unseal.key",
        "kv-access.json",
        "ssh-access.json",
        "operator-access.json",
        "private_key:",
        "token_secret:",
    ):
        assert forbidden not in rendered, forbidden

    print("Access contract tests passed.")


if __name__ == "__main__":
    main()
