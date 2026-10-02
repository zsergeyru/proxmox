#!/usr/bin/env python3
"""Контрактные проверки базовой реализации 410 ai-control."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure" / "guests" / "410-ai-control"
GUEST_MANIFEST = GUEST / "guest.yaml"
PROVISION = GUEST / "provision.yaml"
PLAYBOOK = ROOT / "automation" / "ansible" / "playbooks" / "configure-guest.yml"
ROLE = ROOT / "automation" / "ansible" / "roles" / "ai_control" / "tasks" / "main.yml"
GUEST_LAYOUT = (
    ROOT / "automation" / "ansible" / "roles" / "guest_layout" / "tasks" / "main.yml"
)
UNIT = GUEST / "rootfs" / "etc" / "systemd" / "system" / "ai-control.service"


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


guest = load_yaml(GUEST_MANIFEST)
provision = load_yaml(PROVISION)

assert guest["vmid"] == 410
assert guest["name"] == "ai-control"
assert guest["role"] == "ai-control"
assert guest["pve_management"] is False

ai = provision["ai_control"]
assert ai["user"] == "ai-control"
assert ai["group"] == "ai-control"
assert ai["agent"]["primary"] == "hermes"
assert ai["agent"]["replaceable"] is True
assert ai["web"]["required"] is True

paths = ai["paths"]
expected_paths = {
    "root": "/opt/ai-control",
    "core": "/opt/ai-control/core",
    "agents": "/opt/ai-control/agents",
    "primary_agent": "/opt/ai-control/agents/hermes",
    "repositories": "/opt/ai-control/repos",
    "project_repository": "/opt/ai-control/repos/proxmox",
    "instructions": "/opt/ai-control/instructions",
    "tools": "/opt/ai-control/tools",
    "state": "/opt/ai-control/state",
    "configuration": "/etc/ai-control",
    "runtime": "/run/ai-control",
    "cache": "/var/cache/ai-control",
}
assert paths == expected_paths

executor = provision["integrations"]["infrastructure_executor"]
assert executor["role"] == "infra-manager"
assert executor["service"] == "semaphore"
assert "guest_vmid" not in executor

persistence = provision["persistence"]
assert persistence["backup_required"] == ["/opt/ai-control/state"]
assert "/etc/proxmox-guest/ssh" not in persistence["backup_required"]
assert "/run/ai-control" in persistence["ephemeral"]
assert "/var/cache/ai-control" in persistence["ephemeral"]

playbook_text = PLAYBOOK.read_text(encoding="utf-8")
assert "name: ai_control" in playbook_text
assert '(guest_manifest.role | default(\'\')) == "ai-control"' in playbook_text
assert "guest_manifest.vmid == 410" not in playbook_text
assert "guest_manifest.vmid | int == 410" not in playbook_text

role_text = ROLE.read_text(encoding="utf-8")
assert "provision.ai_control.paths" in role_text
assert "provision.ai_control.service.executable" in role_text
assert "guest_manifest.vmid" not in role_text
assert "guest_vmid" not in role_text
assert "910" not in role_text

guest_layout_text = GUEST_LAYOUT.read_text(encoding="utf-8")
assert "project_repository" not in guest_layout_text
assert "components.agents" not in guest_layout_text

unit_text = UNIT.read_text(encoding="utf-8")
assert "User=ai-control" in unit_text
assert "Group=ai-control" in unit_text
assert "ConditionPathIsExecutable=/opt/ai-control/core/bin/ai-control" in unit_text
assert "ExecStart=/opt/ai-control/core/bin/ai-control" in unit_text
assert "ProtectSystem=strict" in unit_text
assert "/opt/ai-control/repos/proxmox" in unit_text
assert "NoNewPrivileges=true" in unit_text

print("[ОК] AI Control base contract")
