#!/usr/bin/env python3
"""Общий resolver guest-конфигурации для validator и runtime-инструментов."""

from __future__ import annotations

import copy
import ipaddress
from dataclasses import dataclass
from typing import Any

MISSING = object()
DHCP = "dhcp"

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


def _resolve_interfaces(
    source: dict,
    defaults: dict,
) -> tuple[list[dict[str, Any]], ipaddress.IPv4Address | None, str]:
    """Нормализовать расширенный сетевой контракт и определить management IP."""
    network = source.get("network")
    if not isinstance(network, dict):
        raise GuestConfigError("network должен быть mapping/object")
    raw_interfaces = network.get("interfaces")
    if not isinstance(raw_interfaces, list) or not raw_interfaces:
        raise GuestConfigError("network.interfaces должен быть непустым list")

    default_bridge = get_nested(defaults, ("defaults", "network", "bridge"))
    if not isinstance(default_bridge, str) or not default_bridge:
        raise GuestConfigError("defaults.network.bridge должен быть непустой строкой")

    seen_names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    management_indexes: list[int] = []
    gateway_count = 0

    for index, raw in enumerate(raw_interfaces):
        if not isinstance(raw, dict):
            raise GuestConfigError(
                f"network.interfaces[{index}] должен быть mapping/object"
            )
        name = raw.get("name")
        if not isinstance(name, str) or not name:
            raise GuestConfigError(
                f"network.interfaces[{index}].name должен быть непустой строкой"
            )
        if name in seen_names:
            raise GuestConfigError(f"дублирующееся имя сетевого интерфейса {name!r}")
        seen_names.add(name)

        ipv4_value = raw.get("ipv4")
        if ipv4_value == DHCP:
            normalized_ipv4 = DHCP
        else:
            if not isinstance(ipv4_value, str):
                raise GuestConfigError(
                    f"network.interfaces[{index}].ipv4 должен быть IPv4/prefix или dhcp"
                )
            try:
                iface = ipaddress.ip_interface(ipv4_value)
            except ValueError as exc:
                raise GuestConfigError(
                    f"некорректный network.interfaces[{index}].ipv4 "
                    f"{ipv4_value!r}: {exc}"
                ) from exc
            if not isinstance(iface, ipaddress.IPv4Interface):
                raise GuestConfigError(
                    f"network.interfaces[{index}].ipv4 должен быть IPv4"
                )
            normalized_ipv4 = str(iface)

        bridge = raw.get("bridge", default_bridge)
        if not isinstance(bridge, str) or not bridge:
            raise GuestConfigError(
                f"network.interfaces[{index}].bridge должен быть непустой строкой"
            )

        item: dict[str, Any] = {
            "name": name,
            "bridge": bridge,
            "ipv4": normalized_ipv4,
            "management": bool(raw.get("management", False)),
        }

        vlan = raw.get("vlan")
        if vlan is not None:
            if not isinstance(vlan, int) or isinstance(vlan, bool) or not 1 <= vlan <= 4094:
                raise GuestConfigError(
                    f"network.interfaces[{index}].vlan должен быть от 1 до 4094"
                )
            item["vlan"] = vlan

        gateway = raw.get("gateway")
        if gateway is not None:
            try:
                parsed_gateway = ipaddress.ip_address(gateway)
            except ValueError as exc:
                raise GuestConfigError(
                    f"некорректный network.interfaces[{index}].gateway "
                    f"{gateway!r}: {exc}"
                ) from exc
            if not isinstance(parsed_gateway, ipaddress.IPv4Address):
                raise GuestConfigError(
                    f"network.interfaces[{index}].gateway должен быть IPv4"
                )
            item["gateway"] = str(parsed_gateway)
            gateway_count += 1

        if item["management"]:
            management_indexes.append(index)
        normalized.append(item)

    if gateway_count > 1:
        raise GuestConfigError("network.interfaces допускает не более одного gateway")

    if len(normalized) == 1 and not management_indexes:
        normalized[0]["management"] = True
        management_indexes = [0]
    elif len(management_indexes) != 1:
        raise GuestConfigError(
            "network.interfaces должен содержать ровно один management=true"
        )

    management = normalized[management_indexes[0]]
    if management["ipv4"] == DHCP:
        return normalized, None, DHCP

    management_iface = ipaddress.ip_interface(management["ipv4"])
    return normalized, management_iface.ip, "interfaces"


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


def _validated_profile(
    source: dict,
    defaults: dict,
) -> tuple[dict, str, tuple[str, ...]]:
    """Вернуть профиль, его тип и нормализованные LXC-возможности."""
    profile_name = source.get("profile")
    profile = defaults.get("profiles", {}).get(profile_name)
    if not isinstance(profile, dict):
        raise GuestConfigError(f"неизвестный профиль {profile_name!r}")

    kind = profile.get("type")
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
        return profile, kind, ()

    if kind == "lxc":
        ostemplate = profile.get("ostemplate")
        if not isinstance(ostemplate, str) or not ostemplate:
            raise GuestConfigError(
                f"LXC-профиль {profile_name!r} должен задавать ostemplate"
            )
        if "template_vmid" in profile:
            raise GuestConfigError(
                f"LXC-профиль {profile_name!r} не должен задавать template_vmid"
            )
        return profile, kind, resolve_profile_features(profile)

    raise GuestConfigError(
        f"профиль {profile_name!r} имеет неверный type {kind!r}"
    )


def _build_effective_guest(
    source: dict,
    defaults: dict,
    profile: dict,
    kind: str,
    features: tuple[str, ...],
    network: NetworkConfig,
) -> tuple[dict, ipaddress.IPv4Address | None, str]:
    """Собрать effective state и вычислить административный адрес."""
    defaults_fragment = defaults.get("defaults")
    if not isinstance(defaults_fragment, dict):
        raise GuestConfigError("defaults.defaults должен быть mapping/object")

    effective = deep_merge(defaults_fragment, profile)
    effective = deep_merge(effective, source)
    if kind == "lxc":
        effective["features"] = list(features)

    source_network = source.get("network")
    if isinstance(source_network, dict) and "interfaces" in source_network:
        interfaces, management_ip, ip_source = _resolve_interfaces(source, defaults)
        effective["network"] = {"interfaces": interfaces}
        return effective, management_ip, ip_source

    management_ip, ip_source = resolve_management_ip(source, network)
    effective_network = effective.get("network")
    if not isinstance(effective_network, dict):
        raise GuestConfigError("effective network должен быть mapping/object")
    effective_network["ipv4"] = (
        DHCP
        if management_ip is None
        else f"{management_ip}/{network.subnet.prefixlen}"
    )
    return effective, management_ip, ip_source


def resolve_effective_guest(
    source: dict,
    defaults: dict,
    network: NetworkConfig | None = None,
) -> ResolvedGuest:
    """Собрать deterministic effective desired state гостя с profile."""
    profile, kind, features = _validated_profile(source, defaults)
    actual_network = network or parse_network_config(defaults)
    effective, management_ip, ip_source = _build_effective_guest(
        source,
        defaults,
        profile,
        kind,
        features,
        actual_network,
    )
    return ResolvedGuest(
        effective=effective,
        network=actual_network,
        management_ip=management_ip,
        management_ip_source=ip_source,
    )
