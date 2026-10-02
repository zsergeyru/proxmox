"""Разрешение централизованных правил доступа из infrastructure/security/access.yaml."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .common import InfraManagerError


@dataclass(frozen=True)
class GuestRecord:
    vmid: int
    name: str
    kind: str
    node: str
    address: str | None
    subnet: str
    pve_management: bool
    provisioned_linux: bool


@dataclass(frozen=True)
class SshOtpEdge:
    source_vmid: int
    target_vmid: int


@dataclass(frozen=True)
class AccessPolicy:
    guests: dict[int, GuestRecord]
    machine_identity_vmids: frozenset[int]
    ssh_otp_edges: frozenset[SshOtpEdge]
    project_repository_read_vmids: frozenset[int]

    def sources_for(self, target_vmid: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                edge.source_vmid
                for edge in self.ssh_otp_edges
                if edge.target_vmid == target_vmid
            )
        )

    def targets_for(self, source_vmid: int) -> tuple[int, ...]:
        return tuple(
            sorted(
                edge.target_vmid
                for edge in self.ssh_otp_edges
                if edge.source_vmid == source_vmid
            )
        )

    def project_repository_read_allowed(self, vmid: int) -> bool:
        return vmid in self.project_repository_read_vmids


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise InfraManagerError(f"Не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InfraManagerError(f"{path} должен содержать YAML object")
    return value


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _management_address(vmid: int, effective: dict[str, Any]) -> str | None:
    network = effective.get("network")
    if not isinstance(network, dict):
        raise InfraManagerError(f"guest:{vmid} не содержит network")
    value = network.get("ipv4")
    subnet_value = network.get("subnet")
    if value == "dhcp":
        return None
    if isinstance(value, str) and value:
        try:
            return str(ipaddress.ip_interface(value).ip)
        except ValueError:
            try:
                return str(ipaddress.ip_address(value))
            except ValueError as exc:
                raise InfraManagerError(
                    f"guest:{vmid} содержит некорректный network.ipv4"
                ) from exc
    if not isinstance(subnet_value, str):
        raise InfraManagerError(f"guest:{vmid} не содержит network.subnet")
    try:
        subnet = ipaddress.ip_network(subnet_value, strict=True)
    except ValueError as exc:
        raise InfraManagerError(
            f"guest:{vmid} содержит некорректный network.subnet"
        ) from exc
    if not isinstance(subnet, ipaddress.IPv4Network) or subnet.prefixlen != 16:
        raise InfraManagerError(
            f"guest:{vmid}: автоматический адрес поддерживает только IPv4 /16"
        )
    text = f"{vmid:03d}"
    first, second, _, _ = str(subnet.network_address).split(".")
    return f"{first}.{second}.{int(text[0])}.{int(text[1:])}"


def _guest_records(repo_root: Path) -> dict[int, GuestRecord]:
    guests_root = repo_root / "infrastructure/guests"
    defaults = _load_yaml(guests_root / "defaults.yaml")
    common = defaults.get("defaults")
    profiles = defaults.get("profiles")
    if not isinstance(common, dict) or not isinstance(profiles, dict):
        raise InfraManagerError("defaults.yaml имеет некорректную структуру")

    records: dict[int, GuestRecord] = {}
    for manifest in sorted(guests_root.glob("*/guest.yaml")):
        source = _load_yaml(manifest)
        vmid = source.get("vmid")
        name = source.get("name")
        profile_name = source.get("profile")
        if not isinstance(vmid, int) or not isinstance(name, str):
            continue
        if not isinstance(profile_name, str):
            continue
        profile = profiles.get(profile_name)
        if not isinstance(profile, dict):
            continue

        effective = _deep_merge(common, profile)
        effective = _deep_merge(effective, source)
        kind = effective.get("type")
        node = effective.get("node")
        network = effective.get("network")
        if kind not in {"vm", "lxc"} or not isinstance(node, str):
            continue
        if not isinstance(network, dict):
            continue
        subnet = network.get("subnet")
        if not isinstance(subnet, str):
            continue

        records[vmid] = GuestRecord(
            vmid=vmid,
            name=name,
            kind=kind,
            node=node,
            address=_management_address(vmid, effective),
            subnet=subnet,
            pve_management=effective.get("pve_management") is True,
            provisioned_linux=(manifest.parent / "provision.yaml").is_file(),
        )
    return records


def _expand_target(
    selector: str,
    *,
    source_vmid: int,
    guests: dict[int, GuestRecord],
) -> set[int]:
    if selector.startswith("guest:"):
        try:
            vmid = int(selector.partition(":")[2])
        except ValueError as exc:
            raise InfraManagerError(
                f"Некорректная SSH-цель {selector!r}"
            ) from exc
        record = guests.get(vmid)
        if record is None or not record.provisioned_linux:
            raise InfraManagerError(
                f"SSH-цель {selector!r} не является поддерживаемым Linux-гостем"
            )
        return {vmid} if vmid != source_vmid else set()

    if selector == "pool:managed":
        return {
            vmid
            for vmid, record in guests.items()
            if vmid != source_vmid
            and record.provisioned_linux
            and record.pve_management
        }

    if selector == "project:linux-guests":
        return {
            vmid
            for vmid, record in guests.items()
            if vmid != source_vmid and record.provisioned_linux
        }

    raise InfraManagerError(
        f"Неподдерживаемая SSH-цель access.yaml: {selector!r}"
    )


def load_access_policy(repo_root: Path) -> AccessPolicy:
    """Построить эффективную политику SSH OTP проекта."""

    access = _load_yaml(repo_root / "infrastructure/security/access.yaml")
    rules = access.get("rules")
    if not isinstance(rules, list):
        raise InfraManagerError("access.yaml не содержит rules")

    guests = _guest_records(repo_root)
    identity_vmids: set[int] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if (
            rule.get("service") == "ssh"
            and rule.get("resource") == "identity"
            and "issue" in rule.get("access", [])
            and "self" in rule.get("targets", [])
        ):
            subject = rule.get("subject")
            if isinstance(subject, str) and subject.startswith("guest:"):
                vmid_text = subject.partition(":")[2]
                if vmid_text.isdigit():
                    vmid = int(vmid_text)
                    if vmid not in guests:
                        raise InfraManagerError(
                            f"access.yaml ссылается на неизвестный guest:{vmid}"
                        )
                    identity_vmids.add(vmid)

    project_repository_read_vmids: set[int] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if (
            rule.get("service") != "github"
            or rule.get("resource") != "repository"
            or "read" not in rule.get("access", [])
            or "zsergeyru/proxmox" not in rule.get("targets", [])
        ):
            continue
        subject = rule.get("subject")
        if not isinstance(subject, str) or not subject.startswith("guest:"):
            continue
        vmid_text = subject.partition(":")[2]
        if not vmid_text.isdigit():
            continue
        vmid = int(vmid_text)
        if vmid not in guests:
            raise InfraManagerError(
                f"access.yaml ссылается на неизвестный guest:{vmid}"
            )
        project_repository_read_vmids.add(vmid)

    edges: set[SshOtpEdge] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if (
            rule.get("service") != "ssh"
            or rule.get("resource") != "guest"
            or "connect-root" not in rule.get("access", [])
        ):
            continue
        subject = rule.get("subject")
        targets = rule.get("targets")
        if (
            not isinstance(subject, str)
            or not subject.startswith("guest:")
            or not isinstance(targets, list)
        ):
            continue
        source_text = subject.partition(":")[2]
        if not source_text.isdigit():
            continue
        source_vmid = int(source_text)
        if source_vmid not in identity_vmids:
            raise InfraManagerError(
                f"guest:{source_vmid} имеет SSH-цели, но не имеет identity/issue"
            )
        for selector in targets:
            if not isinstance(selector, str):
                continue
            for target_vmid in _expand_target(
                selector,
                source_vmid=source_vmid,
                guests=guests,
            ):
                edges.add(
                    SshOtpEdge(
                        source_vmid=source_vmid,
                        target_vmid=target_vmid,
                    )
                )

    return AccessPolicy(
        guests=guests,
        machine_identity_vmids=frozenset(identity_vmids),
        ssh_otp_edges=frozenset(edges),
        project_repository_read_vmids=frozenset(
            project_repository_read_vmids
        ),
    )
