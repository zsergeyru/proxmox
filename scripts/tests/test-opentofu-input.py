#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "guests" / "render-opentofu-input.py"

spec = importlib.util.spec_from_file_location("render_opentofu_input", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

payload = module.build_payload(ROOT)

assert payload["format_version"] == 1
guests = payload["guests"]

# Управляемый гость с profile обязан попасть во вход OpenTofu.
assert "109" in guests
assert guests["109"]["name"] == "network-gateway"
assert guests["109"]["type"] == "vm"
assert guests["109"]["network"]["ipv4"] == "192.168.1.9/16"

# AI Control разворачивается как обычная VM, но не должен попадать
# в собственную область управления managed.
assert "410" in guests
assert guests["410"]["name"] == "ai-control"
assert guests["410"]["type"] == "vm"
assert guests["410"]["pve_management"] is False
assert "bootstrap" not in guests["410"]

# Область состояния можно ограничить конкретным VMID/CTID.
only_410 = module.build_payload(ROOT, only_vmids={410})["guests"]
assert set(only_410) == {"410"}

without_410 = module.build_payload(ROOT, exclude_vmids={410})["guests"]
assert "410" not in without_410
assert "109" in without_410

# Описательный объект без profile не участвует в универсальном развёртывании.
assert "201" not in guests

# Специальный 910 имеет guest.yaml без profile и не должен попасть в OpenTofu.
assert "910" not in guests

for vmid, guest in guests.items():
    assert str(guest["vmid"]) == vmid
    assert guest["type"] in {"vm", "lxc"}
    assert guest["network"]["bridge"] == "vmbr0"
    assert guest["resources"]["disk_storage"] == "local-lvm"
    ipv4 = guest["network"]["ipv4"]
    assert ipv4 == "dhcp" or "/" in ipv4

print("[ОК] OpenTofu input contract")
