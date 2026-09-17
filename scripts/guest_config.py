#!/usr/bin/env python3
"""Общий resolver guest-конфигурации для validator и PVE deployer."""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Any

MISSING = object()

BOOTSTRAP_CAPABILITY_ORDER = (
    "base",
    "git",
    "docker",
    "ansible_controller",
)
BOOTSTRAP_DEPENDENCIES = {
    "base": (),
    "git": ("base",),
    "docker": ("base",),
    "ansible_controller": ("base", "git", "docker"),
}


class GuestConfigError(ValueError):
    """Конфигурацию гостя нельзя однозначно преобразовать в effective state."""


@dataclass(frozen=True)
class NetworkConfig:
    subnet: ipaddress.IPv4Network
    gateway: ipaddress.IPv4Address


@dataclass(frozen=True)
class ResolvedGuest:
    effective: dict[str, Any]
    network: NetworkConfig
    management_ip: ipaddress.IPv4Address
    management_ip_source: str
    bootstrap_capabilities: tuple[str, ...]


def deep_merge(base: dict, overlay: dict) -> dict:
    """Рекурсивно объединить mappings без изменения исходных объектов."""
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def get_nested(data: dict, path: tuple[str, ...]):
    value = data
    for part in path:
        if not isinstance(value, dict) or part not in value:
            return MISSING
        value = value[part]
    return value


def parse_network_config(defaults: dict) -> NetworkConfig:
    """Прочитать и проверить единую активную management-сеть из defaults.yaml."""
    net = defaults.get("defaults", {}).get("network", {})
    if not isinstance(net, dict):
        raise GuestConfigError("defaults.network должен быть mapping/object")

    try:
        subnet = ipaddress.ip_network(net.get("subnet"), strict=True)
    except (TypeError, ValueError) as exc:
        raise GuestConfigError(f"некорректный defaults.network.subnet: {exc}") from exc
    if not isinstance(subnet, ipaddress.IPv4Network):
        raise GuestConfigError("defaults.network.subnet должен быть IPv4")
    if subnet.prefixlen != 16:
        raise GuestConfigError(
            "defaults.network.subnet должен быть /16 для VMID-адресации"
        )

    try:
        gateway = ipaddress.ip_address(net.get("gateway"))
    except (TypeError, ValueError) as exc:
        raise GuestConfigError(f"некорректный defaults.network.gateway: {exc}") from exc
    if not isinstance(gateway, ipaddress.IPv4Address):
        raise GuestConfigError("defaults.network.gateway должен быть IPv4")
    if gateway not in subnet:
        raise GuestConfigError(f"gateway {gateway} находится вне {subnet}")
    if gateway in {subnet.network_address, subnet.broadcast_address}:
        raise GuestConfigError(f"gateway {gateway} не может быть network/broadcast")
    if net.get("addressing") != "vmid":
        raise GuestConfigError("defaults.network.addressing должен быть 'vmid'")

    return NetworkConfig(subnet=subnet, gateway=gateway)


def vmid_address(vmid: int, network: NetworkConfig) -> ipaddress.IPv4Address:
    """Вычислить management IPv4 по правилу VMID XYZ -> A.B.X.YZ."""
    if not isinstance(vmid, int) or not 100 <= vmid <= 999:
        raise GuestConfigError(f"VMID {vmid!r} не поддерживается VMID-адресацией")

    first, second, _, _ = str(network.subnet.network_address).split(".")
    text = f"{vmid:03d}"
    return ipaddress.ip_address(
        f"{first}.{second}.{int(text[0])}.{int(text[1:])}"
    )


def parse_bare_ipv4(value: object, field: str = "network.ipv4.address") -> ipaddress.IPv4Address:
    """Разобрать source override: только bare IPv4, prefix приходит из defaults."""
    if not isinstance(value, str):
        raise GuestConfigError(f"{field} должен быть строкой IPv4")
    if "/" in value:
        raise GuestConfigError(
            f"{field} должен быть без /prefix; маска берётся из defaults.yaml"
        )
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise GuestConfigError(f"некорректный {field} {value!r}: {exc}") from exc
    if not isinstance(address, ipaddress.IPv4Address):
        raise GuestConfigError(f"{field} должен быть IPv4")
    return address


def resolve_management_ip(
    source: dict,
    network: NetworkConfig,
) -> tuple[ipaddress.IPv4Address, str]:
    """Вернуть management IP и provenance: 'vmid' либо 'guest'."""
    override = get_nested(source, ("network", "ipv4", "address"))
    if override is MISSING:
        return vmid_address(source.get("vmid"), network), "vmid"
    return parse_bare_ipv4(override), "guest"


def resolve_bootstrap_capabilities(effective: dict) -> tuple[str, ...]:
    """Вернуть включённые bootstrap capabilities в каноническом порядке v1."""
    bootstrap = effective.get("bootstrap")
    if bootstrap is None:
        return ()
    if not isinstance(bootstrap, dict):
        raise GuestConfigError("bootstrap должен быть mapping/object")

    capabilities = bootstrap.get("capabilities")
    if not isinstance(capabilities, dict):
        raise GuestConfigError("bootstrap.capabilities должен быть mapping/object")

    unknown = sorted(set(capabilities) - set(BOOTSTRAP_CAPABILITY_ORDER))
    if unknown:
        raise GuestConfigError(
            "неизвестные bootstrap capabilities: " + ", ".join(unknown)
        )

    for name, enabled in capabilities.items():
        if not isinstance(enabled, bool):
            raise GuestConfigError(
                f"bootstrap.capabilities.{name} должен быть boolean"
            )

    enabled = tuple(
        name for name in BOOTSTRAP_CAPABILITY_ORDER if capabilities.get(name) is True
    )
    if not enabled:
        raise GuestConfigError(
            "bootstrap задан, но не включена ни одна capability; удалите bootstrap"
        )

    for name in enabled:
        missing = [
            dependency
            for dependency in BOOTSTRAP_DEPENDENCIES[name]
            if capabilities.get(dependency) is not True
        ]
        if missing:
            raise GuestConfigError(
                f"bootstrap capability {name!r} требует явно включить: "
                + ", ".join(missing)
            )

    boot = effective.get("boot")
    if not isinstance(boot, dict) or boot.get("start_after_deploy") is not True:
        raise GuestConfigError(
            "bootstrap требует boot.start_after_deploy=true, так как выполняется "
            "только после проверенного SSH root"
        )

    return enabled


def resolve_effective_guest(
    source: dict,
    defaults: dict,
    network: NetworkConfig | None = None,
) -> ResolvedGuest:
    """Собрать deterministic effective desired state deployable-гостя."""
    profile_name = source.get("profile")
    profile = defaults.get("profiles", {}).get(profile_name)
    if not isinstance(profile, dict):
        raise GuestConfigError(f"неизвестный профиль {profile_name!r}")

    if network is None:
        network = parse_network_config(defaults)

    effective = deep_merge(defaults.get("defaults", {}), profile)
    effective = deep_merge(effective, source)

    management_ip, ip_source = resolve_management_ip(source, network)
    effective_network = effective.get("network")
    if not isinstance(effective_network, dict):
        raise GuestConfigError("effective network должен быть mapping/object")
    effective_network["ipv4"] = {
        "address": f"{management_ip}/{network.subnet.prefixlen}"
    }

    bootstrap_capabilities = resolve_bootstrap_capabilities(effective)

    return ResolvedGuest(
        effective=effective,
        network=network,
        management_ip=management_ip,
        management_ip_source=ip_source,
        bootstrap_capabilities=bootstrap_capabilities,
    )
