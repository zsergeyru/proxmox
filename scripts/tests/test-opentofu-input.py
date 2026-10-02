#!/usr/bin/env python3

from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "guests" / "render-opentofu-input.py"

spec = importlib.util.spec_from_file_location("render_opentofu_input", MODULE_PATH)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

MODULE_ROOT = ROOT / "scripts" / "infra-manager"
sys.path.insert(0, str(MODULE_ROOT))
from infra_manager import opentofu as opentofu_module

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

# 910 имеет обычный LXC-профиль, но принадлежит отдельному начальному контуру.
assert "910" in guests
assert guests["910"]["type"] == "lxc"
assert guests["910"]["pve_management"] is False

main_scope = module.build_payload(ROOT, exclude_vmids={910})["guests"]
assert "910" not in main_scope

bootstrap_scope = module.build_payload(ROOT, only_vmids={910})["guests"]
assert set(bootstrap_scope) == {"910"}

with tempfile.TemporaryDirectory() as tmp:
    fake = Path(tmp)
    guest_dir = fake / "infrastructure/guests/920-infra-manager"
    guest_dir.mkdir(parents=True)
    (guest_dir / "guest.yaml").write_text(
        "vmid: 920\n"
        "name: infra-manager\n"
        "role: infra-manager\n",
        encoding="utf-8",
    )
    captured: dict[str, set[int] | None] = {}

    def fake_prepare_input(
        repo_root: Path,
        *,
        only_vmids: set[int] | None = None,
        exclude_vmids: set[int] | None = None,
    ):
        assert repo_root == fake
        captured["only"] = only_vmids
        captured["exclude"] = exclude_vmids
        return Path("/tmp/opentofu"), {"guests": {}}

    with (
        patch.object(opentofu_module, "_prepare_input", side_effect=fake_prepare_input),
        patch.object(opentofu_module, "_opentofu_env", return_value={}),
    ):
        opentofu_module.prepare_workspace(fake)

    assert captured["only"] is None
    assert captured["exclude"] == {920}

for vmid, guest in guests.items():
    assert str(guest["vmid"]) == vmid
    assert guest["type"] in {"vm", "lxc"}
    assert guest["network"]["bridge"] == "vmbr0"
    assert guest["resources"]["disk_storage"] == "local-lvm"
    ipv4 = guest["network"]["ipv4"]
    assert ipv4 == "dhcp" or "/" in ipv4

print("[ОК] OpenTofu input contract")
