"""Операции PVE-хоста, которые Proxmox не разрешает выполнять API token."""

from __future__ import annotations

import json

import shlex
from pathlib import Path

from .common import InfraManagerError, console, log_level, require_command, run
from .guest_catalog import find_guest_by_role, guest_management_address
from .settings import PATHS, SETTINGS


def _required_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise InfraManagerError(f"Не найден {label}: {path}")


def _ssh(node: str, *command: str, capture: bool = False):
    """Выполнить команду на PVE от root по отдельному ключу infra-manager."""
    require_command("ssh")
    _required_file(PATHS.pve_host_private_key, "закрытый ключ root-доступа к PVE")
    _required_file(PATHS.pve_host_known_hosts, "known_hosts PVE")

    remote_command = shlex.join(command)

    return run(
        [
            "ssh",
            "-i",
            str(PATHS.pve_host_private_key),
            "-o",
            f"UserKnownHostsFile={PATHS.pve_host_known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            f"root@{node}",
            remote_command,
        ],
        capture_output=capture,
    )


def _ssh_with_input(
    node: str,
    *command: str,
    input_text: str,
):
    """Передать данные на PVE через stdin, не помещая их в argv."""
    require_command("ssh")
    _required_file(PATHS.pve_host_private_key, "закрытый ключ root-доступа к PVE")
    _required_file(PATHS.pve_host_known_hosts, "known_hosts PVE")

    remote_command = shlex.join(command)
    return run(
        [
            "ssh",
            "-i",
            str(PATHS.pve_host_private_key),
            "-o",
            f"UserKnownHostsFile={PATHS.pve_host_known_hosts}",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ConnectTimeout=10",
            f"root@{node}",
            remote_command,
        ],
        capture_output=True,
        input_text=input_text,
    )


OPENBAO_HOST_COMMAND = Path("/usr/local/sbin/infra-manager-openbao-unseal")
OPENBAO_HOST_CONFIG = Path("/etc/infra-manager/openbao-host.json")
RECOVERY_HOST_COMMAND = Path("/usr/local/sbin/infra-manager-recovery")
OPENBAO_LEGACY_SERVICE = "infra-manager-openbao-unseal.service"
OPENBAO_LEGACY_TIMER = "infra-manager-openbao-unseal.timer"


def _install_remote_file(
    node: str,
    source: Path,
    target: Path,
    mode: str,
) -> None:
    """Атомарно установить обычный файл на PVE через доверенный SSH."""
    _required_file(source, f"исходный файл {source}")
    script = r"""
set -eu
target="$1"
mode="$2"
install -d -m 0755 "$(dirname "$target")"
tmp="$(mktemp "$target.tmp.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
chown root:root "$tmp"
chmod "$mode" "$tmp"
mv -f "$tmp" "$target"
trap - EXIT
"""
    _ssh_with_input(
        node,
        "sh",
        "-c",
        script,
        "sh",
        str(target),
        mode,
        input_text=source.read_text(encoding="utf-8"),
    )


def _install_remote_text(
    node: str,
    content: str,
    target: Path,
    mode: str,
) -> None:
    """Атомарно установить сформированный текстовый файл на PVE."""

    script = r"""
set -eu
target="$1"
mode="$2"
install -d -m 0755 "$(dirname "$target")"
tmp="$(mktemp "$target.tmp.XXXXXX")"
trap 'rm -f "$tmp"' EXIT
cat > "$tmp"
chown root:root "$tmp"
chmod "$mode" "$tmp"
mv -f "$tmp" "$target"
trap - EXIT
"""
    _ssh_with_input(
        node,
        "sh",
        "-c",
        script,
        "sh",
        str(target),
        mode,
        input_text=content,
    )


def install_openbao_host_support(node: str, repo_root: Path) -> None:
    """Установить на PVE только сценарий разблокировки OpenBao."""
    command_source = (
        repo_root / "scripts" / "infra-manager" / "host" / "openbao-unseal.py"
    )
    identity = find_guest_by_role(repo_root, SETTINGS.infra_manager_role)
    address = guest_management_address(repo_root, identity)
    if address is None:
        raise InfraManagerError(
            "Для OpenBao infra-manager должен иметь статический административный IPv4"
        )
    host_config = json.dumps(
        {
            "vmid": identity.vmid,
            "name": identity.name,
            "address": address,
        },
        ensure_ascii=False,
        sort_keys=True,
    ) + "\n"

    cleanup = f"""
systemctl disable --now {OPENBAO_LEGACY_TIMER} {OPENBAO_LEGACY_SERVICE} \
    >/dev/null 2>&1 || true
rm -f \
    /etc/systemd/system/{OPENBAO_LEGACY_TIMER} \
    /etc/systemd/system/{OPENBAO_LEGACY_SERVICE} \
    /etc/systemd/system/timers.target.wants/{OPENBAO_LEGACY_TIMER} \
    /etc/systemd/system/multi-user.target.wants/{OPENBAO_LEGACY_SERVICE}
systemctl daemon-reload
"""
    _ssh(node, "sh", "-c", cleanup)

    _install_remote_file(
        node,
        command_source,
        OPENBAO_HOST_COMMAND,
        "0755",
    )
    _install_remote_text(
        node,
        host_config,
        OPENBAO_HOST_CONFIG,
        "0644",
    )
    console.detail(
        f"Сценарий OpenBao установлен на PVE для "
        f"{identity.vmid} {identity.name} ({address})"
    )


def install_recovery_host_support(node: str, repo_root: Path) -> None:
    """Установить на PVE независимый helper аварийного восстановления."""
    source = (
        repo_root / "scripts" / "infra-manager" / "host" / "recovery.py"
    )
    _install_remote_file(
        node,
        source,
        RECOVERY_HOST_COMMAND,
        "0755",
    )
    console.detail("Recovery helper установлен на PVE")


def prepare_recovery_git(node: str) -> None:
    """Подготовить PVE-only аварийную копию Git Deploy Key."""
    _ssh(
        node,
        str(RECOVERY_HOST_COMMAND),
        "--prepare",
    )


def check_recovery_contour(node: str) -> None:
    """Проверить готовность полного аварийного контура на PVE."""
    _ssh(
        node,
        str(RECOVERY_HOST_COMMAND),
        "--check",
    )


def cleanup_transition_state(node: str) -> None:
    """Удалить проверенные переходные файловые secret-источники на PVE."""
    _ssh(
        node,
        str(RECOVERY_HOST_COMMAND),
        "--cleanup-transition",
    )


def initialize_openbao_on_host(node: str) -> None:
    """Выполнить первичную инициализацию OpenBao на стороне PVE."""
    _ssh(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--initialize",
        "--log-level",
        log_level(),
    )


def trigger_openbao_unseal(node: str) -> None:
    """Запустить идемпотентную разблокировку OpenBao через PVE."""
    _ssh(node, str(OPENBAO_HOST_COMMAND))


def check_openbao_kv(node: str) -> None:
    """Проверить KV v2 через PVE-only AppRole без выдачи секретов."""
    _ssh(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--check-kv",
        "--log-level",
        "quiet",
    )


def check_openbao_ssh_access(node: str) -> None:
    """Проверить служебный SSH-доступ, ssh-otp и auth/machine через PVE."""
    _ssh(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--check-ssh-access",
        "--log-level",
        "quiet",
    )


def update_openbao_semaphore_api_token(node: str, api_token: str) -> None:
    """Сохранить перевыпущенный Semaphore token через узкую PVE-only роль."""
    clean = api_token.strip()
    if not clean or any(character.isspace() for character in clean):
        raise InfraManagerError("Некорректный API token Semaphore")
    _ssh_with_input(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--update-semaphore-api-token",
        input_text=clean + "\n",
    )


def sign_ssh_client_key(node: str, public_key: str) -> str:
    """Подписать временный открытый SSH-ключ через PVE-only доступ OpenBao."""
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise InfraManagerError("Для подписи ожидается открытый ключ Ed25519")
    result = _ssh_with_input(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--sign-client-key",
        input_text=normalized + "\n",
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise InfraManagerError(
            "PVE/OpenBao не вернул корректный SSH-сертификат клиента"
        )
    return certificate

def sync_openbao_otp_contract(
    node: str,
    sources: list[dict[str, object]],
) -> None:
    """Синхронизировать OpenBao SSH OTP и машинные AppRole."""
    if not isinstance(sources, list):
        raise InfraManagerError("Некорректный OTP-контракт")
    _ssh_with_input(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--sync-otp-contract",
        "--log-level",
        log_level(),
        input_text=json.dumps(
            {"sources": sources},
            separators=(",", ":"),
        )
        + "\n",
    )


def issue_openbao_machine_credentials(
    node: str,
    vmid: int,
) -> dict[str, str]:
    """Выпустить RoleID/SecretID одной машинной AppRole без хранения на 910."""
    if not isinstance(vmid, int) or vmid <= 0:
        raise InfraManagerError("Некорректный VMID машинной идентичности")
    result = _ssh_with_input(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--issue-machine-credentials",
        input_text=json.dumps({"vmid": vmid}, separators=(",", ":")) + "\n",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise InfraManagerError(
            "PVE/OpenBao вернул некорректные данные машинной идентичности"
        ) from exc
    role_id = payload.get("role_id") if isinstance(payload, dict) else None
    secret_id = payload.get("secret_id") if isinstance(payload, dict) else None
    if (
        not isinstance(role_id, str)
        or not role_id
        or not isinstance(secret_id, str)
        or not secret_id
    ):
        raise InfraManagerError(
            "PVE/OpenBao не вернул полную машинную идентичность"
        )
    return {"role_id": role_id, "secret_id": secret_id}


def read_openbao_tls_ca(node: str) -> str:
    """Прочитать публичный TLS CA OpenBao из управляемого PVE bind mount."""
    result = _ssh(
        node,
        "cat",
        "/mnt/bindmounts/infra-manager/access/openbao-tls/ca.crt",
        capture=True,
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("-----BEGIN CERTIFICATE-----") or not certificate.endswith(
        "-----END CERTIFICATE-----"
    ):
        raise InfraManagerError("PVE не вернул TLS CA OpenBao")
    return certificate + "\n"


def sign_ssh_host_key(
    node: str,
    public_key: str,
    *,
    vmid: int,
    hostname: str,
    address: str,
) -> str:
    """Подписать host key после проверки VMID, имени и IP на PVE."""
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise InfraManagerError(
            "Для подписи SSH-сервера ожидается открытый ключ Ed25519"
        )
    payload = json.dumps(
        {
            "public_key": normalized,
            "vmid": vmid,
            "hostname": hostname,
            "address": address,
        },
        separators=(",", ":"),
    )
    result = _ssh_with_input(
        node,
        str(OPENBAO_HOST_COMMAND),
        "--sign-host-key",
        input_text=payload + "\n",
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise InfraManagerError(
            "PVE/OpenBao не вернул корректный SSH-сертификат сервера"
        )
    return certificate


def _features_from_config(config: str) -> dict[str, str]:
    """Разобрать строку features из pct config."""
    line = next(
        (item for item in config.splitlines() if item.startswith("features: ")),
        "",
    )
    if not line:
        return {}

    result: dict[str, str] = {}
    for item in line.partition(": ")[2].split(","):
        item = item.strip()
        if not item:
            continue
        name, separator, value = item.partition("=")
        result[name] = value if separator else "1"
    return result


def _render_features(features: dict[str, str]) -> str:
    return ",".join(
        f"{name}={value}"
        for name, value in sorted(features.items())
    )


def _container_running(node: str, vmid: int) -> bool:
    result = _ssh(node, "pct", "status", str(vmid), capture=True)
    return result.stdout.strip().endswith("running")


def ensure_container_host(node: str, vmid: int) -> None:
    """Обеспечить полный контракт container-host: nesting=1 и keyctl=1."""
    current_result = _ssh(node, "pct", "config", str(vmid), capture=True)
    current = _features_from_config(current_result.stdout)

    if current.get("nesting") == "1" and current.get("keyctl") == "1":
        console.ok(f"LXC {vmid}: параметры контейнера уже настроены")
        return

    desired = dict(current)
    desired["nesting"] = "1"
    desired["keyctl"] = "1"

    was_running = _container_running(node, vmid)
    if was_running:
        console.info(
            f"Остановка LXC {vmid} для изменения параметров контейнера"
        )
        _ssh(node, "pct", "stop", str(vmid))

    try:
        _ssh(
            node,
            "pct",
            "set",
            str(vmid),
            "--features",
            _render_features(desired),
        )
    finally:
        if was_running:
            _ssh(node, "pct", "start", str(vmid))

    verified = _ssh(node, "pct", "config", str(vmid), capture=True)
    actual = _features_from_config(verified.stdout)
    if actual.get("nesting") != "1" or actual.get("keyctl") != "1":
        raise InfraManagerError(
            f"LXC {vmid}: не удалось применить container-host features"
        )

    console.ok(f"LXC {vmid}: параметры контейнера настроены")


def ensure_infra_self_access(
    node: str,
    vmid: int,
    *,
    hostname: str,
    public_key: str,
) -> None:
    """Разрешить Ansible-ключу 910 вход в сам 910 через доверенный PVE."""
    key_parts = public_key.split()
    if len(key_parts) < 2 or not key_parts[0].startswith("ssh-"):
        raise InfraManagerError("Открытый Ansible-ключ 910 некорректен")
    normalized_key = " ".join(key_parts[:2])

    config = _ssh(node, "pct", "config", str(vmid), capture=True).stdout
    expected_hostname = f"hostname: {hostname}"
    ownership_markers = (
        "owner=proxmox-project",
        "role=infra-manager",
    )
    if expected_hostname not in config or not all(
        marker in config for marker in ownership_markers
    ):
        raise InfraManagerError(
            f"VMID {vmid} не подтверждён как принадлежащий infra-manager"
        )

    script = r"""
from pathlib import Path
import os
import sys

key = sys.argv[1].strip()
begin = "# BEGIN infra-manager ansible self"
end = "# END infra-manager ansible self"
ssh_dir = Path("/root/.ssh")
target = ssh_dir / "authorized_keys"

ssh_dir.mkdir(parents=True, exist_ok=True)
os.chmod(ssh_dir, 0o700)
lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []

result = []
inside = False
for line in lines:
    if line == begin:
        inside = True
        continue
    if line == end:
        inside = False
        continue
    if not inside:
        result.append(line)

while result and not result[-1].strip():
    result.pop()

if result:
    result.append("")
result.extend([begin, f"{key} infra-manager ansible guest", end])
target.write_text("\n".join(result) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
"""
    _ssh(
        node,
        "pct",
        "exec",
        str(vmid),
        "--",
        "python3",
        "-c",
        script,
        normalized_key,
    )
    console.ok(f"LXC {vmid}: постоянный Ansible-доступ к самому себе готов")


def apply_host_requirements(
    *,
    node: str,
    vmid: int,
    kind: str,
    features: tuple[str, ...],
) -> None:
    """Применить только те требования гостя, которым нужен root самого PVE."""
    if kind != "lxc":
        return
    if "container-host" in features:
        ensure_container_host(node, vmid)


def check_root_access(node: str) -> None:
    """Проверить рабочий root SSH-доступ к PVE без изменения состояния."""
    result = _ssh(node, "id", "-u", capture=True)
    if result.stdout.strip() != "0":
        raise InfraManagerError("SSH-доступ к PVE не выполняется от root")
