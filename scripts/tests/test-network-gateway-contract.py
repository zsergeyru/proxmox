#!/usr/bin/env python3
"""Контрактные проверки 109 network-gateway без доступа к PVE."""

from __future__ import annotations

from pathlib import Path
import importlib.util
import tempfile

import yaml

ROOT = Path(__file__).resolve().parents[2]
GUEST_DIR = ROOT / "infrastructure/guests/109-network-gateway"
PLAYBOOK = ROOT / "automation/ansible/playbooks/configure-guest.yml"
ROLE = ROOT / "automation/ansible/roles/network_gateway"


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


guest = load_yaml(GUEST_DIR / "guest.yaml")
provision = load_yaml(GUEST_DIR / "provision.yaml")
playbook = load_yaml(PLAYBOOK)

assert guest["vmid"] == 109
assert guest["role"] == "network-gateway"
interfaces = guest["network"]["interfaces"]
assert [item["name"] for item in interfaces] == ["wan", "lan"]
assert interfaces[0]["ipv4"] == "10.0.0.2/30"
assert interfaces[0]["gateway"] == "10.0.0.1"
assert interfaces[1]["ipv4"] == "192.168.1.9/16"
assert interfaces[1]["management"] is True

assert provision["schema_version"] == 1
assert provision["guest_vmid"] == 109
assert provision["system"]["distribution"] == "debian"
assert str(provision["system"]["version"]) == "13"
assert provision["system"]["architecture"] == "amd64"

gateway = provision["network_gateway"]
assert gateway["interfaces"] == {"wan": "wan", "lan": "lan"}
assert gateway["forwarding"]["enabled"] is True
adguard = gateway["adguard_home"]
assert adguard["enabled"] is True
assert adguard["image"] == "adguard/adguardhome:v0.107.79"
assert adguard["web"]["port"] == 3000
assert adguard["dhcp"]["lease_seconds"] == 86400
assert adguard["interface"] == "lan"
assert adguard["domain"] == "home.arpa"
assert adguard["dhcp"]["enabled"] is True
assert adguard["dhcp"]["range"] == {
    "start": "192.168.1.100",
    "end": "192.168.1.250",
}
assert adguard["upstream"] == {"host": "127.0.0.1", "port": 6053}

smartdns = gateway["smartdns"]
assert smartdns["enabled"] is True
assert smartdns["version"] == "48.4"
assert len(smartdns["upstreams"]) >= 2
assert smartdns["listen"] == {"host": "127.0.0.1", "port": 6053}
assert smartdns["domain_sets"]["enabled"] is True

routing = gateway["routing"]
assert routing["default"] == "direct"
assert routing["sources"] == []
assert routing["update"]["keep_last_good"] is True
assert routing["targets"]["direct"]["type"] == "wan"
assert routing["targets"]["zapret"]["type"] == "nfqws2"

assert gateway["nfqws2"] == {"enabled": True, "target": "zapret"}
assert gateway["firewall"]["enabled"] is True
assert gateway["firewall"]["direct_wan_nat"] is False

required_packages = set(provision["system"]["required_packages"])
assert {"ca-certificates", "curl", "iproute2", "nftables", "wireguard-tools"}.issubset(
    required_packages
)
assert "dnsmasq" not in required_packages

tasks = playbook[0]["tasks"]
network_gateway_tasks = [
    task
    for task in tasks
    if task.get("ansible.builtin.include_role", {}).get("name")
    == "network_gateway"
]
assert len(network_gateway_tasks) == 1
assert "network-gateway" in str(network_gateway_tasks[0].get("when", []))

required_files = [
    ROLE / "tasks/main.yml",
    ROLE / "handlers/main.yml",
    ROLE / "templates/sysctl.conf.j2",
    ROLE / "templates/nftables.conf.j2",
    ROLE / "templates/routing-sources.yaml.j2",
    ROLE / "tasks/routing_sources.yml",
    ROLE / "files/network-gateway-lists.py",
    ROLE / "tasks/smartdns.yml",
    ROLE / "templates/smartdns.conf.j2",
    ROLE / "tasks/adguard_home.yml",
    ROLE / "tasks/adguard_home_bootstrap.yml",
    ROLE / "tasks/adguard_home_configure.yml",
    ROLE / "templates/docker-compose.yml.j2",
    ROLE / "templates/network-gateway-lists.service.j2",
    ROLE / "templates/network-gateway-lists.timer.j2",
]
for path in required_files:
    assert path.is_file() and path.stat().st_size > 0, path

tasks_text = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
assert "ansible_facts.interfaces" in tasks_text
assert "network_gateway_wan_address" in tasks_text
assert "network_gateway_lan_address" in tasks_text
assert "direct_wan_nat == false" in tasks_text

nftables = (ROLE / "templates/nftables.conf.j2").read_text(encoding="utf-8")
assert "policy drop" in nftables
assert "ct state established,related accept" in nftables
assert "masquerade" not in nftables.lower()
assert "snat" not in nftables.lower()
assert "route_{{ target_name }}_v4" in nftables
assert "flags timeout" in nftables

smartdns_template = (ROLE / "templates/smartdns.conf.j2").read_text(encoding="utf-8")
assert "server-https" in smartdns_template
assert "domain-set" in smartdns_template
assert "-nftset" in smartdns_template

compose_template = (ROLE / "templates/docker-compose.yml.j2").read_text(encoding="utf-8")
assert "network_mode: host" in compose_template
assert "adguard-home" in compose_template

normalizer_path = ROLE / "files/network-gateway-lists.py"
spec = importlib.util.spec_from_file_location("network_gateway_lists", normalizer_path)
assert spec is not None and spec.loader is not None
normalizer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(normalizer)

assert normalizer.normalize("Example.COM\n# comment\nexample.com\n", "domain-list") == [
    "example.com"
]
assert normalizer.normalize(
    "127.0.0.1 localhost\n192.168.1.10 PC-SERGEY\n",
    "hosts",
) == ["localhost", "pc-sergey"]
assert normalizer.normalize("192.168.1.1\n192.168.1.1\n", "ip-list") == [
    "192.168.1.1"
]
assert normalizer.normalize("10.0.0.1/24\n", "cidr-list") == ["10.0.0.0/24"]

merged = normalizer.merge_sources(
    [
        {
            "name": "remote",
            "format": "domain-list",
            "target": "vpn1",
            "priority": 100,
            "values": ["example.com"],
        },
        {
            "name": "local",
            "format": "domain-list",
            "target": "direct",
            "priority": 1000,
            "values": ["example.com"],
        },
    ]
)
assert merged["domain-list:example.com"]["target"] == "direct"

try:
    normalizer.merge_sources(
        [
            {
                "name": "a",
                "format": "domain-list",
                "target": "vpn1",
                "priority": 100,
                "values": ["conflict.example"],
            },
            {
                "name": "b",
                "format": "domain-list",
                "target": "vpn2",
                "priority": 100,
                "values": ["conflict.example"],
            },
        ]
    )
except ValueError:
    pass
else:
    raise AssertionError("конфликт одинакового приоритета должен завершаться ошибкой")

with tempfile.TemporaryDirectory() as tmp:
    tmp_path = Path(tmp)
    source_file = tmp_path / "domains.txt"
    source_file.write_text("example.com\n", encoding="utf-8")
    state = tmp_path / "state"
    source = {
        "name": "test",
        "type": "file",
        "path": str(source_file),
        "format": "domain-list",
        "target": "vpn1",
        "priority": 100,
    }
    first = normalizer.load_source(source, state, True)
    assert first["from_last_good"] is False
    source_file.unlink()
    fallback = normalizer.load_source(source, state, True)
    assert fallback["from_last_good"] is True
    assert fallback["values"] == ["example.com"]

print("[ОК] Network Gateway contract")
