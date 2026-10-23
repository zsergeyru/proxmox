"""Плановое продление SSH Host CA сертификатов управляемых Linux-гостей.

Исполняется внутри LXC 910, не в Docker и не на PVE. Единственный
восстановительный канал после истечения сертификата — ранее сохранённый
открытый host key, полученный в сеансе с действительным сертификатом CA.
Ключ из сети при восстановлении никогда не принимается автоматически.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .common import InfraManagerError, run
from .guest_catalog import deployable_guests, load_manifest
from .guest_ssh import GuestSshSession
from .operation_lock import guest_operation_lock
from .pve import PveClient
from .pve_host import sign_ssh_host_key
from .settings import PATHS

STATE = Path("/mnt/persistent-state/ssh-host-renewal")
HOST_CERT = "/etc/ssh/ssh_host_ed25519_key-cert.pub"
HOST_PUB = "/etc/ssh/ssh_host_ed25519_key.pub"
RENEW_BEFORE = timedelta(days=14)
VALID_TO = re.compile(r"Valid:\s+from\s+\S+\s+to\s+(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)")
PUB_PART = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

# Выполняется на целевом Linux только после взаимно проверенного SSH.
# Два этапа: проверка оригинального host key и сертификата в .tmp,
# затем атомарная замена с откатом, если sshd или reload не прошли.
_INSTALL = r"""
import json
import os
import pathlib
import subprocess as sp
import sys
import tempfile

path = pathlib.Path("/etc/ssh/ssh_host_ed25519_key-cert.pub")
data = json.loads(sys.stdin.readline())
pub = data["public_key"].split()[:2]
cert = data["certificate"].strip()
current = pathlib.Path("/etc/ssh/ssh_host_ed25519_key.pub").read_text().split()[:2]
if current != pub or len(current) != 2 or current[0] != "ssh-ed25519":
    raise SystemExit("SSH host key изменился во время продления")

def check(*command):
    return sp.run(command, capture_output=True, text=True, timeout=20).returncode == 0

if not check("/usr/sbin/sshd", "-t"):
    raise SystemExit("Действующая конфигурация sshd не проходит sshd -t")
effective = sp.run(
    ["/usr/sbin/sshd", "-T"], capture_output=True, text=True, timeout=20,
)
if effective.returncode or (
    "hostcertificate /etc/ssh/ssh_host_ed25519_key-cert.pub"
    not in effective.stdout.lower().splitlines()
):
    raise SystemExit("HostCertificate не задан в sshd; автоматическая замена запрещена")

fd, name = tempfile.mkstemp(prefix=".infra-host-cert.", dir="/etc/ssh")
old = None
try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(cert + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(name, 0o644)
    inspected = sp.run(
        ["ssh-keygen", "-L", "-f", name],
        capture_output=True, text=True, timeout=20,
    )
    if inspected.returncode or "host certificate" not in inspected.stdout:
        raise SystemExit("OpenBao не выдал корректный серверный SSH-сертификат")
    if path.is_symlink():
        raise SystemExit("Файл сертификата не должен быть символической ссылкой")
    if path.exists():
        fd_old, old = tempfile.mkstemp(prefix=".infra-host-cert-prev.", dir="/etc/ssh")
        with os.fdopen(fd_old, "wb") as backup, path.open("rb") as source:
            backup.write(source.read())
            backup.flush()
            os.fsync(backup.fileno())
        os.chmod(old, 0o644)
    os.replace(name, path)
    name = None
    if not check("/usr/sbin/sshd", "-t") or not check("systemctl", "reload", "ssh.service"):
        if old is not None:
            os.replace(old, path)
            old = None
        else:
            path.unlink(missing_ok=True)
        sp.run(["systemctl", "reload", "ssh.service"], capture_output=True, timeout=20)
        raise SystemExit("Не удалось применить сертификат: прежний восстановлен")
finally:
    if name is not None:
        pathlib.Path(name).unlink(missing_ok=True)
    if old is not None:
        pathlib.Path(old).unlink(missing_ok=True)
"""

def _pair(raw: str) -> str:
    """Проверить открытый ключ без доверия к комментарию в нём."""

    parts = raw.strip().split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519" or not PUB_PART.fullmatch(parts[1]):
        raise InfraManagerError("Гость вернул некорректный серверный SSH-ключ")
    return " ".join(parts[:2])


def _pin_path(vmid: int) -> Path:
    if not isinstance(vmid, int) or vmid <= 0:
        raise InfraManagerError("Некорректный VMID")
    return STATE / f"{vmid}.json"


def _saved_pin(vmid: int, name: str, address: str) -> str | None:
    path = _pin_path(vmid)
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise InfraManagerError(f"VMID {vmid}: недопустимая запись доверия SSH")
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InfraManagerError(f"VMID {vmid}: запись SSH-доверия повреждена") from exc
    if (
        not isinstance(record, dict) or record.get("vmid") != vmid
        or record.get("name") != name or record.get("address") != address
        or not isinstance(record.get("public_key"), str)
    ):
        raise InfraManagerError(f"VMID {vmid}: идентичность закреплённого ключа изменена")
    return _pair(record["public_key"])


def _save_pin(vmid: int, name: str, address: str, pub: str) -> None:
    """Сохранить неизменяемую идентичность, подтверждённую доверенным Host CA."""

    if _saved_pin(vmid, name, address) is not None:
        return
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{vmid}.", dir=STATE)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(
                {"vmid": vmid, "name": name, "address": address, "public_key": pub},
                output, sort_keys=True,
            )
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_name, 0o600)
        # Ожидаем отдельную блокировку гостя и однопоточную операцию.
        if _pin_path(vmid).exists():
            raise InfraManagerError("Другая операция уже закрепила SSH-ключ")
        os.replace(temp_name, _pin_path(vmid))
    finally:
        Path(temp_name).unlink(missing_ok=True)


def _remaining(raw: str) -> timedelta:
    """Прочитать срок из стандартного вывода ssh-keygen -L."""

    with tempfile.TemporaryDirectory(prefix="host-cert-read-") as directory:
        path = Path(directory) / "cert.pub"
        path.write_text(raw.strip() + "\n", encoding="utf-8")
        result = run(["ssh-keygen", "-L", "-f", str(path)], capture_output=True, check=False)
    if result.returncode != 0 or "host certificate" not in result.stdout:
        raise InfraManagerError("Сервер вернул некорректный SSH host certificate")
    match = VALID_TO.search(result.stdout)
    if not match:
        raise InfraManagerError("Нельзя определить окончание SSH host certificate")
    # OpenSSH показывает даты в часовом поясе контроллера; сравниваем в нём же.
    end = datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    return end - datetime.now()


def _cat(session: GuestSshSession, path: str) -> str:
    result = run(session.ssh_command("cat " + shlex.quote(path)),
                 check=False, capture_output=True)
    if result.returncode:
        raise InfraManagerError("Недоступен серверный SSH-материал гостя")
    return result.stdout.strip()


def _renew_one(vmid: int, name: str, address: str, node: str) -> str:
    with guest_operation_lock(vmid, "ssh-host-renewal"):
        pinned = _saved_pin(vmid, name, address)
        restored = False
        try:
            with GuestSshSession(node, address) as session:
                verified, _ = session.verify_connection()
                if not verified:
                    raise InfraManagerError("Действующий host certificate не подтверждён")
                key = _pair(_cat(session, HOST_PUB))
                if pinned is not None and key != pinned:
                    raise InfraManagerError("VMID: SSH host key отличается от закреплённого")
                if pinned is None:
                    _save_pin(vmid, name, address, key)
                current = _cat(session, HOST_CERT)
                remaining = _remaining(current)
        except InfraManagerError:
            if pinned is None:
                raise InfraManagerError(
                    "Сертификат недоступен, а ранее проверенного ключа нет: "
                    "автоматическое восстановление запрещено"
                )
            restored = True
            with GuestSshSession(node, address, pinned_host_key=pinned) as session:
                verified, _ = session.verify_connection()
                if not verified or _pair(_cat(session, HOST_PUB)) != pinned:
                    raise InfraManagerError(
                        "Восстановление остановлено: закреплённый SSH-ключ не подтверждён"
                    )
                key = pinned
                remaining = timedelta(seconds=-1)
        if not restored and remaining > RENEW_BEFORE:
            return f"действует ещё {remaining.days} дн."
        # PVE-only проверит VMID, имя/IP ещё до подписи OpenBao.
        certificate = sign_ssh_host_key(
            node, key, vmid=vmid, hostname=name, address=address,
        )
        # Проверить новое содержимое и срок до первой записи на госте.
        if _remaining(certificate) <= RENEW_BEFORE:
            raise InfraManagerError("OpenBao выдал сертификат с недостаточным сроком")
        with GuestSshSession(
            node, address, pinned_host_key=key if restored else None
        ) as session:
            verified, _ = session.verify_connection()
            if not verified:
                raise InfraManagerError("Не удалось открыть доверенный SSH-сеанс обновления")
            command = "python3 -c " + shlex.quote(_INSTALL)
            result = run(
                session.ssh_command(command),
                input_text=json.dumps({"public_key": key, "certificate": certificate}) + "\n",
                check=False, capture_output=True,
            )
            if result.returncode:
                raise InfraManagerError(
                    "SSH host certificate не установлен (код операции "
                    f"{result.returncode}). Проверьте sshd и журнал гостя; "
                    "автоматический повтор не меняет SSH-ключ"
                )
        # Не принимать сообщение об успехе, пока не работает обычное SSH Host CA.
        with GuestSshSession(node, address) as session:
            verified, reason = session.verify_connection()
            if not verified or _pair(_cat(session, HOST_PUB)) != key:
                raise InfraManagerError(
                    "Новый SSH host certificate не подтвердил вход через Host CA: "
                    + reason[:160]
                )
        return "восстановлен" if restored else "продлён"


def run_scheduled() -> int:
    """Обходить только подтверждённые работающие VM/LXC из PVE API."""

    if not STATE.parent.is_mount():
        raise InfraManagerError(
            "Постоянный том 910 не подключён; обновление SSH запрещено"
        )
    defaults = load_manifest(PATHS.repo_root / "infrastructure/guests/defaults.yaml")
    node_default = defaults.get("defaults", {}).get("node")
    if not isinstance(node_default, str) or not node_default:
        raise InfraManagerError("Не указан PVE node в defaults.yaml")
    # Нельзя принимать IP из Git за доказательство существования объекта:
    # PVE-only signer перепроверит его непосредственно перед выпуском,
    # а сам обход пропускает отсутствующие и выключенные гости.
    resources = PveClient().data("/cluster/resources", query={"type": "vm"})
    if not isinstance(resources, list):
        raise InfraManagerError("PVE вернул некорректный перечень VM/LXC")
    by_vmid: dict[int, dict[str, object]] = {}
    for item in resources:
        if not isinstance(item, dict):
            raise InfraManagerError("PVE вернул некорректный объект VM/LXC")
        vmid = item.get("vmid")
        if not isinstance(vmid, int) or vmid <= 0 or vmid in by_vmid:
            raise InfraManagerError("PVE вернул дублированный или неверный VMID")
        by_vmid[vmid] = item

    errors = 0
    for identity in deployable_guests(PATHS.repo_root):
        resource = by_vmid.get(identity.vmid)
        if resource is None or resource.get("status") != "running":
            print(f"[ИНФО] VMID {identity.vmid} {identity.name}: не запущен, пропущен")
            continue
        try:
            address = identity.address
            if not address:
                print(f"[ИНФО] VMID {identity.vmid}: отсутствует management IP; пропущен")
                continue
            node = identity.source.get("node", node_default)
            if (
                not isinstance(node, str) or not node
                or resource.get("node") != node
                or resource.get("name") != identity.name
            ):
                raise InfraManagerError("Идентичность VMID/имени/PVE node отличается от Git")
            result = _renew_one(identity.vmid, identity.name, address, node)
            print(f"[ОК] VMID {identity.vmid} {identity.name}: {result}")
        except (InfraManagerError, OSError, ValueError) as exc:
            errors += 1
            print(f"[ПРЕДУПРЕЖДЕНИЕ] VMID {identity.vmid} {identity.name}: {exc}",
                  file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    try:
        raise SystemExit(run_scheduled())
    except (InfraManagerError, OSError, ValueError) as exc:
        print(f"[ОШИБКА] Автообновление SSH: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
