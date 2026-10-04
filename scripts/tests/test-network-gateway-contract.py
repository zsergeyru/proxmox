#!/usr/bin/env python3
"""Контрактные проверки 109 network-gateway без доступа к PVE."""

from __future__ import annotations

from pathlib import Path

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
assert gateway["dhcp"]["enabled"] is True
assert gateway["dhcp"]["range"] == {
    "start": "192.168.1.100",
    "end": "192.168.1.250",
}
assert gateway["dhcp"]["domain"] == "home.arpa"
assert gateway["firewall"]["enabled"] is True
assert gateway["firewall"]["direct_wan_nat"] is False

required_packages = set(provision["system"]["required_packages"])
assert {"dnsmasq", "iproute2", "nftables", "wireguard-tools"}.issubset(
    required_packages
)

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
    ROLE / "templates/dnsmasq.conf.j2",
    ROLE / "templates/nftables.conf.j2",
]
for path in required_files:
    assert path.is_file() and path.stat().st_size > 0, path

tasks_text = (ROLE / "tasks/main.yml").read_text(encoding="utf-8")
assert "ansible_facts.interfaces" in tasks_text
assert "network_gateway_wan_address" in tasks_text
assert "network_gateway_lan_address" in tasks_text
assert "direct_wan_nat == false" in tasks_text

dnsmasq = (ROLE / "templates/dnsmasq.conf.j2").read_text(encoding="utf-8")
assert "port=0" in dnsmasq
assert "dhcp-range=" in dnsmasq
assert "option:router" in dnsmasq
assert "option:dns-server" in dnsmasq

nftables = (ROLE / "templates/nftables.conf.j2").read_text(encoding="utf-8")
assert "policy drop" in nftables
assert "ct state established,related accept" in nftables
assert "masquerade" not in nftables.lower()
assert "snat" not in nftables.lower()

print("[ОК] Network Gateway contract")
