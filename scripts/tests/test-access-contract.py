#!/usr/bin/env python3
"""Контрактные проверки единого infrastructure/security/access.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
ACCESS = ROOT / "infrastructure/security/access.yaml"
SCHEMA = ROOT / "infrastructure/schemas/access.schema.yaml"
GUESTS = ROOT / "infrastructure/guests"


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
        resource="guest",
        target="project:linux-guests",
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
    require_rule(
        data,
        subject="guest:410",
        service="ssh",
        resource="guest",
        target="pool:managed",
        permission="connect-root",
    )

    # Старые management-флаги пока остаются, но их смысл уже описан access.yaml.
    for manifest in sorted(GUESTS.glob("*/guest.yaml")):
        guest = load_yaml(manifest)
        management = guest.get("management", [])
        if not isinstance(management, list):
            continue
        subject = f"guest:{guest['vmid']}"
        if "ssh_identity" in management:
            require_rule(
                data,
                subject=subject,
                service="ssh",
                resource="identity",
                target="self",
                permission="issue",
            )
        if "project_repo_read" in management:
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
        "private_key:",
        "token_secret:",
    ):
        assert forbidden not in rendered, forbidden

    print("Access contract tests passed.")


if __name__ == "__main__":
    main()
