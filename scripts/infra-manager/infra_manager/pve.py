"""PVE HTTPS API client и проверка прав infra-manager."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console

PVE_ENV = Path("/etc/infra-manager/secrets/pve-api.env")
CA_BUNDLE = Path("/etc/infra-manager/ca/ca-bundle.crt")
MANAGED_POOL = "managed"

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
    ) -> None:
        if not nonempty(env_path):
            raise InfraManagerError(
                f"Не найден PVE credential: {env_path}"
            )
        if not nonempty(ca_path):
            raise InfraManagerError(f"Не найден CA bundle: {ca_path}")

        values = read_env_file(env_path)
        self.url = values.get("PVE_API_URL", "").rstrip("/")
        self.token_id = values.get("PVE_API_TOKEN_ID", "")
        self.token_secret = values.get("PVE_API_TOKEN_SECRET", "")
        if not self.url:
            raise InfraManagerError("PVE_API_URL пуст")
        if not self.token_id:
            raise InfraManagerError("PVE_API_TOKEN_ID пуст")
        if not self.token_secret:
            raise InfraManagerError("PVE_API_TOKEN_SECRET пуст")

        self.context = ssl.create_default_context(cafile=str(ca_path))

    def get(
        self,
        endpoint: str,
        *,
        query: dict[str, str] | None = None,
    ) -> Any:
        url = f"{self.url}/api2/json{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)

        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": (
                    f"PVEAPIToken={self.token_id}={self.token_secret}"
                ),
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=20,
                context=self.context,
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(
                "utf-8",
                errors="replace",
            ).strip()
            raise InfraManagerError(
                f"PVE API GET {endpoint} вернул HTTP {exc.code}: "
                f"{detail or '(пустой ответ)'}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InfraManagerError(
                f"PVE API GET {endpoint} недоступен: {exc}"
            ) from exc

        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfraManagerError(
                f"PVE API GET {endpoint} вернул некорректный JSON"
            ) from exc

    def data(
        self,
        endpoint: str,
        *,
        query: dict[str, str] | None = None,
    ) -> Any:
        payload = self.get(endpoint, query=query)
        if not isinstance(payload, dict) or "data" not in payload:
            raise InfraManagerError(
                f"PVE API GET {endpoint} не содержит data"
            )
        return payload["data"]

    def permissions_at(self, path: str) -> Any:
        return self.data(
            "/access/permissions",
            query={"path": path},
        )


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
