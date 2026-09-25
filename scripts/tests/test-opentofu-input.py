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

# Обычный управляемый гость обязан попасть в полный итоговый набор.
assert "110" in guests
assert guests["110"]["name"] == "network-gateway"
assert guests["110"]["type"] == "vm"
assert guests["110"]["network"]["ipv4"] == "192.168.1.10/16"

# AI Control разворачивается как обычная VM, но не должен попадать
# в собственную область управления managed.
assert "410" in guests
assert guests["410"]["name"] == "ai-control"
assert guests["410"]["type"] == "vm"
assert guests["410"]["pve_management"] is False
assert "bootstrap" not in guests["410"]

# 910 теперь описывается обычным LXC-профилем. Отдельный владелец state
# определяется фильтром запуска, а не отсутствием profile.
assert "910" in guests
assert guests["910"]["name"] == "infra-manager"
assert guests["910"]["type"] == "lxc"
assert guests["910"]["features"] == ["container-host"]
assert guests["910"]["pve_management"] is False
assert guests["910"]["network"]["ipv4"] == "192.168.9.10/16"
assert guests["910"]["boot"]["onboot"] is True
assert guests["910"]["boot"]["order"] == 10
assert guests["910"]["boot"]["startup_delay_seconds"] == 30

main_payload = module.build_payload(ROOT, exclude_vmids={910})
assert "910" not in main_payload["guests"]
assert "410" in main_payload["guests"]

bootstrap_payload = module.build_payload(ROOT, include_vmids={910})
assert set(bootstrap_payload["guests"]) == {"910"}

# Описательный объект без profile не участвует в универсальном развёртывании.
assert "210" not in guests

for vmid, guest in guests.items():
    assert str(guest["vmid"]) == vmid
    assert guest["type"] in {"vm", "lxc"}
    assert guest["network"]["bridge"] == "vmbr0"
    assert guest["resources"]["disk_storage"] == "local-lvm"
    ipv4 = guest["network"]["ipv4"]
    assert ipv4 == "dhcp" or "/" in ipv4

print("[ОК] OpenTofu input contract")
