#!/usr/bin/env python3
"""Общий resolver guest-конфигурации для validator и runtime-инструментов."""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Any

MISSING = object()
DHCP = "dhcp"

MANAGEMENT_ORDER = (
    "ssh_identity",
    "project_repo_read",
)
BOOTSTRAP_ORDER = (
    "git",
    "docker",
    "ansible",
)
PROFILE_FEATURE_ORDER = (
    "container-host",
)


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
    management_ip: ipaddress.IPv4Address | None
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
    """Прочитать и проверить единую active management-сеть из defaults.yaml."""
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


def parse_bare_ipv4(value: object, field: str = "network.ipv4") -> ipaddress.IPv4Address:
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


def is_dhcp_ipv4(value: object) -> bool:
    """Вернуть True для явного режима DHCP."""
    return value == DHCP


def resolve_management_ip(
    source: dict,
    network: NetworkConfig,
) -> tuple[ipaddress.IPv4Address | None, str]:
    """Вернуть management IP и источник: vmid, guest либо dhcp."""
    override = get_nested(source, ("network", "ipv4"))
    if override is MISSING:
        return vmid_address(source.get("vmid"), network), "vmid"
    if is_dhcp_ipv4(override):
        return None, DHCP
    return parse_bare_ipv4(override), "guest"


def _canonical_source_list(
    source: dict,
    key: str,
    allowed: tuple[str, ...],
) -> tuple[str, ...]:
    value = source.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise GuestConfigError(f"{key} должен быть непустым list")
    if not source.get("profile"):
        raise GuestConfigError(f"{key} разрешён только гостю с profile")
    if any(not isinstance(item, str) for item in value):
        raise GuestConfigError(f"{key} должен содержать только строки")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise GuestConfigError(
            f"неизвестные элементы {key}: " + ", ".join(unknown)
        )
    if len(value) != len(set(value)):
        raise GuestConfigError(f"{key} не должен содержать дубликаты")
    return tuple(item for item in allowed if item in value)


def resolve_management(
    source: dict,
    defaults: dict,
    profile: dict,
) -> tuple[str, ...]:
    """Вернуть канонический список individual-only management-возможностей."""
    if "management" in defaults.get("defaults", {}):
        raise GuestConfigError("defaults не должны задавать management")
    if "management" in profile:
        raise GuestConfigError("profile не должен задавать management")
    return _canonical_source_list(source, "management", MANAGEMENT_ORDER)


def resolve_bootstrap(
    source: dict,
    defaults: dict,
    profile: dict,
) -> tuple[str, ...]:
    """Вернуть канонический список Bootstrap-инструментов."""
    if "bootstrap" in defaults.get("defaults", {}):
        raise GuestConfigError("defaults не должны задавать bootstrap")
    if "bootstrap" in profile:
        raise GuestConfigError("profile не должен задавать bootstrap")
    return _canonical_source_list(source, "bootstrap", BOOTSTRAP_ORDER)


def resolve_profile_features(profile: dict) -> tuple[str, ...]:
    """Нормализовать список высокоуровневых features профиля."""
    value = profile.get("features")
    if value is None:
        return ()
    if not isinstance(value, list) or not value:
        raise GuestConfigError("profile.features должен быть непустым list")
    if any(not isinstance(item, str) for item in value):
        raise GuestConfigError("profile.features должен содержать только строки")
    unknown = sorted(set(value) - set(PROFILE_FEATURE_ORDER))
    if unknown:
        raise GuestConfigError(
            "неизвестные profile.features: " + ", ".join(unknown)
        )
    if len(value) != len(set(value)):
        raise GuestConfigError("profile.features не должен содержать дубликаты")
    return tuple(item for item in PROFILE_FEATURE_ORDER if item in value)


def resolve_effective_guest(
    source: dict,
    defaults: dict,
    network: NetworkConfig | None = None,
) -> ResolvedGuest:
    """Собрать deterministic effective desired state гостя с profile."""
    profile_name = source.get("profile")
    profile = defaults.get("profiles", {}).get(profile_name)
    if not isinstance(profile, dict):
        raise GuestConfigError(f"неизвестный профиль {profile_name!r}")

    kind = profile.get("type")
    if kind not in {"vm", "lxc"}:
        raise GuestConfigError(f"профиль {profile_name!r} имеет неверный type {kind!r}")

    if kind == "vm":
        template_vmid = profile.get("template_vmid")
        if not isinstance(template_vmid, int):
            raise GuestConfigError(
                f"VM-профиль {profile_name!r} должен задавать template_vmid"
            )
        if "ostemplate" in profile or "features" in profile:
            raise GuestConfigError(
                f"VM-профиль {profile_name!r} не должен задавать LXC-параметры"
            )
        features: tuple[str, ...] = ()
    else:
        ostemplate = profile.get("ostemplate")
        if not isinstance(ostemplate, str) or not ostemplate:
            raise GuestConfigError(
                f"LXC-профиль {profile_name!r} должен задавать ostemplate"
            )
        if "template_vmid" in profile:
            raise GuestConfigError(
                f"LXC-профиль {profile_name!r} не должен задавать template_vmid"
            )
        features = resolve_profile_features(profile)

    if network is None:
        network = parse_network_config(defaults)

    defaults_fragment = defaults.get("defaults")
    if not isinstance(defaults_fragment, dict):
        raise GuestConfigError("defaults.defaults должен быть mapping/object")

    effective = deep_merge(defaults_fragment, profile)
    effective = deep_merge(effective, source)

    management = resolve_management(source, defaults, profile)
    bootstrap = resolve_bootstrap(source, defaults, profile)
    effective["management"] = list(management)
    effective["bootstrap"] = list(bootstrap)

    if kind == "lxc":
        effective["features"] = list(features)
        if "docker" in bootstrap and "container-host" not in features:
            raise GuestConfigError(
                "bootstrap 'docker' для LXC требует feature 'container-host' "
                "в выбранном профиле"
            )

    management_ip, ip_source = resolve_management_ip(source, network)
    effective_network = effective.get("network")
    if not isinstance(effective_network, dict):
        raise GuestConfigError("effective network должен быть mapping/object")
    effective_network["ipv4"] = (
        DHCP
        if management_ip is None
        else f"{management_ip}/{network.subnet.prefixlen}"
    )

    return ResolvedGuest(
        effective=effective,
        network=network,
        management_ip=management_ip,
        management_ip_source=ip_source,
        bootstrap_capabilities=bootstrap,
    )
