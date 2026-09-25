"""PVE HTTPS API client и проверка прав infra-manager."""

from __future__ import annotations

import ipaddress
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console
from .settings import PATHS, SETTINGS

PVE_ENV = PATHS.pve_api_env
CA_BUNDLE = PATHS.ca_bundle
MANAGED_POOL = SETTINGS.managed_pool

VM_ADMIN_PRIVS = {
    "VM.Allocate",
    "VM.Audit",
    "VM.Backup",
    "VM.Clone",
    "VM.Config.CDROM",
    "VM.Config.Cloudinit",
    "VM.Config.CPU",
    "VM.Config.Disk",
    "VM.Config.HWType",
    "VM.Config.Memory",
    "VM.Config.Network",
    "VM.Config.Options",
    "VM.GuestAgent.Audit",
    "VM.PowerMgmt",
    "VM.Snapshot",
    "VM.Snapshot.Rollback",
}

FORBIDDEN_ROOT_PRIVS = {
    "Permissions.Modify",
    "Sys.Modify",
    "Sys.PowerMgmt",
    "User.Modify",
    "Group.Allocate",
    "Realm.Allocate",
    "Realm.AllocateUser",
    "Pool.Allocate",
    "Datastore.Allocate",
    "Datastore.AllocateSpace",
    "Datastore.AllocateTemplate",
    "SDN.Allocate",
    "SDN.Use",
    "Mapping.Modify",
}


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


class PveClient:
    def __init__(
        self,
        env_path: Path = PVE_ENV,
        ca_path: Path = CA_BUNDLE,
        *,
        url: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
    ) -> None:
        if not nonempty(ca_path):
            raise InfraManagerError(f"Не найден CA bundle: {ca_path}")

        if url is None and token_id is None and token_secret is None:
            if not nonempty(env_path):
                raise InfraManagerError(
                    f"Не найден PVE credential: {env_path}"
                )
            values = read_env_file(env_path)
            url = values.get("PVE_API_URL", "")
            token_id = values.get("PVE_API_TOKEN_ID", "")
            token_secret = values.get("PVE_API_TOKEN_SECRET", "")

        self.url = (url or "").rstrip("/")
        self.token_id = token_id or ""
        self.token_secret = token_secret or ""
        if not self.url:
            raise InfraManagerError("PVE API URL пуст")
        if not self.token_id:
            raise InfraManagerError("PVE API token id пуст")
        if not self.token_secret:
            raise InfraManagerError("PVE API token secret пуст")

        self.context = ssl.create_default_context(cafile=str(ca_path))

    @classmethod
    def from_opentofu_env(
        cls,
        ca_path: Path = CA_BUNDLE,
    ) -> PveClient:
        endpoint = os.environ.get("TF_VAR_pve_endpoint", "").strip()
        token = os.environ.get("TF_VAR_pve_api_token", "").strip()
        token_id, separator, token_secret = token.partition("=")
        if not endpoint:
            raise InfraManagerError("Не задан TF_VAR_pve_endpoint")
        if not separator or not token_id or not token_secret:
            raise InfraManagerError(
                "TF_VAR_pve_api_token имеет неверный формат"
            )
        return cls(
            ca_path=ca_path,
            url=endpoint,
            token_id=token_id,
            token_secret=token_secret,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        query: dict[str, str | int] | None = None,
        form: dict[str, str | int] | None = None,
        timeout: int = 120,
    ) -> Any:
        url = f"{self.url}/api2/json{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": (
                f"PVEAPIToken={self.token_id}={self.token_secret}"
            ),
        }
        if form is not None:
            body = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"

        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout,
                context=self.context,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise InfraManagerError(
                f"PVE API {method} {endpoint} вернул HTTP {exc.code}: "
                f"{detail or '(пустой ответ)'}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InfraManagerError(
                f"PVE API {method} {endpoint} недоступен: {exc}"
            ) from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfraManagerError(
                f"PVE API {method} {endpoint} вернул некорректный JSON"
            ) from exc

    def get(
        self,
        endpoint: str,
        *,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        return self._request("GET", endpoint, query=query)

    def post(
        self,
        endpoint: str,
        *,
        form: dict[str, str | int] | None = None,
    ) -> Any:
        return self._request("POST", endpoint, form=form)

    def put(
        self,
        endpoint: str,
        *,
        form: dict[str, str | int] | None = None,
    ) -> Any:
        return self._request("PUT", endpoint, form=form)

    def delete(
        self,
        endpoint: str,
        *,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        return self._request("DELETE", endpoint, query=query)

    @staticmethod
    def _data_from_payload(payload: Any, context: str) -> Any:
        if not isinstance(payload, dict) or "data" not in payload:
            raise InfraManagerError(f"{context} не содержит data")
        return payload["data"]

    def data(
        self,
        endpoint: str,
        *,
        query: dict[str, str | int] | None = None,
    ) -> Any:
        return self._data_from_payload(
            self.get(endpoint, query=query),
            f"PVE API GET {endpoint}",
        )

    def request_data(
        self,
        method: str,
        endpoint: str,
        *,
        query: dict[str, str | int] | None = None,
        form: dict[str, str | int] | None = None,
    ) -> Any:
        return self._data_from_payload(
            self._request(method, endpoint, query=query, form=form),
            f"PVE API {method} {endpoint}",
        )

    def wait_task(
        self,
        *,
        node: str,
        upid: str,
        timeout: int = 180,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            payload = self.data(f"/nodes/{node}/tasks/{upid}/status")
            if isinstance(payload, dict) and payload.get("status") == "stopped":
                exit_status = payload.get("exitstatus")
                if exit_status != "OK":
                    raise InfraManagerError(
                        f"Задача PVE завершилась: "
                        f"{exit_status or 'неизвестно'}"
                    )
                return
            time.sleep(1)
        raise InfraManagerError(f"Задача PVE не завершилась: {upid}")

    def run_task(
        self,
        method: str,
        endpoint: str,
        *,
        node: str,
        query: dict[str, str | int] | None = None,
        form: dict[str, str | int] | None = None,
        timeout: int = 180,
    ) -> None:
        upid = self.request_data(
            method,
            endpoint,
            query=query,
            form=form,
        )
        if not isinstance(upid, str) or not upid:
            raise InfraManagerError("PVE не вернул task id")
        self.wait_task(node=node, upid=upid, timeout=timeout)

    def find_vm(self, vmid: int) -> dict[str, Any] | None:
        resources = self.data(
            "/cluster/resources",
            query={"type": "vm"},
        )
        if not isinstance(resources, list):
            raise InfraManagerError(
                "PVE API /cluster/resources вернул неожиданный формат"
            )
        matches = [
            item
            for item in resources
            if isinstance(item, dict)
            and str(item.get("vmid", "")) == str(vmid)
        ]
        if len(matches) > 1:
            raise InfraManagerError(
                f"PVE вернул несколько объектов с VMID {vmid}"
            )
        return matches[0] if matches else None

    def vm_config(self, *, node: str, vmid: int) -> dict[str, Any]:
        config = self.data(f"/nodes/{node}/qemu/{vmid}/config")
        if not isinstance(config, dict):
            raise InfraManagerError(
                f"PVE config VMID {vmid} имеет неожиданный формат"
            )
        return config

    def guest_status(self, *, node: str, vmid: int, kind: str) -> str:
        """Вернуть состояние VM или LXC."""
        endpoint_kind = {"vm": "qemu", "lxc": "lxc"}.get(kind)
        if endpoint_kind is None:
            raise InfraManagerError(
                f"Неизвестный тип гостя для чтения состояния: {kind!r}"
            )
        status = self.data(
            f"/nodes/{node}/{endpoint_kind}/{vmid}/status/current"
        )
        if not isinstance(status, dict):
            raise InfraManagerError(
                f"PVE status VMID {vmid} имеет неожиданный формат"
            )
        return str(status.get("status") or "")

    def vm_status(self, *, node: str, vmid: int) -> str:
        """Совместимая оболочка для существующего VM-кода."""
        return self.guest_status(node=node, vmid=vmid, kind="vm")

    def latest_lxc_template(self, *, node: str, selector: str) -> str:
        """Разрешить селектор семейства vztmpl в актуальный локальный volid."""
        storage, separator, family = selector.partition(":vztmpl/")
        if not separator or not storage or not family:
            raise InfraManagerError(
                f"Некорректный LXC template selector: {selector!r}"
            )

        content = self.data(
            f"/nodes/{node}/storage/{storage}/content",
            query={"content": "vztmpl"},
        )
        if not isinstance(content, list):
            raise InfraManagerError(
                f"PVE storage {storage} вернул неожиданный список templates"
            )

        prefix = f"{selector}_"
        candidates = sorted(
            str(item.get("volid"))
            for item in content
            if isinstance(item, dict)
            and isinstance(item.get("volid"), str)
            and str(item.get("volid")).startswith(prefix)
        )
        if not candidates:
            raise InfraManagerError(
                f"На PVE не найден LXC template семейства {selector}"
            )
        return candidates[-1]

    def permissions_at(self, path: str) -> Any:
        return self.data(
            "/access/permissions",
            query={"path": path},
        )

    def guest_interfaces(
        self,
        *,
        node: str,
        vmid: int,
        kind: str,
    ) -> Any:
        """Получить фактические сетевые интерфейсы работающего гостя."""
        if kind == "vm":
            return self.data(
                f"/nodes/{node}/qemu/{vmid}/agent/network-get-interfaces"
            )
        if kind == "lxc":
            return self.data(
                f"/nodes/{node}/lxc/{vmid}/interfaces"
            )
        raise InfraManagerError(
            f"Неизвестный тип гостя для чтения интерфейсов: {kind!r}"
        )

    def guest_management_ipv4(
        self,
        *,
        node: str,
        vmid: int,
        kind: str,
        subnet: str,
    ) -> ipaddress.IPv4Address | None:
        """Найти единственный IPv4 гостя в административной подсети."""
        interfaces = self.guest_interfaces(
            node=node,
            vmid=vmid,
            kind=kind,
        )
        return select_management_ipv4(interfaces, subnet)


def _interface_records(data: Any) -> list[dict[str, Any]]:
    """Нормализовать ответы PVE для VM и LXC к списку интерфейсов."""
    value = data.get("result") if isinstance(data, dict) and "result" in data else data
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [item for item in value.values() if isinstance(item, dict)]
    return []


def _modern_interface_ipv4s(
    interface: dict[str, Any],
) -> set[ipaddress.IPv4Address]:
    """Извлечь IPv4 из современного списка ip-addresses."""
    found: set[ipaddress.IPv4Address] = set()
    addresses = interface.get("ip-addresses")
    if not isinstance(addresses, list):
        return found

    for item in addresses:
        if not isinstance(item, dict):
            continue
        if item.get("ip-address-type") not in {"ipv4", "inet"}:
            continue
        try:
            address = ipaddress.ip_address(item.get("ip-address"))
        except (TypeError, ValueError):
            continue
        if isinstance(address, ipaddress.IPv4Address):
            found.add(address)
    return found


def _legacy_interface_ipv4(
    interface: dict[str, Any],
) -> ipaddress.IPv4Address | None:
    """Извлечь IPv4 из старого поля inet."""
    legacy = interface.get("inet")
    if not isinstance(legacy, str) or not legacy:
        return None
    try:
        address = ipaddress.ip_interface(legacy).ip
    except ValueError:
        return None
    return address if isinstance(address, ipaddress.IPv4Address) else None


def _interface_ipv4s(data: Any) -> set[ipaddress.IPv4Address]:
    """Извлечь IPv4 из современного и совместимого старого ответа PVE."""
    found: set[ipaddress.IPv4Address] = set()
    for interface in _interface_records(data):
        found.update(_modern_interface_ipv4s(interface))
        legacy = _legacy_interface_ipv4(interface)
        if legacy is not None:
            found.add(legacy)
    return found


def select_management_ipv4(
    interfaces: Any,
    subnet: str,
) -> ipaddress.IPv4Address | None:
    """Выбрать единственный IPv4 из административной подсети."""
    try:
        network = ipaddress.ip_network(subnet, strict=True)
    except ValueError as exc:
        raise InfraManagerError(
            f"Некорректная административная подсеть {subnet!r}: {exc}"
        ) from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise InfraManagerError(
            f"Административная подсеть должна быть IPv4: {subnet!r}"
        )

    candidates = sorted(
        address
        for address in _interface_ipv4s(interfaces)
        if address in network
        and address not in {network.network_address, network.broadcast_address}
    )

    if not candidates:
        return None
    if len(candidates) > 1:
        values = ", ".join(str(address) for address in candidates)
        raise InfraManagerError(
            "Найдено несколько IPv4 гостя в административной подсети: "
            + values
        )
    return candidates[0]


def permission_present(data: Any, privilege: str) -> bool:
    objects: list[dict[str, Any]] = []
    if isinstance(data, dict):
        objects.extend(
            value
            for value in data.values()
            if isinstance(value, dict)
        )
        if privilege in data:
            objects.append(data)
    elif isinstance(data, list):
        objects.extend(
            item for item in data if isinstance(item, dict)
        )

    return any(
        item.get(privilege) in (1, True)
        for item in objects
    )


def require_permissions(
    client: PveClient,
    path: str,
    required: set[str],
) -> None:
    data = client.permissions_at(path)
    missing = sorted(
        privilege
        for privilege in required
        if not permission_present(data, privilege)
    )
    if missing:
        raise InfraManagerError(
            f"На {path} отсутствуют обязательные privileges: "
            + " ".join(missing)
        )


def forbid_permissions(
    client: PveClient,
    path: str,
    forbidden: set[str],
) -> None:
    data = client.permissions_at(path)
    found = sorted(
        privilege
        for privilege in forbidden
        if permission_present(data, privilege)
    )
    if found:
        raise InfraManagerError(
            f"На {path} обнаружены запрещённые privileges: "
            + " ".join(found)
        )


def check_access(*, quiet: bool = False) -> int:
    if os.geteuid() != 0:
        raise InfraManagerError(
            "Проверка PVE access должна выполняться от root"
        )

    client = PveClient()
    version = client.data("/version")
    if not isinstance(version, dict) or not (
        version.get("version") or version.get("release")
    ):
        raise InfraManagerError(
            "PVE API token не прошёл проверку авторизации"
        )

    pool = client.data(f"/pools/{MANAGED_POOL}")
    if pool is None:
        raise InfraManagerError(f"Pool {MANAGED_POOL} недоступен")

    require_permissions(client, "/vms", VM_ADMIN_PRIVS)
    require_permissions(
        client,
        f"/pool/{MANAGED_POOL}",
        {"Pool.Audit", "VM.Allocate"},
    )
    require_permissions(
        client,
        "/storage/local",
        {
            "Datastore.Audit",
            "Datastore.AllocateSpace",
            "Datastore.AllocateTemplate",
        },
    )
    require_permissions(
        client,
        "/storage/local-lvm",
        {"Datastore.Audit", "Datastore.AllocateSpace"},
    )
    require_permissions(
        client,
        "/sdn/zones/localnetwork/vmbr0",
        {"SDN.Audit", "SDN.Use"},
    )
    forbid_permissions(client, "/", FORBIDDEN_ROOT_PRIVS)

    if not quiet:
        console.ok("PVE API access infra-manager соответствует контракту")
    return 0
