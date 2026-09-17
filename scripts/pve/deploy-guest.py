#!/usr/bin/env python3
"""Детерминированный PLAN/APPLY deployer одной Proxmox VM/LXC из Git desired state."""

from __future__ import annotations

import argparse
import base64
import dataclasses
import importlib.util
import json
import os
import re
import shlex
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parents[2]
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from guest_config import GuestConfigError, resolve_effective_guest  # noqa: E402

CONFIG_DIR = Path("/etc/proxmox-deployer")
LOCAL_CONFIG = CONFIG_DIR / "config.yaml"
TOKEN_FILE = CONFIG_DIR / "secrets/host-deploy.token"
PVE_CA = CONFIG_DIR / "pve-root-ca.pem"
SSH_DIR = CONFIG_DIR / "ssh"
DEPLOYER_KEY = SSH_DIR / "pve_guest_ed25519"
DEPLOYER_PUB = SSH_DIR / "pve_guest_ed25519.pub"
PROJECT_REPO_PUB = SSH_DIR / "github_proxmox_repo_ed25519.pub"
GITHUB_KNOWN_HOSTS = SSH_DIR / "known_hosts"
KNOWN_HOSTS = Path("/var/lib/pvedeploy/.ssh/known_hosts")
REGISTRY_DIR = Path("/var/lib/proxmox-deployer/public-keys")
AUDIT_FILE = Path("/var/log/proxmox-deployer/audit/deploy-guest.jsonl")
SYNC_SOURCE = ROOT / "scripts/pve/sync-management-keys.py"
EFFECTIVE_SCHEMA = ROOT / "schemas/guest-effective.schema.yaml"

EXPECTED_TOKEN_ID = "deployer@pve!host-deploy"
EXPECTED_REPO = "zsergeyru/proxmox"
OWNERSHIP_TAG = "proxmox-deployer"
MANAGEMENT_TAG = "management-ssh"
INCOMPLETE_TAG = "deploy-incomplete"
SERVICE_BEGIN = "[proxmox-deployer]"
SERVICE_LINES = ("managed-by=proxmox-deployer", "source-repo=zsergeyru/proxmox")
PROJECT_CREDENTIAL = "/etc/proxmox-guest/credentials/github-proxmox-read"
PROJECT_KNOWN_HOSTS = "/etc/proxmox-guest/ssh/github-proxmox-known_hosts"
PROJECT_SSH_CONFIG = "/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf"
PROJECT_ALIAS = "github-proxmox-read"
PROJECT_URL = "git@github.com:zsergeyru/proxmox.git"
PROJECT_REWRITTEN_URL = "git@github-proxmox-read:zsergeyru/proxmox.git"
PROJECT_CONFIG_MARKER = "# managed-by=proxmox-deployer project-repo-read"
PROJECT_GIT_CONFIG = "/etc/gitconfig"
PROJECT_GIT_BEGIN = "# BEGIN PROXMOX-PROJECT-REPO-READ"
PROJECT_GIT_END = "# END PROXMOX-PROJECT-REPO-READ"
MANAGEMENT_PRIVATE = "/etc/proxmox-guest/ssh/management_ed25519"
MANAGEMENT_PUBLIC = MANAGEMENT_PRIVATE + ".pub"

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_FAILED = 3
API_TIMEOUT = 20
TASK_TIMEOUT = 600
SSH_READY_TIMEOUT = 180


class DeployError(RuntimeError):
    """Deploy нельзя безопасно продолжить."""


class BlockedError(DeployError):
    """Нужна явная ручная операция, запрещённая обычному deploy."""


@dataclasses.dataclass(frozen=True)
class Desired:
    vmid: int
    manifest: Path
    source: dict[str, Any]
    effective: dict[str, Any]
    management_ip: str
    bootstrap_capabilities: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Actual:
    exists: bool
    kind: str | None = None
    node: str | None = None
    status: str | None = None
    config: dict[str, Any] | None = None
    pool: str | None = None


@dataclasses.dataclass(frozen=True)
class PlanItem:
    area: str
    action: str
    classification: str
    detail: str

    @property
    def changes(self) -> bool:
        return self.action not in {"NO CHANGE", "NOT REQUESTED"}

    @property
    def blocked(self) -> bool:
        return self.action == "BLOCKED" or self.classification == "FORBIDDEN"


@dataclasses.dataclass
class Plan:
    desired: Desired
    actual: Actual
    items: list[PlanItem]

    @property
    def blocked(self) -> bool:
        return any(item.blocked for item in self.items)

    @property
    def changes(self) -> bool:
        return any(item.changes for item in self.items)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def safe_message(value: object, limit: int = 800) -> str:
    return str(value).replace("\n", " ")[:limit]


def audit(vmid: int, revision: str, mode: str, phase: str, result: str, message: str) -> None:
    record = {
        "time": now_iso(),
        "vmid": vmid,
        "revision": revision,
        "mode": mode,
        "phase": phase,
        "result": result,
        "message": safe_message(message),
    }
    try:
        AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        print(f"[WARN] audit log не записан: {exc}", file=sys.stderr)


def run_command(
    args: list[str],
    *,
    input_bytes: bytes | None = None,
    input_text: str | None = None,
    timeout: int = 30,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    if input_bytes is not None and input_text is not None:
        raise ValueError("input_bytes and input_text mutually exclusive")
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": timeout,
        "check": False,
        "env": env,
    }
    if input_bytes is not None:
        kwargs["input"] = input_bytes
    else:
        kwargs["input"] = input_text
        kwargs["text"] = True
    try:
        proc = subprocess.run(args, **kwargs)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"не удалось выполнить {args[0]}: {exc}") from exc
    if check and proc.returncode != 0:
        stderr = proc.stderr.decode(errors="replace") if isinstance(proc.stderr, bytes) else proc.stderr
        stdout = proc.stdout.decode(errors="replace") if isinstance(proc.stdout, bytes) else proc.stdout
        detail = (stderr or stdout or "").strip()
        raise DeployError(
            f"команда {args[0]} завершилась с кодом {proc.returncode}"
            + (f": {detail[:800]}" if detail else "")
        )
    return proc


def git_revision() -> str:
    proc = run_command(
        ["git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), "rev-parse", "HEAD"]
    )
    revision = proc.stdout.strip()
    expected = os.environ.get("DEPLOY_GUEST_SOURCE_REVISION", "")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DeployError("не удалось определить точную Git revision deploy-guest")
    if not re.fullmatch(r"[0-9a-f]{40}", expected):
        raise DeployError("DEPLOY_GUEST_SOURCE_REVISION не задан root-wrapper")
    if revision != expected:
        raise DeployError(f"source revision {revision} не совпадает с wrapper revision {expected}")
    return revision


def validate_repository() -> None:
    proc = run_command(
        [sys.executable, str(ROOT / "scripts/validate_repo.py")], timeout=90, check=False
    )
    if proc.returncode:
        detail = (proc.stdout + proc.stderr).strip()
        raise DeployError("repository validator не пройден" + (f": {detail[:1500]}" if detail else ""))


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DeployError(f"не удалось прочитать {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DeployError(f"{path} должен содержать YAML mapping")
    return value


def load_desired(vmid: int) -> Desired:
    matches = sorted((ROOT / "guests").glob(f"{vmid:03d}-*/guest.yaml"))
    if len(matches) != 1:
        raise DeployError(f"для VMID {vmid} ожидается ровно один guest.yaml, найдено {len(matches)}")
    manifest = matches[0]
    source = read_yaml(manifest)
    if source.get("vmid") != vmid or source.get("deployable") is not True:
        raise DeployError(f"VMID {vmid}: manifest не соответствует deployable contract")
    defaults = read_yaml(ROOT / "guests/defaults.yaml")
    try:
        resolved = resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        raise DeployError(f"не удалось построить effective state VMID {vmid}: {exc}") from exc
    schema = read_yaml(EFFECTIVE_SCHEMA)
    errors = sorted(
        Draft202012Validator(schema).iter_errors(resolved.effective),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        detail = "; ".join(
            f"{'.'.join(str(x) for x in e.absolute_path) or '<root>'}: {e.message}"
            for e in errors[:8]
        )
        raise DeployError(f"effective state не проходит schema: {detail}")
    return Desired(
        vmid=vmid,
        manifest=manifest.relative_to(ROOT),
        source=source,
        effective=resolved.effective,
        management_ip=str(resolved.management_ip),
        bootstrap_capabilities=resolved.bootstrap_capabilities,
    )


def load_local_config() -> dict[str, Any]:
    cfg = read_yaml(LOCAL_CONFIG)
    if str(cfg.get("repo")) != EXPECTED_REPO:
        raise DeployError(f"{LOCAL_CONFIG}: неожиданный repo")
    if str(cfg.get("host_deploy_identity")) != EXPECTED_TOKEN_ID:
        raise DeployError(f"{LOCAL_CONFIG}: неожиданный host_deploy_identity")
    return cfg


def load_token() -> tuple[str, str]:
    if TOKEN_FILE.is_symlink() or not TOKEN_FILE.is_file():
        raise DeployError(f"не найден API token: {TOKEN_FILE}")
    values: dict[str, str] = {}
    for raw in TOKEN_FILE.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        if "=" not in raw:
            raise DeployError(f"некорректная строка в {TOKEN_FILE}")
        key, value = raw.split("=", 1)
        values[key] = value
    token_id = values.get("token_id", "")
    secret = values.get("token_secret", "")
    if token_id != EXPECTED_TOKEN_ID or not secret:
        raise DeployError(f"{TOKEN_FILE} не содержит ожидаемый {EXPECTED_TOKEN_ID}")
    return token_id, secret


def public_key_line(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise DeployError(f"public key отсутствует или является symlink: {path}")
    lines = [line.strip() for line in path.read_bytes().splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith(b"ssh-ed25519 "):
        raise DeployError(f"{path} должен содержать один Ed25519 public key")
    return lines[0]


def fingerprint_key_bytes(line: bytes) -> str:
    with tempfile.NamedTemporaryFile(mode="wb", delete=True) as fh:
        fh.write(line + (b"\n" if not line.endswith(b"\n") else b""))
        fh.flush()
        proc = run_command(["ssh-keygen", "-lf", fh.name, "-E", "sha256"])
    fields = proc.stdout.strip().split()
    if len(fields) < 2 or not fields[1].startswith("SHA256:"):
        raise DeployError("не удалось получить fingerprint public key")
    return fields[1]


def validate_deployer_identity() -> str:
    if DEPLOYER_KEY.is_symlink() or not DEPLOYER_KEY.is_file():
        raise DeployError(f"deployer private key отсутствует: {DEPLOYER_KEY}")
    if stat.S_IMODE(DEPLOYER_KEY.stat().st_mode) & 0o077:
        raise DeployError(f"{DEPLOYER_KEY} должен быть mode 0600")
    derived = run_command(["ssh-keygen", "-y", "-f", str(DEPLOYER_KEY)]).stdout.strip().split()
    expected = public_key_line(DEPLOYER_PUB).decode().split()
    if len(derived) < 2 or derived[:2] != expected[:2]:
        raise DeployError("deployer private/public key не соответствуют друг другу")
    return fingerprint_key_bytes((" ".join(expected[:2])).encode())


def validate_known_hosts() -> None:
    if KNOWN_HOSTS.is_symlink() or not KNOWN_HOSTS.is_file():
        raise DeployError(f"guest known_hosts отсутствует или небезопасен: {KNOWN_HOSTS}")
    if stat.S_IMODE(KNOWN_HOSTS.stat().st_mode) != 0o600:
        raise DeployError(f"{KNOWN_HOSTS} должен иметь mode 0600")


def load_sync_module():
    spec = importlib.util.spec_from_file_location("proxmox_sync_management_keys", SYNC_SOURCE)
    if spec is None or spec.loader is None:
        raise DeployError(f"не удалось загрузить {SYNC_SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_registry():
    module = load_sync_module()
    try:
        registry = module.load_registry()
    except Exception as exc:
        raise DeployError(f"management registry не прошёл валидацию: {exc}") from exc
    return module, registry


class PveApi:
    def __init__(self, node: str, token_id: str, token_secret: str):
        self.node = node
        self.base = f"https://{node}:8006/api2/json"
        self.header = {"Authorization": f"PVEAPIToken={token_id}={token_secret}"}
        try:
            self.context = ssl.create_default_context(cafile=str(PVE_CA))
        except (OSError, ssl.SSLError) as exc:
            raise DeployError(f"не удалось загрузить runtime PVE CA {PVE_CA}: {exc}") from exc

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        body = None
        headers = dict(self.header)
        if data is not None:
            normalized = {
                key: str(int(value)) if isinstance(value, bool) else str(value)
                for key, value in data.items()
                if value is not None
            }
            body = urllib.parse.urlencode(normalized).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, context=self.context, timeout=API_TIMEOUT) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:1200]
            raise DeployError(
                f"PVE API {method} {path}: HTTP {exc.code} {exc.reason}"
                + (f": {detail}" if detail else "")
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            reason = getattr(exc, "reason", exc)
            raise DeployError(f"PVE API {method} {path}: {type(reason).__name__}: {reason!r}") from exc
        if not isinstance(payload, dict) or "data" not in payload:
            raise DeployError(f"PVE API {method} {path} вернул неожиданный ответ")
        return payload["data"]

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, data)

    def put(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self.request("PUT", path, data)

    def wait_task(self, upid: Any, timeout: int = TASK_TIMEOUT) -> None:
        if not isinstance(upid, str) or not upid.startswith("UPID:"):
            return
        parts = upid.split(":")
        if len(parts) < 3 or not parts[1]:
            raise DeployError(f"некорректный PVE UPID: {safe_message(upid)}")
        node = parts[1]
        encoded = urllib.parse.quote(upid, safe="")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.get(f"/nodes/{urllib.parse.quote(node)}/tasks/{encoded}/status")
            if isinstance(state, dict) and state.get("status") == "stopped":
                if state.get("exitstatus") != "OK":
                    raise DeployError(f"PVE task завершилась со статусом {state.get('exitstatus')!r}")
                return
            time.sleep(2)
        raise DeployError(f"PVE task timeout после {timeout}s")


def parse_tags(value: object) -> set[str]:
    return {item.strip() for item in str(value or "").split(";") if item.strip()}


def format_tags(tags: set[str]) -> str:
    return ";".join(sorted(tags))


def service_description(desired: Desired) -> str:
    return (
        f"{desired.effective['description']}\n\n{SERVICE_BEGIN}\n"
        f"{SERVICE_LINES[0]}\n{SERVICE_LINES[1]}\nmanifest={desired.manifest.as_posix()}"
    )


def has_ownership(config: dict[str, Any], desired: Desired) -> bool:
    tags = parse_tags(config.get("tags"))
    description = str(config.get("description") or "")
    required = (SERVICE_BEGIN, *SERVICE_LINES, f"manifest={desired.manifest.as_posix()}")
    return OWNERSHIP_TAG in tags and all(line in description for line in required)


def desired_final_tags(config: dict[str, Any] | None = None) -> set[str]:
    tags = parse_tags((config or {}).get("tags"))
    tags.update({OWNERSHIP_TAG, MANAGEMENT_TAG})
    tags.discard(INCOMPLETE_TAG)
    return tags


def desired_incomplete_tags(config: dict[str, Any] | None = None) -> set[str]:
    tags = desired_final_tags(config)
    tags.add(INCOMPLETE_TAG)
    return tags


def parse_size_bytes(text: object) -> int | None:
    if not isinstance(text, str):
        return None
    match = re.search(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT])(?:,|$)", text, re.I)
    if not match:
        return None
    powers = {"K": 1, "M": 2, "G": 3, "T": 4}
    return int(float(match.group(1)) * (1024 ** powers[match.group(2).upper()]))


def storage_from_volume(text: object) -> str | None:
    if not isinstance(text, str):
        return None
    volume = text.split(",", 1)[0]
    return volume.split(":", 1)[0] if ":" in volume else None


def boolish(value: object) -> bool:
    return str(value).lower() in {"1", "true", "yes", "on"}


def option_enabled(value: object) -> bool:
    """Parse PVE boolean option whose value may carry comma-separated suboptions."""
    return str(value or "").split(",", 1)[0].lower() in {"1", "true", "yes", "on"}


def preserve_boolean_suboptions(value: object, enabled: bool) -> str:
    parts = [part for part in str(value or "").split(",") if part]
    tail = parts[1:] if parts else []
    return ",".join(["1" if enabled else "0", *tail])


def expected_lxc_features(desired: Desired, current: object) -> str:
    values: dict[str, str] = {}
    extras: list[str] = []
    for part in str(current or "").split(","):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            if key in {"keyctl", "nesting"}:
                values[key] = value
                continue
        extras.append(part)
    values["keyctl"] = str(int(bool(desired.effective["lxc"]["features"]["keyctl"])))
    values["nesting"] = str(int(bool(desired.effective["lxc"]["features"]["nesting"])))
    return ",".join([*extras, f"keyctl={values['keyctl']}", f"nesting={values['nesting']}"])


def update_kv_option(text: str, key: str, value: str) -> str:
    parts = text.split(",") if text else []
    out: list[str] = []
    replaced = False
    for part in parts:
        if part.startswith(key + "="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(part)
    if not replaced:
        out.append(f"{key}={value}")
    return ",".join(part for part in out if part)


def expected_ipconfig(desired: Desired) -> str:
    net = desired.effective["network"]
    return f"ip={net['ipv4']['address']},gw={net['gateway']}"


def expected_lxc_net(desired: Desired, current: str | None = None) -> str:
    net = desired.effective["network"]
    text = current or "name=eth0"
    for key, value in (
        ("name", "eth0"),
        ("bridge", str(net["bridge"])),
        ("ip", str(net["ipv4"]["address"])),
        ("gw", str(net["gateway"])),
    ):
        text = update_kv_option(text, key, value)
    return text


def expected_vm_net(desired: Desired, current: str) -> str:
    bridge = str(desired.effective["network"]["bridge"])
    return update_kv_option(current or "virtio", "bridge", bridge)


def discover_actual(api: PveApi, desired: Desired) -> Actual:
    resources = api.get("/cluster/resources?type=vm")
    if not isinstance(resources, list):
        raise DeployError("PVE API cluster resources вернул не список")
    matches = [item for item in resources if int(item.get("vmid", -1)) == desired.vmid]
    if not matches:
        return Actual(False)
    if len(matches) != 1:
        raise BlockedError(f"VMID {desired.vmid}: найдено несколько PVE objects")
    resource = matches[0]
    kind = str(resource.get("type", ""))
    expected_kind = "qemu" if desired.effective["type"] == "vm" else "lxc"
    if kind != expected_kind:
        raise BlockedError(f"VMID {desired.vmid}: существует {kind}, manifest требует {expected_kind}")
    node = str(resource.get("node") or "")
    config = api.get(f"/nodes/{urllib.parse.quote(node)}/{kind}/{desired.vmid}/config")
    if not isinstance(config, dict):
        raise DeployError(f"VMID {desired.vmid}: PVE config не является mapping")
    if not has_ownership(config, desired):
        raise BlockedError(
            f"VMID {desired.vmid}: отсутствует полный ownership contract; automatic adoption запрещён"
        )
    pool = resource.get("pool")
    return Actual(
        True,
        kind=kind,
        node=node,
        status=str(resource.get("status") or "unknown"),
        config=config,
        pool=str(pool) if pool else None,
    )


def verify_sources(api: PveApi, desired: Desired) -> str | None:
    e = desired.effective
    node = str(e["node"])
    if node != api.node:
        raise BlockedError("cross-node deploy v1 запрещён")
    storage = str(e["resources"]["disk"]["storage"])
    status = api.get(f"/nodes/{urllib.parse.quote(node)}/storage/{urllib.parse.quote(storage)}/status")
    if not isinstance(status, dict) or not boolish(status.get("active", 1)):
        raise DeployError(f"storage {storage} недоступен")
    if str(e["network"]["bridge"]) != "vmbr0":
        raise BlockedError(f"bridge {e['network']['bridge']!r} не разрешён contract v1")
    pool = e.get("placement", {}).get("pool")
    if pool:
        api.get(f"/pools/{urllib.parse.quote(str(pool))}")
    if e["type"] == "vm":
        template_vmid = int(e["vm"]["source"]["template_vmid"])
        config = api.get(f"/nodes/{urllib.parse.quote(node)}/qemu/{template_vmid}/config")
        if not isinstance(config, dict) or not boolish(config.get("template")):
            raise DeployError(f"VM source {template_vmid} не является template")
        if not isinstance(config.get("net0"), str) or not config.get("net0"):
            raise DeployError(f"VM source {template_vmid} не содержит обязательный net0")
        if not isinstance(config.get("scsi0"), str) or not config.get("scsi0"):
            raise DeployError(f"VM source {template_vmid} не содержит обязательный scsi0")
        return None
    selector = str(e["lxc"]["source"]["ostemplate"])
    selector_storage = selector.split(":", 1)[0]
    content = api.get(
        f"/nodes/{urllib.parse.quote(node)}/storage/{urllib.parse.quote(selector_storage)}/content?content=vztmpl"
    )
    candidates = sorted(
        {
            str(item.get("volid"))
            for item in content
            if isinstance(item, dict)
            and isinstance(item.get("volid"), str)
            and (item["volid"] == selector or item["volid"].startswith(selector + "_") or selector in item["volid"])
        }
    )
    if len(candidates) != 1:
        raise DeployError(f"LXC template selector {selector!r}: найдено {len(candidates)}: {candidates}")
    return candidates[0]


def compare_item(area: str, actual: object, desired: object, classification: str = "ONLINE") -> PlanItem:
    if str(actual) == str(desired):
        return PlanItem(area, "NO CHANGE", classification, str(desired))
    return PlanItem(area, "UPDATE", classification, f"{actual!r} -> {desired!r}")


def plan_pve(desired: Desired, actual: Actual) -> list[PlanItem]:
    e = desired.effective
    if not actual.exists:
        return [PlanItem("PVE", "CREATE", "ONLINE", f"{e['type']} {desired.vmid} -> {desired.management_ip}")]
    cfg = actual.config or {}
    items: list[PlanItem] = []
    if e["type"] == "vm":
        items.extend(
            [
                compare_item("PVE name", cfg.get("name"), e["name"]),
                compare_item("PVE cores", cfg.get("cores"), e["resources"]["cpu"]["cores"], "REQUIRES-STOP"),
                compare_item("PVE memory", cfg.get("memory"), e["resources"]["memory_mb"], "REQUIRES-STOP"),
                compare_item("PVE guest agent", option_enabled(cfg.get("agent")), bool(e["vm"]["guest_agent"]), "REQUIRES-STOP"),
                compare_item("PVE ciuser", cfg.get("ciuser"), "root", "REQUIRES-STOP"),
                compare_item("PVE ipconfig0", cfg.get("ipconfig0"), expected_ipconfig(desired), "REQUIRES-STOP"),
            ]
        )
        current_net = str(cfg.get("net0") or "")
        items.append(compare_item("PVE net0", current_net, expected_vm_net(desired, current_net), "REQUIRES-STOP"))
        if "ballooning_mb" in e["resources"]:
            items.append(compare_item("PVE balloon", cfg.get("balloon", 0), e["resources"]["ballooning_mb"], "REQUIRES-STOP"))
        disk = cfg.get("scsi0")
    else:
        items.extend(
            [
                compare_item("PVE hostname", cfg.get("hostname"), e["name"]),
                compare_item("PVE cores", cfg.get("cores"), e["resources"]["cpu"]["cores"], "REQUIRES-STOP"),
                compare_item("PVE memory", cfg.get("memory"), e["resources"]["memory_mb"], "REQUIRES-STOP"),
                compare_item("PVE swap", cfg.get("swap"), e["resources"]["swap_mb"], "REQUIRES-STOP"),
            ]
        )
        unprivileged = bool(e["lxc"]["unprivileged"])
        if boolish(cfg.get("unprivileged")) == unprivileged:
            items.append(PlanItem("PVE unprivileged", "NO CHANGE", "ONLINE", str(unprivileged)))
        else:
            items.append(PlanItem("PVE unprivileged", "BLOCKED", "FORBIDDEN", "recreate required"))
        current_features = str(cfg.get("features") or "")
        target_features = expected_lxc_features(desired, current_features)
        items.append(
            PlanItem(
                "PVE features",
                "NO CHANGE" if set(filter(None, current_features.split(","))) == set(filter(None, target_features.split(","))) else "UPDATE",
                "REQUIRES-STOP",
                target_features,
            )
        )
        current_net = str(cfg.get("net0") or "")
        items.append(compare_item("PVE net0", current_net, expected_lxc_net(desired, current_net), "REQUIRES-STOP"))
        disk = cfg.get("rootfs")
    desired_storage = str(e["resources"]["disk"]["storage"])
    current_storage = storage_from_volume(disk)
    current_size = parse_size_bytes(disk)
    desired_size = int(e["resources"]["disk"]["size_gb"]) * 1024**3
    if current_storage and current_storage != desired_storage:
        items.append(PlanItem("PVE disk storage", "BLOCKED", "FORBIDDEN", f"{current_storage} -> {desired_storage}"))
    elif current_size is None:
        items.append(PlanItem("PVE disk", "BLOCKED", "FORBIDDEN", f"unknown size: {disk!r}"))
    elif current_size > desired_size:
        items.append(PlanItem("PVE disk", "BLOCKED", "FORBIDDEN", "disk shrink запрещён"))
    elif current_size < desired_size:
        items.append(PlanItem("PVE disk", "UPDATE", "ONLINE", f"grow to {e['resources']['disk']['size_gb']}G"))
    else:
        items.append(PlanItem("PVE disk", "NO CHANGE", "ONLINE", f"{e['resources']['disk']['size_gb']}G"))
    items.append(compare_item("PVE onboot", boolish(cfg.get("onboot")), bool(e["boot"]["onboot"])))
    items.append(compare_item("PVE protection", boolish(cfg.get("protection")), bool(e.get("protection", False))))
    items.append(compare_item("PVE description", str(cfg.get("description") or ""), service_description(desired)))
    current_tags = parse_tags(cfg.get("tags"))
    final_tags = desired_final_tags(cfg)
    items.append(
        PlanItem(
            "PVE tags",
            "NO CHANGE" if current_tags == final_tags else "UPDATE",
            "ONLINE",
            format_tags(final_tags),
        )
    )
    desired_pool = e.get("placement", {}).get("pool")
    items.append(compare_item("PVE pool", actual.pool, desired_pool))
    return items


def host_key_present(address: str, port: int = 22) -> bool:
    for target in (address, f"[{address}]:{port}"):
        proc = run_command(["ssh-keygen", "-F", target, "-f", str(KNOWN_HOSTS)], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def ssh_base_args(address: str, known_hosts: Path, port: int = 22) -> list[str]:
    return [
        "ssh", "-i", str(DEPLOYER_KEY),
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=7", "-p", str(port), f"root@{address}",
    ]


def ssh_run(
    address: str,
    command: str,
    *,
    known_hosts: Path = KNOWN_HOSTS,
    input_text: str | None = None,
    timeout: int = 60,
    check: bool = True,
) -> subprocess.CompletedProcess:
    return run_command(
        ssh_base_args(address, known_hosts) + ["--", "bash", "-c", command],
        input_text=input_text,
        timeout=timeout,
        check=check,
    )


def wait_for_ssh_port(address: str, port: int = 22, timeout: int = SSH_READY_TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((address, port), timeout=3):
                return
        except OSError:
            time.sleep(2)
    raise DeployError(f"SSH {address}:{port} не стал доступен за {timeout}s")


def stage_new_host_keys(address: str, port: int = 22) -> Path:
    proc = run_command(["ssh-keyscan", "-T", "7", "-p", str(port), address], timeout=20, check=False)
    lines = [line for line in proc.stdout.splitlines() if line and not line.startswith("#")]
    if not lines:
        raise DeployError(f"не удалось получить SSH host key новой машины {address}:{port}")
    fd, name = tempfile.mkstemp(prefix=".deploy-guest-hostkey-", dir=str(KNOWN_HOSTS.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    path = Path(name)
    os.chmod(path, 0o600)
    return path


def verify_root_ssh(address: str, known_hosts: Path = KNOWN_HOSTS) -> None:
    proc = ssh_run(
        address,
        "set -eu; test \"$(id -u)\" = 0; grep -q '^ID=debian$' /etc/os-release; printf OK",
        known_hosts=known_hosts,
        timeout=30,
    )
    if proc.stdout.strip() != "OK":
        raise DeployError("root SSH verification вернула неожиданный ответ")


def persist_new_host_keys(staged: Path) -> None:
    current = KNOWN_HOSTS.read_text(encoding="utf-8") if KNOWN_HOSTS.exists() else ""
    existing = set(current.splitlines())
    combined = current + ("\n" if current and not current.endswith("\n") else "")
    for line in staged.read_text(encoding="utf-8").splitlines():
        if line and line not in existing:
            combined += line + "\n"
    fd, name = tempfile.mkstemp(prefix=".known_hosts.", dir=str(KNOWN_HOSTS.parent))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(combined)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, KNOWN_HOSTS)
    finally:
        tmp.unlink(missing_ok=True)


def management_identity_state(address: str) -> tuple[str, bytes | None, str | None]:
    script = r'''
set -eu
priv=/etc/proxmox-guest/ssh/management_ed25519
pub=${priv}.pub
if [ ! -e "$priv" ] && [ ! -e "$pub" ]; then printf 'ABSENT\n'; exit 0; fi
if [ ! -f "$priv" ] || [ ! -f "$pub" ] || [ -L "$priv" ] || [ -L "$pub" ]; then printf 'BROKEN\n'; exit 0; fi
[ "$(stat -c '%U:%G:%a' "$priv")" = "root:root:600" ] || { printf 'BROKEN\n'; exit 0; }
[ "$(stat -c '%U:%G:%a' "$pub")" = "root:root:644" ] || { printf 'BROKEN\n'; exit 0; }
derived="$(ssh-keygen -y -f "$priv")" || { printf 'BROKEN\n'; exit 0; }
set -- $derived; d1=$1; d2=$2
set -- $(cat "$pub"); p1=$1; p2=$2
[ "$d1" = "$p1" ] && [ "$d2" = "$p2" ] || { printf 'BROKEN\n'; exit 0; }
printf 'VALID\n'; cat "$pub"
'''
    proc = ssh_run(address, script, timeout=30)
    lines = proc.stdout.splitlines()
    state = lines[0].strip() if lines else "BROKEN"
    if state == "ABSENT":
        return state, None, None
    if state != "VALID" or len(lines) < 2:
        return "BROKEN", None, None
    line = lines[1].strip().encode()
    if not line.startswith(b"ssh-ed25519 "):
        return "BROKEN", None, None
    return "VALID", line + b"\n", fingerprint_key_bytes(line)


def registry_key_state(vmid: int) -> tuple[str, bytes | None, str | None]:
    path = REGISTRY_DIR / f"{vmid}.pub"
    if not path.exists():
        return "ABSENT", None, None
    if path.is_symlink() or not path.is_file():
        return "BROKEN", None, None
    try:
        line = public_key_line(path)
        return "VALID", line + b"\n", fingerprint_key_bytes(line)
    except DeployError:
        return "BROKEN", None, None


def project_master_fingerprint() -> str:
    return fingerprint_key_bytes(public_key_line(PROJECT_REPO_PUB))


def project_git_config_block() -> str:
    return (
        f"{PROJECT_GIT_BEGIN}\n"
        f'[url "{PROJECT_REWRITTEN_URL}"]\n'
        f"    insteadOf = {PROJECT_URL}\n"
        f"{PROJECT_GIT_END}\n"
    )


def project_repo_state(address: str, expected_fp: str) -> str:
    expected_block_b64 = base64.b64encode(project_git_config_block().encode()).decode()
    script = f'''
set -eu
cred={shlex.quote(PROJECT_CREDENTIAL)}
kh={shlex.quote(PROJECT_KNOWN_HOSTS)}
cfg={shlex.quote(PROJECT_SSH_CONFIG)}
gitcfg={shlex.quote(PROJECT_GIT_CONFIG)}
present=0
for f in "$cred" "$kh" "$cfg"; do [ -e "$f" ] && present=$((present+1)); done
begin_count=0; end_count=0
if [ -e "$gitcfg" ]; then
    [ -f "$gitcfg" ] && [ ! -L "$gitcfg" ] || {{ printf 'BROKEN\\n'; exit 0; }}
    begin_count="$(grep -Fxc {shlex.quote(PROJECT_GIT_BEGIN)} "$gitcfg" || true)"
    end_count="$(grep -Fxc {shlex.quote(PROJECT_GIT_END)} "$gitcfg" || true)"
fi
if [ "$present" -eq 0 ] && [ "$begin_count" -eq 0 ] && [ "$end_count" -eq 0 ]; then printf 'ABSENT\\n'; exit 0; fi
[ "$present" -eq 3 ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$begin_count" -eq 1 ] && [ "$end_count" -eq 1 ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$cred" ] && [ ! -L "$cred" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$kh" ] && [ ! -L "$kh" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ -f "$cfg" ] && [ ! -L "$cfg" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$cred")" = "root:root:600" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$kh")" = "root:root:644" ] || {{ printf 'BROKEN\\n'; exit 0; }}
[ "$(stat -c '%U:%G:%a' "$cfg")" = "root:root:644" ] || {{ printf 'BROKEN\\n'; exit 0; }}
grep -Fxq {shlex.quote(PROJECT_CONFIG_MARKER)} "$cfg" || {{ printf 'BROKEN\\n'; exit 0; }}
begin_line="$(grep -nFx {shlex.quote(PROJECT_GIT_BEGIN)} "$gitcfg" | cut -d: -f1)"
end_line="$(grep -nFx {shlex.quote(PROJECT_GIT_END)} "$gitcfg" | cut -d: -f1)"
[ "$begin_line" -lt "$end_line" ] || {{ printf 'BROKEN\\n'; exit 0; }}
actual_block="$(sed -n "${{begin_line}},${{end_line}}p" "$gitcfg")"
expected_block="$(printf '%s' {shlex.quote(expected_block_b64)} | base64 -d)"
[ "$actual_block" = "$expected_block" ] || {{ printf 'BROKEN\\n'; exit 0; }}
pub="$(ssh-keygen -y -f "$cred" 2>/dev/null)" || {{ printf 'BROKEN\\n'; exit 0; }}
fp="$(printf '%s\\n' "$pub" | ssh-keygen -lf - -E sha256 2>/dev/null | awk '{{print $2}}')"
[ "$fp" = {shlex.quote(expected_fp)} ] || {{ printf 'FOREIGN\\n'; exit 0; }}
auth="$(ssh -F "$cfg" -T {shlex.quote(PROJECT_ALIAS)} 2>&1 || true)"
printf '%s\\n' "$auth" | grep -qi 'successfully authenticated' || {{ printf 'BROKEN\\n'; exit 0; }}
if command -v git >/dev/null 2>&1; then
    git ls-remote --heads {shlex.quote(PROJECT_URL)} >/dev/null 2>&1 || {{ printf 'BROKEN\\n'; exit 0; }}
fi
printf 'VALID\\n'
'''
    proc = ssh_run(address, script, timeout=45)
    return proc.stdout.strip().splitlines()[0] if proc.stdout.strip() else "BROKEN"


def bootstrap_check(address: str, capability: str) -> bool:
    checks = {
        "base": "python3 -c 'import apt' >/dev/null 2>&1 && test -s /etc/ssl/certs/ca-certificates.crt && command -v rsync >/dev/null",
        "git": "git --version >/dev/null 2>&1",
        "docker": "docker version >/dev/null 2>&1 && docker info >/dev/null 2>&1 && docker compose version >/dev/null 2>&1",
        "ansible_controller": "ansible --version >/dev/null 2>&1 && docker info >/dev/null 2>&1",
    }
    return ssh_run(address, checks[capability], timeout=30, check=False).returncode == 0


def plan_remote(desired: Desired, actual: Actual) -> list[PlanItem]:
    e = desired.effective
    items: list[PlanItem] = []
    if not actual.exists:
        if host_key_present(desired.management_ip):
            return [PlanItem("SSH trust", "BLOCKED", "FORBIDDEN", "new guest address already exists in persistent known_hosts")]
        if e["management"]["ssh_identity"]:
            reg_state, _, _ = registry_key_state(desired.vmid)
            if reg_state != "ABSENT":
                return [PlanItem("Management SSH identity", "BLOCKED", "FORBIDDEN", f"new VMID has stale registry state={reg_state}")]
        after = "APPLY AFTER START"
        items.append(PlanItem("Management SSH identity", after if e["management"]["ssh_identity"] else "NOT REQUESTED", "ONLINE", "guest identity"))
        items.append(PlanItem("Project repo read", after if e["management"]["project_repo_read"] else "NOT REQUESTED", "ONLINE", EXPECTED_REPO if e["management"]["project_repo_read"] else "false"))
        for capability in desired.bootstrap_capabilities:
            items.append(PlanItem(f"Bootstrap {capability}", after, "ONLINE", capability))
        if not desired.bootstrap_capabilities:
            items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
        return items
    if not host_key_present(desired.management_ip):
        return [PlanItem("SSH trust", "BLOCKED", "FORBIDDEN", "existing guest absent from persistent known_hosts")]
    if actual.status != "running":
        after = "APPLY AFTER START"
        items.append(PlanItem("Management SSH identity", after if e["management"]["ssh_identity"] else "NOT REQUESTED", "ONLINE", "guest identity"))
        items.append(PlanItem("Project repo read", after if e["management"]["project_repo_read"] else "NOT REQUESTED", "ONLINE", EXPECTED_REPO if e["management"]["project_repo_read"] else "false"))
        for capability in desired.bootstrap_capabilities:
            items.append(PlanItem(f"Bootstrap {capability}", after, "ONLINE", capability))
        if not desired.bootstrap_capabilities:
            items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
        return items
    try:
        verify_root_ssh(desired.management_ip)
    except DeployError as exc:
        return [PlanItem("SSH root", "BLOCKED", "FORBIDDEN", safe_message(exc))]
    items.append(PlanItem("SSH root", "NO CHANGE", "ONLINE", "strict trust verified"))
    if e["management"]["ssh_identity"]:
        state, _, fp = management_identity_state(desired.management_ip)
        reg_state, _, reg_fp = registry_key_state(desired.vmid)
        if state == "BROKEN" or reg_state == "BROKEN":
            items.append(PlanItem("Management SSH identity", "BLOCKED", "FORBIDDEN", f"guest={state} registry={reg_state}"))
        elif state == "ABSENT" and reg_state != "ABSENT":
            items.append(PlanItem("Management SSH identity", "BLOCKED", "FORBIDDEN", "stale registry key"))
        elif state == "ABSENT" or reg_state == "ABSENT":
            items.append(PlanItem("Management SSH identity", "APPLY", "ONLINE", "create/register" if state == "ABSENT" else f"register {fp}"))
        elif fp != reg_fp:
            items.append(PlanItem("Management SSH identity", "BLOCKED", "FORBIDDEN", "fingerprint conflict"))
        else:
            items.append(PlanItem("Management SSH identity", "NO CHANGE", "ONLINE", fp or "valid"))
    else:
        items.append(PlanItem("Management SSH identity", "NOT REQUESTED", "ONLINE", "false"))
    expected_fp = project_master_fingerprint()
    pstate = project_repo_state(desired.management_ip, expected_fp)
    if e["management"]["project_repo_read"]:
        action = "NO CHANGE" if pstate == "VALID" else ("BLOCKED" if pstate == "FOREIGN" else "APPLY")
        items.append(PlanItem("Project repo read", action, "FORBIDDEN" if action == "BLOCKED" else "ONLINE", f"state={pstate}"))
    else:
        action = "NO CHANGE" if pstate == "ABSENT" else ("REMOVE" if pstate == "VALID" else "BLOCKED")
        items.append(PlanItem("Project repo read", action, "FORBIDDEN" if action == "BLOCKED" else "ONLINE", f"state={pstate}"))
    for capability in desired.bootstrap_capabilities:
        items.append(PlanItem(f"Bootstrap {capability}", "NO CHANGE" if bootstrap_check(desired.management_ip, capability) else "APPLY", "ONLINE", capability))
    if not desired.bootstrap_capabilities:
        items.append(PlanItem("Bootstrap", "NOT REQUESTED", "ONLINE", "none"))
    return items


def build_plan(desired: Desired, actual: Actual) -> Plan:
    items = plan_pve(desired, actual) + plan_remote(desired, actual)
    if actual.exists and actual.status == "running" and any(
        item.changes and item.classification == "REQUIRES-STOP" for item in items
    ):
        items.append(PlanItem("Running guest safety", "BLOCKED", "FORBIDDEN", "automatic stop/reboot запрещён"))
    return Plan(desired, actual, items)


def print_plan(plan: Plan, revision: str, apply: bool) -> None:
    d = plan.desired
    print(f"=== deploy-guest {d.vmid} {'APPLY' if apply else 'PLAN'} ===")
    print(f"revision: {revision}")
    print(f"manifest: {d.manifest}")
    print(f"type: {d.effective['type']}  node: {d.effective['node']}  ip: {d.management_ip}")
    print(f"actual: {'exists/' + str(plan.actual.status) if plan.actual.exists else 'absent'}\n")
    for item in plan.items:
        print(f"{item.area:28} {item.action:17} {item.classification:13} {item.detail}")
    print("\n[PLAN] " + ("BLOCKED" if plan.blocked else ("changes required" if plan.changes else "NO CHANGE")))


def config_path(desired: Desired, node: str | None = None) -> str:
    kind = "qemu" if desired.effective["type"] == "vm" else "lxc"
    return f"/nodes/{urllib.parse.quote(node or str(desired.effective['node']))}/{kind}/{desired.vmid}/config"


def set_pool(api: PveApi, actual_pool: str | None, desired_pool: str | None, vmid: int) -> None:
    if actual_pool == desired_pool:
        return
    if actual_pool:
        api.put(f"/pools/{urllib.parse.quote(actual_pool)}", {"vms": vmid, "delete": 1})
    if desired_pool:
        api.put(f"/pools/{urllib.parse.quote(desired_pool)}", {"vms": vmid})


def vm_update_payload(desired: Desired, cfg: dict[str, Any], incomplete: bool) -> dict[str, Any]:
    e = desired.effective
    payload: dict[str, Any] = {
        "name": e["name"],
        "cores": e["resources"]["cpu"]["cores"],
        "memory": e["resources"]["memory_mb"],
        "agent": preserve_boolean_suboptions(cfg.get("agent"), bool(e["vm"]["guest_agent"])),
        "ciuser": "root",
        "ipconfig0": expected_ipconfig(desired),
        "onboot": 1 if e["boot"]["onboot"] else 0,
        "protection": 1 if e.get("protection", False) else 0,
        "tags": format_tags(desired_incomplete_tags(cfg) if incomplete else desired_final_tags(cfg)),
        "description": service_description(desired),
        "net0": expected_vm_net(desired, str(cfg.get("net0") or "")),
    }
    if "ballooning_mb" in e["resources"]:
        payload["balloon"] = e["resources"]["ballooning_mb"]
    return payload


def lxc_update_payload(desired: Desired, cfg: dict[str, Any], incomplete: bool) -> dict[str, Any]:
    e = desired.effective
    return {
        "hostname": e["name"],
        "cores": e["resources"]["cpu"]["cores"],
        "memory": e["resources"]["memory_mb"],
        "swap": e["resources"]["swap_mb"],
        "net0": expected_lxc_net(desired, str(cfg.get("net0") or "")),
        "features": f"keyctl={int(bool(e['lxc']['features']['keyctl']))},nesting={int(bool(e['lxc']['features']['nesting']))}",
        "onboot": 1 if e["boot"]["onboot"] else 0,
        "protection": 1 if e.get("protection", False) else 0,
        "tags": format_tags(desired_incomplete_tags(cfg) if incomplete else desired_final_tags(cfg)),
        "description": service_description(desired),
    }


def grow_disk_if_needed(api: PveApi, desired: Desired, cfg: dict[str, Any]) -> None:
    e = desired.effective
    disk_name = "scsi0" if e["type"] == "vm" else "rootfs"
    current = parse_size_bytes(cfg.get(disk_name))
    wanted_gb = int(e["resources"]["disk"]["size_gb"])
    wanted = wanted_gb * 1024**3
    if current is None:
        raise DeployError(f"не удалось определить размер {disk_name}")
    if current > wanted:
        raise BlockedError("disk shrink запрещён")
    if current < wanted:
        delta_gb = wanted_gb - current // 1024**3
        kind = "qemu" if e["type"] == "vm" else "lxc"
        result = api.put(
            f"/nodes/{urllib.parse.quote(str(e['node']))}/{kind}/{desired.vmid}/resize",
            {"disk": disk_name, "size": f"+{delta_gb}G"},
        )
        api.wait_task(result)


def create_vm(api: PveApi, desired: Desired, aggregate: bytes) -> None:
    e = desired.effective
    clone_data: dict[str, Any] = {
        "newid": desired.vmid,
        "name": e["name"],
        "full": 1,
        "storage": e["resources"]["disk"]["storage"],
    }
    if e.get("placement", {}).get("pool"):
        clone_data["pool"] = e["placement"]["pool"]
    upid = api.post(
        f"/nodes/{urllib.parse.quote(str(e['node']))}/qemu/{int(e['vm']['source']['template_vmid'])}/clone",
        clone_data,
    )
    api.wait_task(upid)
    cfg = api.get(config_path(desired))
    payload = vm_update_payload(desired, cfg, True)
    payload["sshkeys"] = aggregate.decode()
    api.put(config_path(desired), payload)
    grow_disk_if_needed(api, desired, api.get(config_path(desired)))


def create_lxc(api: PveApi, desired: Desired, aggregate: bytes, ostemplate: str) -> None:
    e = desired.effective
    data: dict[str, Any] = {
        "vmid": desired.vmid,
        "hostname": e["name"],
        "ostemplate": ostemplate,
        "rootfs": f"{e['resources']['disk']['storage']}:{e['resources']['disk']['size_gb']}",
        "cores": e["resources"]["cpu"]["cores"],
        "memory": e["resources"]["memory_mb"],
        "swap": e["resources"]["swap_mb"],
        "net0": expected_lxc_net(desired),
        "unprivileged": 1 if e["lxc"]["unprivileged"] else 0,
        "features": f"keyctl={int(bool(e['lxc']['features']['keyctl']))},nesting={int(bool(e['lxc']['features']['nesting']))}",
        "onboot": 1 if e["boot"]["onboot"] else 0,
        "protection": 1 if e.get("protection", False) else 0,
        "tags": format_tags(desired_incomplete_tags()),
        "description": service_description(desired),
        "ssh-public-keys": aggregate.decode(),
    }
    if e.get("placement", {}).get("pool"):
        data["pool"] = e["placement"]["pool"]
    api.wait_task(api.post(f"/nodes/{urllib.parse.quote(str(e['node']))}/lxc", data))


def apply_existing_pve(api: PveApi, desired: Desired, actual: Actual) -> None:
    cfg = actual.config or {}
    payload = vm_update_payload(desired, cfg, True) if desired.effective["type"] == "vm" else lxc_update_payload(desired, cfg, True)
    api.put(config_path(desired, actual.node), payload)
    grow_disk_if_needed(api, desired, api.get(config_path(desired, actual.node)))
    set_pool(api, actual.pool, desired.effective.get("placement", {}).get("pool"), desired.vmid)


def start_guest_if_needed(api: PveApi, desired: Desired) -> None:
    if not desired.effective["boot"]["start_after_deploy"]:
        return
    resources = api.get("/cluster/resources?type=vm")
    resource = next((item for item in resources if int(item.get("vmid", -1)) == desired.vmid), None)
    if resource is None:
        raise DeployError(f"VMID {desired.vmid} исчез после APPLY")
    if str(resource.get("status")) == "running":
        return
    kind = "qemu" if desired.effective["type"] == "vm" else "lxc"
    node = str(resource.get("node") or desired.effective["node"])
    api.wait_task(api.post(f"/nodes/{urllib.parse.quote(node)}/{kind}/{desired.vmid}/status/start"))


def establish_ssh_trust(desired: Desired, created_now: bool) -> None:
    wait_for_ssh_port(desired.management_ip)
    if created_now:
        if host_key_present(desired.management_ip):
            raise BlockedError("новый guest уже имеет запись в persistent known_hosts; reuse trust запрещён")
        staged = stage_new_host_keys(desired.management_ip)
        try:
            verify_root_ssh(desired.management_ip, staged)
            persist_new_host_keys(staged)
        finally:
            staged.unlink(missing_ok=True)
        verify_root_ssh(desired.management_ip)
    else:
        if not host_key_present(desired.management_ip):
            raise BlockedError("существующий guest отсутствует в persistent known_hosts")
        verify_root_ssh(desired.management_ip)


def create_management_identity(address: str) -> None:
    script = r'''
set -eu
dir=/etc/proxmox-guest/ssh
priv=/etc/proxmox-guest/ssh/management_ed25519
pub=${priv}.pub
if [ -e "$priv" ] || [ -e "$pub" ]; then echo concurrent-identity >&2; exit 70; fi
install -d -o root -g root -m 0700 "$dir"
umask 077
ssh-keygen -q -t ed25519 -N '' -C management -f "$priv"
chown root:root "$priv" "$pub"; chmod 0600 "$priv"; chmod 0644 "$pub"
'''
    ssh_run(address, script, timeout=30)


def atomic_registry_write(vmid: int, public_line: bytes) -> None:
    path = REGISTRY_DIR / f"{vmid}.pub"
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise BlockedError(f"registry path небезопасен: {path}")
    fd, name = tempfile.mkstemp(prefix=f".{vmid}.pub.", dir=str(REGISTRY_DIR))
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(public_line if public_line.endswith(b"\n") else public_line + b"\n")
            fh.flush(); os.fsync(fh.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def sync_management_keys(revision: str) -> int:
    env = os.environ.copy()
    env["SYNC_MANAGEMENT_KEYS_SOURCE_REVISION"] = revision
    proc = run_command([sys.executable, str(SYNC_SOURCE)], timeout=300, check=False, env=env)
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    if proc.returncode not in {0, 2}:
        raise DeployError(f"sync-management-keys завершился с кодом {proc.returncode}")
    return proc.returncode


def ensure_management_identity(desired: Desired, revision: str) -> None:
    if not desired.effective["management"]["ssh_identity"]:
        return
    state, line, fp = management_identity_state(desired.management_ip)
    reg_state, _, reg_fp = registry_key_state(desired.vmid)
    if state == "BROKEN" or reg_state == "BROKEN":
        raise BlockedError(f"management identity ambiguous: guest={state} registry={reg_state}")
    if state == "ABSENT":
        if reg_state != "ABSENT":
            raise BlockedError("stale registry key exists")
        create_management_identity(desired.management_ip)
        state, line, fp = management_identity_state(desired.management_ip)
    if state != "VALID" or line is None or fp is None:
        raise DeployError("management identity verify не пройден")
    if reg_state == "VALID" and reg_fp != fp:
        raise BlockedError("management registry fingerprint conflict")
    if reg_state == "ABSENT":
        atomic_registry_write(desired.vmid, line)
        sync_management_keys(revision)
    reg_state, _, reg_fp = registry_key_state(desired.vmid)
    if reg_state != "VALID" or reg_fp != fp:
        raise DeployError("management registry final verify не пройден")


def read_project_private_from_fd() -> bytes:
    raw_fd = os.environ.get("DEPLOY_GUEST_PROJECT_REPO_KEY_FD", "")
    if not raw_fd.isdigit():
        raise DeployError("wrapper не передал dedicated Project Git READ FD")
    fd = int(raw_fd)
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise DeployError(f"Project Git READ FD недоступен: {exc}") from exc
    if not stat.S_ISREG(st.st_mode):
        raise DeployError("Project Git READ FD не является обычным файлом")
    data = b""
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            raise DeployError("Project Git READ key неожиданно велик")
    if b"PRIVATE KEY" not in data:
        raise DeployError("Project Git READ FD не содержит private key")
    return data


def private_key_fingerprint(data: bytes) -> str:
    fd, name = tempfile.mkstemp(prefix=".project-key-")
    path = Path(name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(path, 0o600)
        derived = run_command(["ssh-keygen", "-y", "-f", str(path)]).stdout.strip().encode()
        return fingerprint_key_bytes(derived)
    finally:
        path.unlink(missing_ok=True)


def project_ssh_config_text() -> str:
    return (
        f"{PROJECT_CONFIG_MARKER}\n"
        f"Host {PROJECT_ALIAS}\n"
        "    HostName github.com\n"
        "    User git\n"
        f"    IdentityFile {PROJECT_CREDENTIAL}\n"
        "    IdentitiesOnly yes\n"
        f"    UserKnownHostsFile {PROJECT_KNOWN_HOSTS}\n"
        "    StrictHostKeyChecking yes\n"
        "    BatchMode yes\n"
    )


def apply_project_repo_read(desired: Desired, private_data: bytes) -> None:
    expected_fp = project_master_fingerprint()
    if private_key_fingerprint(private_data) != expected_fp:
        raise DeployError("Project Git READ private key не соответствует canonical public key")
    known_hosts = GITHUB_KNOWN_HOSTS.read_text(encoding="utf-8")
    payload = {
        "credential": base64.b64encode(private_data).decode(),
        "known_hosts": known_hosts,
        "ssh_config": project_ssh_config_text(),
        "git_block": project_git_config_block(),
    }
    remote = r'''
set -eu
payload="$(mktemp)"; trap 'rm -f "$payload"' EXIT; cat >"$payload"
python3 - "$payload" <<'PY'
import base64, json, os, pathlib, sys, tempfile
data=json.load(open(sys.argv[1], encoding="utf-8"))
items=[
(pathlib.Path("/etc/proxmox-guest/credentials/github-proxmox-read"),base64.b64decode(data["credential"]),0o600,0o700),
(pathlib.Path("/etc/proxmox-guest/ssh/github-proxmox-known_hosts"),data["known_hosts"].encode(),0o644,0o700),
(pathlib.Path("/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf"),data["ssh_config"].encode(),0o644,0o755)]
for path,content,mode,parent_mode in items:
    if path.is_symlink(): raise SystemExit(f"refuse symlink {path}")
    path.parent.mkdir(parents=True,exist_ok=True); os.chown(path.parent,0,0); os.chmod(path.parent,parent_mode)
    fd,tmp_name=tempfile.mkstemp(prefix=f".{path.name}.",dir=path.parent); tmp=pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd,"wb") as fh: fh.write(content); fh.flush(); os.fsync(fh.fileno())
        os.chown(tmp,0,0); os.chmod(tmp,mode); os.replace(tmp,path)
    finally:
        if tmp.exists(): tmp.unlink()

gitcfg=pathlib.Path("/etc/gitconfig")
if gitcfg.is_symlink() or (gitcfg.exists() and not gitcfg.is_file()): raise SystemExit("unsafe /etc/gitconfig")
current=gitcfg.read_text(encoding="utf-8") if gitcfg.exists() else ""
lines=current.splitlines()
begin="# BEGIN PROXMOX-PROJECT-REPO-READ"; end="# END PROXMOX-PROJECT-REPO-READ"
bi=[i for i,v in enumerate(lines) if v==begin]; ei=[i for i,v in enumerate(lines) if v==end]
block=data["git_block"].rstrip("\n").splitlines()
if not bi and not ei:
    out=lines + ([""] if lines and lines[-1] else []) + block
elif len(bi)==1 and len(ei)==1 and bi[0] < ei[0]:
    out=lines[:bi[0]] + block + lines[ei[0]+1:]
else:
    raise SystemExit("ambiguous project repo block in /etc/gitconfig")
fd,tmp_name=tempfile.mkstemp(prefix=".gitconfig.",dir=gitcfg.parent); tmp=pathlib.Path(tmp_name)
try:
    with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write("\n".join(out).rstrip("\n")+"\n"); fh.flush(); os.fsync(fh.fileno())
    os.chown(tmp,0,0); os.chmod(tmp,0o644); os.replace(tmp,gitcfg)
finally:
    if tmp.exists(): tmp.unlink()
PY
auth="$(ssh -F /etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf -T github-proxmox-read 2>&1 || true)"
printf '%s\n' "$auth" | grep -qi 'successfully authenticated'
if command -v git >/dev/null 2>&1; then git ls-remote --heads 'git@github.com:zsergeyru/proxmox.git' >/dev/null; fi
'''
    ssh_run(desired.management_ip, remote, input_text=json.dumps(payload), timeout=90)
    if project_repo_state(desired.management_ip, expected_fp) != "VALID":
        raise DeployError("Project Git READ final verify не пройден")


def remove_project_repo_read(desired: Desired) -> None:
    expected_fp = project_master_fingerprint()
    state = project_repo_state(desired.management_ip, expected_fp)
    if state == "ABSENT":
        return
    if state != "VALID":
        raise BlockedError(f"Project Git READ REMOVE запрещён для state={state}")
    remote = r'''
set -eu
python3 - <<'PY'
import os, pathlib, tempfile
for raw in (
    "/etc/proxmox-guest/credentials/github-proxmox-read",
    "/etc/proxmox-guest/ssh/github-proxmox-known_hosts",
    "/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf",
):
    path=pathlib.Path(raw)
    if path.is_symlink() or (path.exists() and not path.is_file()): raise SystemExit(f"unsafe managed path {path}")
    if path.exists(): path.unlink()
gitcfg=pathlib.Path("/etc/gitconfig")
if gitcfg.is_symlink() or (gitcfg.exists() and not gitcfg.is_file()): raise SystemExit("unsafe /etc/gitconfig")
if gitcfg.exists():
    lines=gitcfg.read_text(encoding="utf-8").splitlines()
    begin="# BEGIN PROXMOX-PROJECT-REPO-READ"; end="# END PROXMOX-PROJECT-REPO-READ"
    bi=[i for i,v in enumerate(lines) if v==begin]; ei=[i for i,v in enumerate(lines) if v==end]
    if len(bi)!=1 or len(ei)!=1 or bi[0]>=ei[0]: raise SystemExit("ambiguous project repo block in /etc/gitconfig")
    out=lines[:bi[0]]+lines[ei[0]+1:]
    fd,tmp_name=tempfile.mkstemp(prefix=".gitconfig.",dir=gitcfg.parent); tmp=pathlib.Path(tmp_name)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as fh: fh.write(("\n".join(out).rstrip("\n")+"\n") if out else ""); fh.flush(); os.fsync(fh.fileno())
        os.chown(tmp,0,0); os.chmod(tmp,0o644); os.replace(tmp,gitcfg)
    finally:
        if tmp.exists(): tmp.unlink()
PY
'''
    ssh_run(desired.management_ip, remote, timeout=30)
    if project_repo_state(desired.management_ip, expected_fp) != "ABSENT":
        raise DeployError("Project Git READ REMOVE final verify не пройден")


def apt_install(address: str, packages: list[str]) -> None:
    args = " ".join(shlex.quote(item) for item in packages)
    script = f"""
set -eu
if dpkg --audit | grep -q .; then echo 'dpkg --audit reports unfinished state' >&2; exit 81; fi
if find /var/lib/dpkg/updates -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then echo 'dpkg updates directory is not empty' >&2; exit 82; fi
missing=''
for pkg in {args}; do
    if ! dpkg-query -W -f='${{db:Status-Abbrev}}' "$pkg" 2>/dev/null | grep -q '^ii'; then
        missing="$missing $pkg"
    fi
done
[ -n "$missing" ] || exit 0
export DEBIAN_FRONTEND=noninteractive
apt-get update
# shellcheck disable=SC2086
apt-get install -y --no-install-recommends $missing
"""
    ssh_run(address, script, timeout=300)


def apply_bootstrap_capability(address: str, capability: str) -> None:
    if bootstrap_check(address, capability):
        return
    if capability == "base":
        apt_install(address, ["python3", "python3-apt", "ca-certificates", "rsync"])
    elif capability == "git":
        apt_install(address, ["git"])
    elif capability == "docker":
        apt_install(address, ["docker.io", "docker-compose"])
        ssh_run(address, "set -eu; systemctl enable docker >/dev/null; systemctl start docker", timeout=90)
    elif capability == "ansible_controller":
        apt_install(address, ["ansible-core"])
    else:
        raise DeployError(f"неизвестная Bootstrap capability: {capability}")
    if not bootstrap_check(address, capability):
        raise DeployError(f"Bootstrap capability {capability} не прошла final verify")


def clear_incomplete_tag(api: PveApi, desired: Desired) -> None:
    cfg = api.get(config_path(desired))
    tags = parse_tags(cfg.get("tags")); tags.discard(INCOMPLETE_TAG); tags.update({OWNERSHIP_TAG, MANAGEMENT_TAG})
    api.put(config_path(desired), {"tags": format_tags(tags)})


def apply_plan(api: PveApi, desired: Desired, actual: Actual, registry_module: Any, registry: Any, lxc_template: str | None, revision: str) -> None:
    if actual.exists and actual.status == "running" and any(
        item.changes and item.classification == "REQUIRES-STOP" for item in plan_pve(desired, actual)
    ):
        raise BlockedError("running guest требует REQUIRES-STOP изменений")
    registry_module.ensure_aggregate(registry, False)
    created_now = False
    if not actual.exists:
        if desired.effective["management"]["ssh_identity"] and (REGISTRY_DIR / f"{desired.vmid}.pub").exists():
            raise BlockedError("новый VMID имеет stale registry key")
        if desired.effective["type"] == "vm":
            create_vm(api, desired, registry.aggregate)
        else:
            if not lxc_template:
                raise DeployError("resolved LXC template отсутствует")
            create_lxc(api, desired, registry.aggregate, lxc_template)
        created_now = True
    else:
        cfg = actual.config or {}
        if INCOMPLETE_TAG not in parse_tags(cfg.get("tags")):
            api.put(config_path(desired, actual.node), {"tags": format_tags(desired_incomplete_tags(cfg))})
        apply_existing_pve(api, desired, discover_actual(api, desired))
    start_guest_if_needed(api, desired)
    if not desired.effective["boot"]["start_after_deploy"]:
        raise BlockedError("v1 final acceptance требует running guest")
    establish_ssh_trust(desired, created_now)
    ensure_management_identity(desired, revision)
    if desired.effective["management"]["project_repo_read"]:
        if project_repo_state(desired.management_ip, project_master_fingerprint()) != "VALID":
            private_data = read_project_private_from_fd()
            try:
                apply_project_repo_read(desired, private_data)
            finally:
                private_data = b""
    else:
        remove_project_repo_read(desired)
    for capability in desired.bootstrap_capabilities:
        apply_bootstrap_capability(desired.management_ip, capability)
    verify_root_ssh(desired.management_ip)
    if desired.effective["management"]["ssh_identity"]:
        state, _, fp = management_identity_state(desired.management_ip)
        rstate, _, rfp = registry_key_state(desired.vmid)
        if state != "VALID" or rstate != "VALID" or fp != rfp:
            raise DeployError("final management identity verify не пройден")
    wanted_project = "VALID" if desired.effective["management"]["project_repo_read"] else "ABSENT"
    if project_repo_state(desired.management_ip, project_master_fingerprint()) != wanted_project:
        raise DeployError("final Project Git READ verify не пройден")
    for capability in desired.bootstrap_capabilities:
        if not bootstrap_check(desired.management_ip, capability):
            raise DeployError(f"final Bootstrap verify failed: {capability}")
    final = discover_actual(api, desired)
    if not final.exists:
        raise DeployError("final PVE verify: object disappeared")
    clear_incomplete_tag(api, desired)
    final = discover_actual(api, desired)
    if INCOMPLETE_TAG in parse_tags((final.config or {}).get("tags")):
        raise DeployError("deploy-incomplete не снят")
    print(f"[SUCCESS] VMID {desired.vmid} приведён к desired state")


def preflight(vmid: int) -> tuple[str, Desired, PveApi, Actual, Any, Any, str | None]:
    revision = git_revision()
    validate_repository()
    desired = load_desired(vmid)
    cfg = load_local_config()
    if str(cfg.get("managed_pool")) != "managed":
        raise DeployError(f"{LOCAL_CONFIG}: неожиданный managed_pool")
    validate_deployer_identity(); validate_known_hosts()
    token_id, token_secret = load_token()
    api = PveApi(str(desired.effective["node"]), token_id, token_secret)
    registry_module, registry = load_registry()
    lxc_template = verify_sources(api, desired)
    actual = discover_actual(api, desired)
    if desired.effective["management"]["project_repo_read"]:
        raw_fd = os.environ.get("DEPLOY_GUEST_PROJECT_REPO_KEY_FD", "")
        if not raw_fd.isdigit():
            raise DeployError("project_repo_read=true, но root-wrapper не передал dedicated FD")
        try:
            st = os.fstat(int(raw_fd))
        except OSError as exc:
            raise DeployError(f"Project Git READ FD недоступен: {exc}") from exc
        if not stat.S_ISREG(st.st_mode):
            raise DeployError("Project Git READ FD не является обычным файлом")
        project_master_fingerprint()
    return revision, desired, api, actual, registry_module, registry, lxc_template


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PLAN/APPLY one deployable Proxmox guest")
    parser.add_argument("vmid", type=int, help="VMID 100-999")
    parser.add_argument("--apply", action="store_true", help="применить PLAN; без флага строго read-only")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not 100 <= args.vmid <= 999:
        print("ОШИБКА: VMID должен быть в диапазоне 100-999", file=sys.stderr)
        return EXIT_FAILED
    revision = os.environ.get("DEPLOY_GUEST_SOURCE_REVISION", "")
    mode = "apply" if args.apply else "plan"
    try:
        revision, desired, api, actual, registry_module, registry, lxc_template = preflight(args.vmid)
        plan = build_plan(desired, actual)
        print_plan(plan, revision, args.apply)
        audit(args.vmid, revision, mode, "plan", "blocked" if plan.blocked else "ok", "PLAN built")
        if plan.blocked:
            return EXIT_BLOCKED
        if not args.apply:
            return EXIT_OK
        if not plan.changes:
            print(f"[SUCCESS] VMID {args.vmid}: NO CHANGE")
            audit(args.vmid, revision, mode, "final", "success", "NO CHANGE")
            return EXIT_OK
        apply_plan(api, desired, actual, registry_module, registry, lxc_template, revision)
        audit(args.vmid, revision, mode, "final", "success", "desired state verified")
        return EXIT_OK
    except BlockedError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        audit(args.vmid, revision, mode, "error", "blocked", safe_message(exc))
        return EXIT_BLOCKED
    except DeployError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        audit(args.vmid, revision, mode, "error", "failed", safe_message(exc))
        return EXIT_FAILED
    except Exception as exc:
        print(f"ОШИБКА: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        audit(args.vmid, revision, mode, "error", "unexpected", f"{type(exc).__name__}: {safe_message(exc)}")
        return EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
