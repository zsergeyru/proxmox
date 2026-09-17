#!/usr/bin/env python3
"""Синхронизация management public keys по Debian VM/LXC с tag management-ssh."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from guest_config import (  # noqa: E402
    GuestConfigError,
    parse_network_config,
    resolve_effective_guest,
    vmid_address,
)

CONFIG_DIR = Path("/etc/proxmox-deployer")
SSH_DIR = CONFIG_DIR / "ssh"
TOKEN_FILE = CONFIG_DIR / "secrets/host-deploy.token"
DEPLOYER_KEY = SSH_DIR / "pve_guest_ed25519"
DEPLOYER_PUB = SSH_DIR / "pve_guest_ed25519.pub"
PVE_CA = CONFIG_DIR / "pve-root-ca.pem"
RUNTIME_DIR = Path("/var/lib/proxmox-deployer")
REGISTRY_DIR = RUNTIME_DIR / "public-keys"
AGGREGATE_FILE = REGISTRY_DIR / "management-authorized-keys"
DEPLOYER_REGISTRY_KEY = REGISTRY_DIR / "deployer.pub"
KNOWN_HOSTS = Path("/var/lib/pvedeploy/.ssh/known_hosts")
AUDIT_FILE = Path("/var/log/proxmox-deployer/audit/sync-management-keys.jsonl")
GUEST_CATALOG = Path("/etc/proxmox-guest/public-keys")
AUTHORIZED_KEYS = Path("/root/.ssh/authorized_keys")
BEGIN_MARKER = "# BEGIN PROXMOX-MANAGEMENT-KEYS"
END_MARKER = "# END PROXMOX-MANAGEMENT-KEYS"
EXPECTED_TOKEN_ID = "deployer@pve!host-deploy"
VMID_KEY_RE = re.compile(r"^[1-9][0-9]{2}\.pub$")
PRIVATE_MARKERS = (
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
)


class SyncError(RuntimeError):
    """Безопасное продолжение синхронизации невозможно."""


class UnsafeRemoteState(SyncError):
    """В госте обнаружено неоднозначное или потенциально чужое состояние."""


@dataclass(frozen=True)
class RegistryState:
    files: dict[str, bytes]
    aggregate: bytes
    aggregate_changed: bool
    fingerprints: dict[str, str]


@dataclass(frozen=True)
class Participant:
    vmid: int
    name: str
    kind: str
    node: str
    status: str
    address: str
    user: str
    port: int
    manifest: str | None


@dataclass(frozen=True)
class RemoteState:
    os_release: bytes
    auth_exists: bool
    authorized_keys: bytes
    catalog_exists: bool
    catalog_files: dict[str, bytes]


@dataclass
class PreparedParticipant:
    participant: Participant
    known_hosts_file: Path | None = None
    new_host_keys: list[str] = field(default_factory=list)
    remote: RemoteState | None = None
    desired_authorized_keys: bytes = b""
    needs_catalog: bool = False
    needs_authorized_keys: bool = False

    @property
    def needs_apply(self) -> bool:
        return self.needs_catalog or self.needs_authorized_keys


@dataclass(frozen=True)
class RunResult:
    total: int
    online: int
    changed: int
    unchanged: int
    pending: int


def fail(message: str) -> None:
    raise SyncError(message)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def run_command(
    args: list[str],
    *,
    input_text: str | None = None,
    timeout: int = 20,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"не удалось выполнить {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if len(detail) > 500:
            detail = detail[:500] + "…"
        raise SyncError(
            f"команда {args[0]} завершилась с кодом {proc.returncode}"
            + (f": {detail}" if detail else "")
        )
    return proc


def git_revision() -> str:
    proc = run_command(["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"])
    revision = proc.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        fail("не удалось определить точную Git revision sync-management-keys")
    expected = (
        os.environ.get("SYNC_MANAGEMENT_KEYS_SOURCE_REVISION")
        or os.environ.get("DEPLOY_GUEST_SOURCE_REVISION")
        or ""
    )
    if expected and expected != revision:
        fail(
            f"source revision sync-management-keys {revision} не совпадает "
            f"с зафиксированной revision {expected}"
        )
    return revision


def validate_repository() -> None:
    validator = ROOT / "scripts/validate_repo.py"
    proc = run_command([sys.executable, str(validator)], timeout=60, check=False)
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip()
        raise SyncError(
            "repository validator не пройден перед sync-management-keys"
            + (f":\n{detail}" if detail else "")
        )


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise SyncError(f"не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SyncError(f"{path} должен содержать YAML mapping")
    return value


def public_key_fingerprint(path: Path) -> str:
    proc = run_command(["ssh-keygen", "-lf", str(path), "-E", "sha256"])
    fields = proc.stdout.strip().split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        fail(f"не удалось получить SHA256 fingerprint для {path}")
    return fields[1]


def validate_public_key_file(path: Path, label: str) -> tuple[bytes, str]:
    if path.is_symlink() or not path.is_file():
        fail(f"{label} должен быть обычным файлом и не может быть symlink")
    data = path.read_bytes()
    if any(marker.encode() in data for marker in PRIVATE_MARKERS):
        fail(f"{label} содержит private-key material")
    lines = [line.strip() for line in data.splitlines() if line.strip()]
    if len(lines) != 1:
        fail(f"{label} должен содержать ровно один непустой OpenSSH public key")
    parts = lines[0].split()
    if len(parts) < 2 or parts[0] != b"ssh-ed25519":
        fail(f"{label} должен содержать Ed25519 public key без authorized_keys options")
    fingerprint = public_key_fingerprint(path)
    return lines[0] + b"\n", fingerprint


def load_registry() -> RegistryState:
    if REGISTRY_DIR.is_symlink() or not REGISTRY_DIR.is_dir():
        fail(f"канонический registry отсутствует или небезопасен: {REGISTRY_DIR}")
    if DEPLOYER_PUB.is_symlink() or not DEPLOYER_PUB.is_file():
        fail(f"штатный deployer public key отсутствует: {DEPLOYER_PUB}")

    allowed: list[str] = []
    for item in sorted(REGISTRY_DIR.iterdir(), key=lambda p: p.name):
        if item.name == "management-authorized-keys":
            if item.is_symlink() or not item.is_file():
                fail(f"{item} должен быть обычным файлом")
            continue
        if item.name == "deployer.pub" or VMID_KEY_RE.fullmatch(item.name):
            allowed.append(item.name)
            continue
        fail(f"неизвестный объект в management public-key registry: {item.name}")

    if "deployer.pub" not in allowed:
        fail("registry не содержит обязательный deployer.pub")

    ordered = ["deployer.pub"] + sorted(
        (name for name in allowed if name != "deployer.pub"),
        key=lambda name: int(name[:-4]),
    )
    files: dict[str, bytes] = {}
    fingerprints: dict[str, str] = {}
    seen: dict[str, str] = {}
    for name in ordered:
        line, fingerprint = validate_public_key_file(REGISTRY_DIR / name, name)
        if fingerprint in seen:
            fail(
                f"один management public key зарегистрирован дважды: "
                f"{seen[fingerprint]} и {name} ({fingerprint})"
            )
        seen[fingerprint] = name
        files[name] = line
        fingerprints[name] = fingerprint

    canonical_deployer_fp = public_key_fingerprint(DEPLOYER_PUB)
    if fingerprints["deployer.pub"] != canonical_deployer_fp:
        fail(
            "registry deployer.pub не соответствует штатной deployer identity; "
            "автоматическая ротация запрещена"
        )

    aggregate = b"".join(files[name] for name in ordered)
    if not aggregate.strip():
        fail("management registry не может быть пустым")
    current = AGGREGATE_FILE.read_bytes() if AGGREGATE_FILE.is_file() and not AGGREGATE_FILE.is_symlink() else None
    if AGGREGATE_FILE.exists() and (AGGREGATE_FILE.is_symlink() or not AGGREGATE_FILE.is_file()):
        fail(f"{AGGREGATE_FILE} существует в небезопасном состоянии")
    return RegistryState(
        files=files,
        aggregate=aggregate,
        aggregate_changed=current != aggregate,
        fingerprints=fingerprints,
    )


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def ensure_aggregate(registry: RegistryState, check_only: bool) -> None:
    if not registry.aggregate_changed:
        print("[OK] registry aggregate уже актуален")
        return
    if check_only:
        print("[CHECK] registry aggregate требует пересборки")
        return
    atomic_write(AGGREGATE_FILE, registry.aggregate, 0o644)
    print("[OK] registry aggregate атомарно пересобран")


def load_api_token() -> tuple[str, str]:
    if TOKEN_FILE.is_symlink() or not TOKEN_FILE.is_file():
        fail(f"не найден API token deployer: {TOKEN_FILE}")
    values: dict[str, str] = {}
    for raw in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if "=" not in raw:
            fail(f"некорректная строка в {TOKEN_FILE}")
        key, value = raw.split("=", 1)
        values[key] = value
    token_id = values.get("token_id", "")
    secret = values.get("token_secret", "")
    if token_id != EXPECTED_TOKEN_ID or not secret:
        fail(f"{TOKEN_FILE} не содержит ожидаемый {EXPECTED_TOKEN_ID}")
    return token_id, secret


def api_get(base_url: str, path: str, token_id: str, token_secret: str) -> Any:
    if not PVE_CA.is_file():
        fail(f"не найден PVE CA: {PVE_CA}")
    try:
        context = ssl.create_default_context(cafile=str(PVE_CA))
    except (OSError, ssl.SSLError) as exc:
        raise SyncError(f"не удалось загрузить runtime PVE CA {PVE_CA}: {exc}") from exc
    request = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, context=context, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"ошибка PVE API GET {path}: {exc}") from exc
    if not isinstance(payload, dict) or "data" not in payload:
        fail(f"PVE API GET {path} вернул неожиданный ответ")
    return payload["data"]


def parse_tags(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {item.strip() for item in value.split(";") if item.strip()}


def find_manifest(vmid: int) -> Path | None:
    matches = sorted((ROOT / "guests").glob(f"{vmid:03d}-*/guest.yaml"))
    if len(matches) > 1:
        fail(f"для VMID {vmid} найдено несколько guest.yaml")
    return matches[0] if matches else None


def participant_from_resource(
    resource: dict[str, Any],
    defaults: dict[str, Any],
) -> Participant:
    try:
        vmid = int(resource["vmid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SyncError(f"PVE resource без корректного VMID: {resource!r}") from exc
    if not 100 <= vmid <= 999:
        fail(f"management-ssh participant VMID {vmid} вне поддерживаемого диапазона 100-999")
    kind = str(resource.get("type", ""))
    if kind not in {"qemu", "lxc"}:
        fail(f"VMID {vmid}: неподдерживаемый PVE resource type {kind!r}")
    status = str(resource.get("status", "unknown"))
    node = str(resource.get("node", ""))
    if not node:
        fail(f"VMID {vmid}: PVE API не вернул node")
    name = str(resource.get("name") or f"vmid-{vmid}")

    network = parse_network_config(defaults)
    manifest = find_manifest(vmid)
    if manifest is None:
        address = str(vmid_address(vmid, network))
        return Participant(vmid, name, kind, node, status, address, "root", 22, None)

    source = read_yaml(manifest)
    if source.get("deployable") is not True:
        fail(f"VMID {vmid}: tagged object имеет manifest, но deployable != true")
    try:
        resolved = resolve_effective_guest(source, defaults, network)
    except GuestConfigError as exc:
        raise SyncError(f"VMID {vmid}: не удалось построить effective state: {exc}") from exc
    effective = resolved.effective
    expected_kind = "qemu" if effective.get("type") == "vm" else "lxc"
    if kind != expected_kind:
        fail(f"VMID {vmid}: PVE type {kind} не соответствует manifest type {effective.get('type')}")
    management = effective.get("management")
    if not isinstance(management, dict) or not isinstance(management.get("ssh"), dict):
        fail(f"VMID {vmid}: effective management.ssh отсутствует")
    ssh = management["ssh"]
    user = ssh.get("user")
    port = ssh.get("port")
    if user != "root" or port != 22:
        fail(f"VMID {vmid}: sync v1 поддерживает только management.ssh root:22")
    return Participant(
        vmid=vmid,
        name=name,
        kind=kind,
        node=node,
        status=status,
        address=str(resolved.management_ip),
        user=user,
        port=port,
        manifest=str(manifest.relative_to(ROOT)),
    )


def discover_participants(defaults: dict[str, Any]) -> list[Participant]:
    node = defaults.get("defaults", {}).get("node")
    if not isinstance(node, str) or not node:
        fail("guests/defaults.yaml не содержит defaults.node")
    token_id, token_secret = load_api_token()
    base_url = f"https://{node}:8006/api2/json"
    resources = api_get(base_url, "/cluster/resources?type=vm", token_id, token_secret)
    if not isinstance(resources, list):
        fail("PVE API /cluster/resources вернул не список")
    found: list[Participant] = []
    seen: set[int] = set()
    for resource in resources:
        if not isinstance(resource, dict):
            continue
        if "management-ssh" not in parse_tags(resource.get("tags")):
            continue
        participant = participant_from_resource(resource, defaults)
        if participant.vmid in seen:
            fail(f"PVE API вернул дублирующийся management-ssh VMID {participant.vmid}")
        seen.add(participant.vmid)
        found.append(participant)
    return sorted(found, key=lambda item: item.vmid)


def known_host_lookup_name(participant: Participant) -> str:
    return participant.address if participant.port == 22 else f"[{participant.address}]:{participant.port}"


def known_host_exists(participant: Participant) -> bool:
    if KNOWN_HOSTS.is_symlink() or not KNOWN_HOSTS.is_file():
        fail(f"SSH trust database отсутствует или небезопасна: {KNOWN_HOSTS}")
    proc = run_command(
        ["ssh-keygen", "-F", known_host_lookup_name(participant), "-f", str(KNOWN_HOSTS)],
        check=False,
    )
    if proc.returncode not in {0, 1}:
        fail(f"не удалось проверить known_hosts для VMID {participant.vmid}")
    return proc.returncode == 0 and bool(proc.stdout.strip())


def scan_host_keys(participant: Participant) -> list[str]:
    proc = run_command(
        [
            "ssh-keyscan",
            "-T",
            "5",
            "-p",
            str(participant.port),
            participant.address,
        ],
        timeout=10,
        check=False,
    )
    lines = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    valid = [line for line in lines if len(line.split()) >= 3]
    if not valid:
        raise SyncError(
            f"VMID {participant.vmid}: SSH host key не получен с "
            f"{participant.address}:{participant.port}"
        )
    return valid


def temporary_known_hosts(new_lines: list[str]) -> Path:
    existing = KNOWN_HOSTS.read_text(encoding="utf-8")
    fd, name = tempfile.mkstemp(prefix="sync-management-known-hosts.")
    path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        if existing:
            fh.write(existing)
            if not existing.endswith("\n"):
                fh.write("\n")
        for line in new_lines:
            fh.write(line + "\n")
    os.chmod(path, 0o600)
    return path


def ssh_base(participant: Participant, known_hosts_file: Path) -> list[str]:
    return [
        "ssh",
        "-i",
        str(DEPLOYER_KEY),
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={known_hosts_file}",
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "ConnectTimeout=7",
        "-p",
        str(participant.port),
        f"{participant.user}@{participant.address}",
    ]


REMOTE_INSPECT = r'''set -eu
[ "$(id -u)" -eq 0 ] || { echo 'not-root' >&2; exit 41; }
[ ! -L /root/.ssh ] || { echo '/root/.ssh is symlink' >&2; exit 42; }
printf 'OS\t'
base64 -w0 /etc/os-release
printf '\n'
if [ -e /root/.ssh/authorized_keys ]; then
    [ -f /root/.ssh/authorized_keys ] && [ ! -L /root/.ssh/authorized_keys ] || { echo 'unsafe authorized_keys' >&2; exit 43; }
    printf 'AUTH\t1\t'
    base64 -w0 /root/.ssh/authorized_keys
    printf '\n'
else
    printf 'AUTH\t0\t\n'
fi
catalog=/etc/proxmox-guest/public-keys
if [ -e "$catalog" ]; then
    [ -d "$catalog" ] && [ ! -L "$catalog" ] || { echo 'unsafe public-key catalog' >&2; exit 44; }
    printf 'CATALOG\t1\n'
    while IFS= read -r entry; do
        [ -n "$entry" ] || continue
        path="$catalog/$entry"
        [ -f "$path" ] && [ ! -L "$path" ] || { echo "unsafe catalog entry: $entry" >&2; exit 45; }
        printf 'FILE\t'
        printf '%s' "$entry" | base64 -w0
        printf '\t'
        base64 -w0 "$path"
        printf '\n'
    done < <(find "$catalog" -mindepth 1 -maxdepth 1 -printf '%f\n' | LC_ALL=C sort)
else
    printf 'CATALOG\t0\n'
fi
'''


def decode_b64(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise UnsafeRemoteState(f"некорректный base64 от гостя: {label}") from exc


def parse_remote_inspection(text: str, participant: Participant) -> RemoteState:
    os_release: bytes | None = None
    auth_exists = False
    authorized = b""
    catalog_exists = False
    catalog_files: dict[str, bytes] = {}
    seen_catalog = False
    for line in text.splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        if parts[0] == "OS" and len(parts) == 2:
            os_release = decode_b64(parts[1], "os-release")
        elif parts[0] == "AUTH" and len(parts) == 3:
            auth_exists = parts[1] == "1"
            authorized = decode_b64(parts[2], "authorized_keys") if auth_exists else b""
        elif parts[0] == "CATALOG" and len(parts) == 2:
            catalog_exists = parts[1] == "1"
            seen_catalog = True
        elif parts[0] == "FILE" and len(parts) == 3:
            name = decode_b64(parts[1], "catalog filename").decode("utf-8", errors="strict")
            catalog_files[name] = decode_b64(parts[2], name)
        else:
            raise UnsafeRemoteState(
                f"VMID {participant.vmid}: неожиданный формат SSH inspection"
            )
    if os_release is None or not seen_catalog:
        raise UnsafeRemoteState(f"VMID {participant.vmid}: неполный SSH inspection")
    return RemoteState(os_release, auth_exists, authorized, catalog_exists, catalog_files)


def os_release_id(data: bytes) -> str:
    for raw in data.decode("utf-8", errors="replace").splitlines():
        if raw.startswith("ID="):
            return raw[3:].strip().strip('"\'')
    return ""


def inspect_remote(participant: Participant, known_hosts_file: Path) -> RemoteState:
    proc = run_command(
        ssh_base(participant, known_hosts_file) + ["bash", "-s"],
        input_text=REMOTE_INSPECT,
        timeout=20,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip()
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: verified root SSH inspection не выполнен"
            + (f": {detail[:400]}" if detail else "")
        )
    state = parse_remote_inspection(proc.stdout, participant)
    if os_release_id(state.os_release) != "debian":
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: management-ssh participant не является Debian"
        )
    return state


def validate_remote_catalog(state: RemoteState, participant: Participant) -> None:
    for name in state.catalog_files:
        if name == "deployer.pub" or name == "management-authorized-keys" or VMID_KEY_RE.fullmatch(name):
            continue
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: неизвестный файл в guest public-key catalog: {name}"
        )


def render_authorized_keys(current: bytes, aggregate: bytes, participant: Participant) -> bytes:
    try:
        text = current.decode("utf-8")
        aggregate_text = aggregate.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: authorized_keys должен быть UTF-8/ASCII text"
        ) from exc
    lines = text.splitlines()
    begin = [idx for idx, line in enumerate(lines) if line == BEGIN_MARKER]
    end = [idx for idx, line in enumerate(lines) if line == END_MARKER]
    if not begin and not end:
        before = lines
        after: list[str] = []
    elif len(begin) == 1 and len(end) == 1 and begin[0] < end[0]:
        before = lines[: begin[0]]
        after = lines[end[0] + 1 :]
    else:
        raise UnsafeRemoteState(
            f"VMID {participant.vmid}: managed authorized_keys block повреждён или неоднозначен"
        )
    aggregate_lines = [line for line in aggregate_text.splitlines() if line.strip()]
    output = before + [BEGIN_MARKER] + aggregate_lines + [END_MARKER] + after
    return ("\n".join(output) + "\n").encode("utf-8")


def desired_catalog(registry: RegistryState) -> dict[str, bytes]:
    result = dict(registry.files)
    result["management-authorized-keys"] = registry.aggregate
    return result


def prepare_participant(
    participant: Participant,
    registry: RegistryState,
    temp_paths: list[Path],
) -> PreparedParticipant:
    prepared = PreparedParticipant(participant=participant)
    if participant.status != "running":
        return prepared

    if not DEPLOYER_KEY.is_file() or DEPLOYER_KEY.is_symlink():
        fail(f"deployer private SSH key отсутствует или небезопасен: {DEPLOYER_KEY}")

    if known_host_exists(participant):
        known_hosts_file = KNOWN_HOSTS
    else:
        prepared.new_host_keys = scan_host_keys(participant)
        known_hosts_file = temporary_known_hosts(prepared.new_host_keys)
        temp_paths.append(known_hosts_file)
    prepared.known_hosts_file = known_hosts_file
    state = inspect_remote(participant, known_hosts_file)
    validate_remote_catalog(state, participant)
    prepared.remote = state
    target_catalog = desired_catalog(registry)
    prepared.needs_catalog = state.catalog_files != target_catalog
    prepared.desired_authorized_keys = render_authorized_keys(
        state.authorized_keys if state.auth_exists else b"",
        registry.aggregate,
        participant,
    )
    prepared.needs_authorized_keys = (
        not state.auth_exists or state.authorized_keys != prepared.desired_authorized_keys
    )
    return prepared


def persist_new_host_keys(prepared: list[PreparedParticipant]) -> None:
    additions: list[str] = []
    for item in prepared:
        additions.extend(item.new_host_keys)
    if not additions:
        return
    current = KNOWN_HOSTS.read_bytes()
    data = bytearray(current)
    if data and not data.endswith(b"\n"):
        data.extend(b"\n")
    for line in additions:
        data.extend(line.encode("utf-8") + b"\n")
    atomic_write(KNOWN_HOSTS, bytes(data), 0o600)
    print(f"[OK] сохранены новые SSH host keys: {len(additions)} строк")


def quote_b64(data: bytes) -> str:
    return shlex.quote(base64.b64encode(data).decode("ascii"))


def build_apply_script(
    prepared: PreparedParticipant,
    registry: RegistryState,
) -> str:
    assert prepared.remote is not None
    expected_auth_exists = "1" if prepared.remote.auth_exists else "0"
    expected_auth_sha = sha256_bytes(prepared.remote.authorized_keys)
    target_catalog = desired_catalog(registry)
    lines = [
        "set -eu",
        "umask 077",
        "[ \"$(id -u)\" -eq 0 ] || exit 60",
        "[ ! -L /root/.ssh ] || { echo 'unsafe /root/.ssh' >&2; exit 61; }",
        f"expected_auth_exists={shlex.quote(expected_auth_exists)}",
        f"expected_auth_sha={shlex.quote(expected_auth_sha)}",
        "if [ -e /root/.ssh/authorized_keys ]; then",
        "  [ -f /root/.ssh/authorized_keys ] && [ ! -L /root/.ssh/authorized_keys ] || exit 62",
        "  current_auth_exists=1",
        "  current_auth_sha=$(sha256sum /root/.ssh/authorized_keys | awk '{print $1}')",
        "else",
        "  current_auth_exists=0",
        "  current_auth_sha=$(printf '' | sha256sum | awk '{print $1}')",
        "fi",
        "[ \"$current_auth_exists\" = \"$expected_auth_exists\" ] && [ \"$current_auth_sha\" = \"$expected_auth_sha\" ] || { echo 'authorized_keys changed after preflight' >&2; exit 63; }",
        "parent=/etc/proxmox-guest",
        "catalog=$parent/public-keys",
        "[ ! -L \"$catalog\" ] || exit 64",
        "if [ -e \"$catalog\" ]; then [ -d \"$catalog\" ] || exit 65; fi",
        "install -d -o root -g root -m 0755 \"$parent\"",
        "new=$(mktemp -d \"$parent/.public-keys.new.XXXXXX\")",
        "trap 'rm -rf \"$new\"' EXIT",
        "chmod 0755 \"$new\"",
    ]
    for name, data in sorted(
        target_catalog.items(),
        key=lambda item: (0 if item[0] == "deployer.pub" else 2 if item[0] == "management-authorized-keys" else 1, item[0]),
    ):
        lines.extend(
            [
                f"printf '%s' {quote_b64(data)} | base64 -d >\"$new/{name}\"",
                f"chmod 0644 \"$new/{name}\"",
            ]
        )
    lines.extend(
        [
            "old=''",
            "if [ -d \"$catalog\" ]; then old=$(mktemp -d \"$parent/.public-keys.old.XXXXXX\"); rmdir \"$old\"; mv \"$catalog\" \"$old\"; fi",
            "mv \"$new\" \"$catalog\"",
            "new=''",
            "[ -z \"$old\" ] || rm -rf \"$old\"",
            "install -d -o root -g root -m 0700 /root/.ssh",
            "auth_tmp=$(mktemp /root/.ssh/.authorized_keys.sync.XXXXXX)",
            f"printf '%s' {quote_b64(prepared.desired_authorized_keys)} | base64 -d >\"$auth_tmp\"",
            "chown root:root \"$auth_tmp\"",
            "chmod 0600 \"$auth_tmp\"",
            "mv \"$auth_tmp\" /root/.ssh/authorized_keys",
            "trap - EXIT",
        ]
    )
    return "\n".join(lines) + "\n"


def apply_participant(prepared: PreparedParticipant, registry: RegistryState) -> None:
    assert prepared.remote is not None
    participant = prepared.participant
    script = build_apply_script(prepared, registry)
    proc = run_command(
        ssh_base(participant, KNOWN_HOSTS) + ["bash", "-s"],
        input_text=script,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.strip()
        raise SyncError(
            f"VMID {participant.vmid}: guest mutation failed rc={proc.returncode}"
            + (f": {detail[:400]}" if detail else "")
        )
    verified = inspect_remote(participant, KNOWN_HOSTS)
    validate_remote_catalog(verified, participant)
    if verified.catalog_files != desired_catalog(registry):
        raise SyncError(f"VMID {participant.vmid}: public-key catalog verify failed")
    if not verified.auth_exists or verified.authorized_keys != prepared.desired_authorized_keys:
        raise SyncError(f"VMID {participant.vmid}: authorized_keys verify failed")


def append_audit(payload: dict[str, Any]) -> None:
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"[WARN] audit log не записан: {exc}", file=sys.stderr)


def sync(check_only: bool) -> RunResult:
    revision = git_revision()
    validate_repository()
    defaults = read_yaml(ROOT / "guests/defaults.yaml")
    registry = load_registry()
    ensure_aggregate(registry, check_only)

    participants = discover_participants(defaults)
    if not participants:
        print("[OK] management-ssh participants отсутствуют")
        append_audit(
            {
                "time": now_iso(),
                "component": "sync-management-keys",
                "mode": "check" if check_only else "apply",
                "revision": revision,
                "result": "success",
                "participants": 0,
                "registry_fingerprints": registry.fingerprints,
            }
        )
        return RunResult(0, 0, 0, 0, 0)

    print(f"[INFO] management-ssh participants: {len(participants)}")
    prepared: list[PreparedParticipant] = []
    temp_paths: list[Path] = []
    try:
        for participant in participants:
            if participant.status != "running":
                print(
                    f"[PENDING] VMID {participant.vmid} {participant.name}: "
                    f"status={participant.status}, guest не запускается ради sync"
                )
                prepared.append(PreparedParticipant(participant=participant))
                continue
            item = prepare_participant(participant, registry, temp_paths)
            prepared.append(item)
            trust = "new-host-key" if item.new_host_keys else "strict-known-host"
            action = "APPLY" if item.needs_apply else "NO CHANGE"
            print(
                f"[{action}] VMID {participant.vmid} {participant.name} "
                f"{participant.address}:{participant.port} trust={trust}"
            )

        # До этой точки не было ни одной guest mutation и не изменялся known_hosts.
        if check_only:
            changed = sum(1 for item in prepared if item.participant.status == "running" and item.needs_apply)
            unchanged = sum(1 for item in prepared if item.participant.status == "running" and not item.needs_apply)
            pending = sum(1 for item in prepared if item.participant.status != "running")
            result = RunResult(len(prepared), changed + unchanged, changed, unchanged, pending)
            append_audit(
                {
                    "time": now_iso(),
                    "component": "sync-management-keys",
                    "mode": "check",
                    "revision": revision,
                    "result": "pending" if pending else "success",
                    "total": result.total,
                    "changed": changed,
                    "unchanged": unchanged,
                    "pending": pending,
                    "registry_fingerprints": registry.fingerprints,
                }
            )
            return result

        persist_new_host_keys(prepared)
        changed = 0
        unchanged = 0
        pending = 0
        for item in prepared:
            if item.participant.status != "running":
                pending += 1
                continue
            if not item.needs_apply:
                unchanged += 1
                continue
            apply_participant(item, registry)
            changed += 1
            print(f"[OK] VMID {item.participant.vmid}: management keys synchronized and verified")

        result = RunResult(len(prepared), changed + unchanged, changed, unchanged, pending)
        append_audit(
            {
                "time": now_iso(),
                "component": "sync-management-keys",
                "mode": "apply",
                "revision": revision,
                "result": "pending" if pending else "success",
                "total": result.total,
                "changed": changed,
                "unchanged": unchanged,
                "pending": pending,
                "registry_fingerprints": registry.fingerprints,
            }
        )
        return result
    finally:
        for path in temp_paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync-management-keys",
        description="Синхронизировать management public keys по tagged Debian VM/LXC.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="полный read-only preflight без изменения aggregate, known_hosts и гостей",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    started = now_iso()
    try:
        result = sync(args.check)
    except UnsafeRemoteState as exc:
        print(f"ОШИБКА БЕЗОПАСНОСТИ: {exc}", file=sys.stderr)
        append_audit(
            {
                "time": started,
                "component": "sync-management-keys",
                "mode": "check" if args.check else "apply",
                "result": "blocked",
                "error": str(exc),
            }
        )
        return 3
    except SyncError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        append_audit(
            {
                "time": started,
                "component": "sync-management-keys",
                "mode": "check" if args.check else "apply",
                "result": "failed",
                "error": str(exc),
            }
        )
        return 4
    except Exception as exc:  # safety net without secret dumps
        print(f"ОШИБКА: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        append_audit(
            {
                "time": started,
                "component": "sync-management-keys",
                "mode": "check" if args.check else "apply",
                "result": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        return 4

    print(
        f"[ИТОГ] total={result.total} online={result.online} changed={result.changed} "
        f"unchanged={result.unchanged} pending={result.pending}"
    )
    return 2 if result.pending else 0


if __name__ == "__main__":
    sys.exit(main())
