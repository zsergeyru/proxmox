#!/usr/bin/env python3
"""Инициализация и разблокировка OpenBao из доверенного PVE-хоста."""

from __future__ import annotations

import argparse
import fcntl
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_HOST_MODULE_ROOT = Path(__file__).resolve().parent
_INSTALLED_HOST_MODULE_ROOT = Path("/usr/local/lib/infra-manager")
if (_HOST_MODULE_ROOT / "openbao_host").is_dir():
    sys.path.insert(0, str(_HOST_MODULE_ROOT))
else:
    sys.path.insert(0, str(_INSTALLED_HOST_MODULE_ROOT))

from openbao_host.errors import OpenBaoHostError
from openbao_host.tls import prepare_tls_material as _prepare_tls_material
from openbao_host.snippets import (
    CHECK_KV_ACCESS_CODE,
    CHECK_SSH_ACCESS_CODE,
    CONFIGURE_CLIENT_SIGNING_ROLE_CODE,
    CONFIGURE_HOST_SIGNING_ROLE_CODE,
    CONFIGURE_KV_CODE,
    CONFIGURE_OTP_CONTRACT_CODE,
    CONFIGURE_SSH_ACCESS_CODE,
    CONFIGURE_SSH_CA_CODE,
    GENERATE_ROOT_CODE,
    HEALTH_CODE,
    INIT_CODE,
    ISSUE_MACHINE_CREDENTIALS_CODE,
    MATERIALIZE_RUNTIME_SECRETS_CODE,
    PREPARE_GENERATE_ROOT_CONFIG_CODE,
    READ_CLIENT_CA_CODE,
    READ_HOST_CA_CODE,
    RECONCILE_KV_ACCESS_CODE,
    REVOKE_ROOT_CODE,
    SIGN_CLIENT_KEY_CODE,
    SIGN_HOST_KEY_CODE,
    SSH_CA_STATUS_CODE,
    STATUS_CODE,
    UNSEAL_CODE,
    UPDATE_SEMAPHORE_API_TOKEN_CODE,
)

HOST_CONFIG_PATH = Path("/etc/infra-manager/openbao-host.json")
VMID = 0
TLS_ADDRESS = ""
TLS_DNS_NAME = ""
OPENBAO_URL = "http://127.0.0.1:8200"
KEY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao")
KEY_PATH = KEY_DIR / "unseal.key"
SSH_ACCESS_PATH = KEY_DIR / "ssh-access.json"
KV_ACCESS_PATH = KEY_DIR / "kv-access.json"
LOCK_PATH = Path("/run/lock/infra-manager-openbao-unseal.lock")
CT_RUNTIME_DIR = Path("/run/infra-manager")
CT_KEY_PATH = CT_RUNTIME_DIR / "openbao-unseal.key"
CT_COMPAT_CONFIG_PATH = CT_RUNTIME_DIR / "openbao-generate-root.hcl"
CT_OPENBAO_CONFIG_PATH = Path("/opt/infra-manager/compose/openbao/openbao.hcl")
CT_OPENBAO_DATA_PATH = Path("/mnt/persistent-state/openbao")
CT_CLIENT_CA_PATH = Path("/etc/infra-manager/ca/ssh-client-ca.pub")
CT_HOST_CA_PATH = Path("/etc/infra-manager/ca/ssh-host-ca.pub")
TEMP_OPENBAO_CONTAINER = "infra-manager-openbao-generate-root"
KV_MOUNT = "infra-secrets"
RUNTIME_SECRET_DIR = Path("/run/infra-manager/secrets")
LOG_LEVEL = "normal"

def load_host_identity() -> None:
    """Загрузить VMID, имя и адрес infra-manager из установленного PVE-конфига."""

    global VMID, TLS_ADDRESS, TLS_DNS_NAME

    try:
        payload = json.loads(HOST_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenBaoHostError(
            f"Не удалось прочитать конфигурацию infra-manager: {HOST_CONFIG_PATH}"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenBaoHostError("Конфигурация infra-manager должна быть JSON object")

    vmid = payload.get("vmid")
    name = payload.get("name")
    address = payload.get("address")
    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Конфигурация infra-manager содержит неверный vmid")
    if not isinstance(name, str) or not name:
        raise OpenBaoHostError("Конфигурация infra-manager содержит неверное name")
    if not isinstance(address, str) or not address:
        raise OpenBaoHostError("Конфигурация infra-manager содержит неверный address")
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError as exc:
        raise OpenBaoHostError(
            "Конфигурация infra-manager содержит некорректный IPv4"
        ) from exc
    if not isinstance(parsed, ipaddress.IPv4Address):
        raise OpenBaoHostError("OpenBao infra-manager поддерживает только IPv4")

    VMID = vmid
    TLS_ADDRESS = str(parsed)
    TLS_DNS_NAME = name


def log_detail(message: str) -> None:
    """Показать техническую подробность только в verbose-режиме."""
    if LOG_LEVEL == "verbose":
        print(message)


def log_status(message: str) -> None:
    """Показать рабочее состояние, кроме quiet-режима."""
    if LOG_LEVEL != "quiet":
        print(message)


def run(
    argv: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or "").strip()
        suffix = f": {detail}" if detail else ""
        raise OpenBaoHostError(
            f"Команда завершилась с кодом {result.returncode}: "
            f"{' '.join(argv)}{suffix}"
        )
    return result


def pct_exec(
    *args: str,
    capture: bool = False,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return run(
        ["pct", "exec", str(VMID), "--", *args],
        capture=capture,
        check=check,
        input_text=input_text,
    )


def container_running() -> bool:
    result = run(
        ["pct", "status", str(VMID)],
        capture=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip().endswith("running")


def read_status(*, wait: bool) -> dict[str, object]:
    attempts = 60 if wait else 1
    last_error = ""

    for _ in range(attempts):
        if container_running():
            result = pct_exec(
                "python3",
                "-c",
                STATUS_CODE,
                capture=True,
                check=False,
            )
            if result.returncode == 0:
                try:
                    payload = json.loads(result.stdout)
                except json.JSONDecodeError:
                    last_error = "OpenBao вернул некорректный JSON"
                else:
                    if isinstance(payload, dict):
                        initialized = payload.get("initialized")
                        sealed = payload.get("sealed")
                        if isinstance(initialized, bool) and isinstance(sealed, bool):
                            return payload
                    last_error = "OpenBao вернул неполное состояние"
            else:
                last_error = (result.stderr or "").strip()

        if attempts > 1:
            time.sleep(2)

    raise OpenBaoHostError(
        "OpenBao в infra-manager не стал доступен"
        + (f": {last_error}" if last_error else "")
    )



def wait_until_active(*, attempts: int = 60, delay: float = 1.0) -> dict[str, object]:
    """Дождаться, когда разблокированный Raft-узел станет активным."""

    last_detail = ""
    for attempt in range(attempts):
        result = pct_exec(
            "python3",
            "-c",
            HEALTH_CODE,
            capture=True,
            check=False,
        )
        if result.returncode == 0:
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_detail = "некорректный JSON health"
            else:
                if isinstance(payload, dict):
                    status = payload.get("http_status")
                    body = payload.get("body")
                    if (
                        status == 200
                        and isinstance(body, dict)
                        and body.get("initialized") is True
                        and body.get("sealed") is False
                        and body.get("standby") is not True
                    ):
                        return body
                    last_detail = (
                        f"HTTP {status}, standby="
                        f"{body.get('standby') if isinstance(body, dict) else '?'}"
                    )
                else:
                    last_detail = "некорректный health-ответ"
        else:
            last_detail = (result.stderr or "").strip() or (
                f"код {result.returncode}"
            )

        if attempt + 1 < attempts:
            time.sleep(delay)

    raise OpenBaoHostError(
        "OpenBao разблокирован, но Raft-узел не стал активным"
        + (f": {last_detail}" if last_detail else "")
    )


def require_active_openbao(action: str) -> dict[str, object]:
    """Проверить unseal и дождаться активного узла перед служебной операцией."""

    status = read_status(wait=False)
    if status.get("sealed") is not False:
        raise OpenBaoHostError(f"OpenBao запечатан; {action} невозможна")
    return wait_until_active()


def prepare_tls_material() -> None:
    """Подготовить TLS CA на PVE и серверный комплект infra-manager."""
    _prepare_tls_material(
        address=TLS_ADDRESS,
        dns_name=TLS_DNS_NAME,
        ssh_access_path=SSH_ACCESS_PATH,
        log_detail=log_detail,
    )


def write_unseal_key(key: str) -> None:
    if not key:
        raise OpenBaoHostError("OpenBao вернул пустой unseal-ключ")

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)

    fd, temporary_name = tempfile.mkstemp(
        prefix=".unseal.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(key)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, KEY_PATH)
        os.chmod(KEY_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_ssh_access_credentials(credentials: dict[str, object]) -> None:
    required = {"ssh-ca-config", "ssh-signer", "ssh-otp-config"}
    if set(credentials) != required:
        raise OpenBaoHostError(
            "OpenBao вернул неполный набор служебных SSH-доступов"
        )

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".ssh-access.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                credentials,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SSH_ACCESS_PATH)
        os.chmod(SSH_ACCESS_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def write_kv_access_credentials(credentials: dict[str, object]) -> None:
    required = {"kv-reader", "kv-semaphore-writer"}
    if set(credentials) != required:
        raise OpenBaoHostError(
            "OpenBao вернул неполный набор доступа к KV"
        )

    KEY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(KEY_DIR, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".kv-access.",
        dir=KEY_DIR,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                credentials,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, KV_ACCESS_PATH)
        os.chmod(KV_ACCESS_PATH, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def read_kv_access_credentials() -> dict[str, object]:
    if not KV_ACCESS_PATH.is_file() or KV_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа к KV: {KV_ACCESS_PATH}"
        )
    try:
        payload = json.loads(KV_ACCESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "Служебные данные доступа к KV содержат некорректный JSON"
        ) from exc
    required = {"kv-reader", "kv-semaphore-writer"}
    if not isinstance(payload, dict) or set(payload) != required:
        raise OpenBaoHostError(
            "Служебные данные доступа к KV имеют некорректный состав"
        )
    return payload


def check_kv_access() -> None:
    credentials = read_kv_access_credentials()
    result = pct_exec(
        "python3",
        "-c",
        CHECK_KV_ACCESS_CODE,
        capture=True,
        input_text=json.dumps(credentials, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат проверки KV"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError("KV v2 не прошёл проверку")


def configure_kv_access(root_token: str) -> None:
    if KV_ACCESS_PATH.exists():
        raise OpenBaoHostError(
            f"Найден старый файл доступа к KV {KV_ACCESS_PATH}; "
            "автоматическая перезапись запрещена"
        )
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_KV_CODE,
        capture=True,
        input_text=root_token,
    )
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа к KV"
        ) from exc
    if not isinstance(credentials, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа к KV"
        )
    write_kv_access_credentials(credentials)
    check_kv_access()
    log_detail("[ОК] KV v2 и ограниченный доступ к рабочим секретам настроены")


def reconcile_kv_access(root_token: str) -> None:
    """Восстановить служебные KV AppRole без изменения KV-данных."""

    result = pct_exec(
        "python3",
        "-c",
        RECONCILE_KV_ACCESS_CODE,
        capture=True,
        input_text=root_token,
    )
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные восстановленные данные доступа к KV"
        ) from exc
    if not isinstance(credentials, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректные восстановленные данные доступа к KV"
        )
    write_kv_access_credentials(credentials)
    check_kv_access()
    log_status("[ОК] Служебный доступ к KV восстановлен")


def materialize_runtime_secrets() -> None:
    credentials = read_kv_access_credentials()
    result = pct_exec(
        "python3",
        "-c",
        MATERIALIZE_RUNTIME_SECRETS_CODE,
        capture=True,
        input_text=json.dumps(credentials, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат материализации секретов"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Рабочие секреты не были материализованы из OpenBao"
        )
    log_detail(
        f"[ОК] Рабочие секреты материализованы во временную область {RUNTIME_SECRET_DIR}"
    )


def update_semaphore_api_token(api_token: str) -> None:
    clean = api_token.strip()
    if not clean or any(char.isspace() for char in clean):
        raise OpenBaoHostError("Некорректный API token Semaphore")
    credentials = read_kv_access_credentials()
    writer = credentials.get("kv-semaphore-writer")
    result = pct_exec(
        "python3",
        "-c",
        UPDATE_SEMAPHORE_API_TOKEN_CODE,
        capture=True,
        input_text=json.dumps(
            {
                "credentials": writer,
                "api_token": clean,
            },
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат обновления Semaphore token"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "API token Semaphore не был обновлён в OpenBao"
        )
    log_detail("[ОК] API token Semaphore обновлён в OpenBao")


def unseal() -> None:
    status = read_status(wait=True)
    if not bool(status["initialized"]):
        log_detail("[ИНФО] OpenBao ещё не инициализирован")
        return
    if not bool(status["sealed"]):
        wait_until_active()
        log_status("[ОК] OpenBao уже разблокирован и активен")
        return
    if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"OpenBao инициализирован, но отсутствует ключ: {KEY_PATH}"
        )

    pct_exec("install", "-d", "-m", "0700", str(CT_RUNTIME_DIR))
    run(
        [
            "pct",
            "push",
            str(VMID),
            str(KEY_PATH),
            str(CT_KEY_PATH),
            "--user",
            "0",
            "--group",
            "0",
            "--perms",
            "0600",
        ]
    )
    try:
        result = pct_exec(
            "python3",
            "-c",
            UNSEAL_CODE,
            capture=True,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise OpenBaoHostError(
                "OpenBao вернул некорректный ответ на разблокировку"
            ) from exc
        if not isinstance(payload, dict) or payload.get("sealed") is not False:
            raise OpenBaoHostError("OpenBao остался запечатан после разблокировки")
    finally:
        pct_exec("rm", "-f", str(CT_KEY_PATH), check=False)

    wait_until_active()
    log_status("[ОК] OpenBao разблокирован и активен")


def _revoke_root_token(token: str) -> None:
    if not token:
        raise OpenBaoHostError("OpenBao вернул пустой корневой токен")
    pct_exec(
        "python3",
        "-c",
        REVOKE_ROOT_CODE,
        capture=True,
        input_text=token,
    )


def revoke_initial_root_token(token: str) -> None:
    _revoke_root_token(token)
    log_detail("[ОК] Initial root token отозван")


def revoke_temporary_root_token(token: str) -> None:
    _revoke_root_token(token)
    log_detail("[ОК] Временный корневой токен OpenBao отозван")


def _generate_root_from_running_server() -> str:
    if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найден ключ разблокировки OpenBao: {KEY_PATH}"
        )

    unseal_key = KEY_PATH.read_text(encoding="utf-8").strip()
    result = pct_exec(
        "python3",
        "-c",
        GENERATE_ROOT_CODE,
        capture=True,
        input_text=unseal_key,
    )
    token = result.stdout.strip()
    if not token or any(char.isspace() for char in token):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный временный корневой токен"
        )
    return token


def generate_temporary_root_token() -> str:
    image_result = pct_exec(
        "docker",
        "inspect",
        "-f",
        "{{.Config.Image}}",
        "openbao",
        capture=True,
    )
    image = image_result.stdout.strip()
    if not image:
        raise OpenBaoHostError("Не удалось определить образ OpenBao")

    pct_exec(
        "python3",
        "-c",
        PREPARE_GENERATE_ROOT_CONFIG_CODE,
        capture=True,
    )

    token: str | None = None
    original_stopped = False
    try:
        pct_exec("docker", "stop", "openbao", capture=True)
        original_stopped = True

        pct_exec(
            "docker",
            "rm",
            "-f",
            TEMP_OPENBAO_CONTAINER,
            capture=True,
            check=False,
        )
        pct_exec(
            "docker",
            "run",
            "-d",
            "--name",
            TEMP_OPENBAO_CONTAINER,
            "--network",
            "host",
            "--user",
            "0",
            "-e",
            "BAO_SKIP_DROP_ROOT=1",
            "-e",
            "SKIP_CHOWN=1",
            "-v",
            f"{CT_OPENBAO_DATA_PATH}:/openbao/file",
            "-v",
            "/etc/infra-manager/openbao/tls:/openbao/tls:ro",
            "-v",
            f"{CT_COMPAT_CONFIG_PATH}:/openbao/config/openbao.hcl:ro",
            image,
            "server",
            capture=True,
        )

        unseal()
        token = _generate_root_from_running_server()
    finally:
        pct_exec(
            "docker",
            "rm",
            "-f",
            TEMP_OPENBAO_CONTAINER,
            capture=True,
            check=False,
        )
        pct_exec("rm", "-f", str(CT_COMPAT_CONFIG_PATH), check=False)

        if original_stopped:
            pct_exec("docker", "start", "openbao", capture=True)
            unseal()

    if token is None:
        raise OpenBaoHostError("Временный корневой токен OpenBao не выпущен")

    log_detail("[ОК] Временный корневой токен выпущен в защищённом локальном окне")
    return token


def ssh_cas_ready() -> bool:
    result = pct_exec(
        "python3",
        "-c",
        SSH_CA_STATUS_CODE,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректное состояние SSH-центров доверия"
        ) from exc
    if not isinstance(payload, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректное состояние SSH-центров доверия"
        )
    return (
        payload.get("client") is True
        and payload.get("host") is True
        and payload.get("distinct") is True
    )


def configure_ssh_cas(token: str) -> None:
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_SSH_CA_CODE,
        capture=True,
        input_text=token,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный ответ настройки SSH-центров доверия"
        ) from exc
    client = payload.get("client_public_key") if isinstance(payload, dict) else None
    host = payload.get("host_public_key") if isinstance(payload, dict) else None
    if (
        not isinstance(client, str)
        or not isinstance(host, str)
        or not client
        or not host
        or client == host
    ):
        raise OpenBaoHostError(
            "OpenBao не подтвердил два независимых SSH-центра доверия"
        )


def ensure_ssh_cas(root_token: str | None = None) -> None:
    if ssh_cas_ready():
        log_detail("[ОК] Два SSH-центра доверия OpenBao уже готовы")
        return

    if root_token is None:
        raise OpenBaoHostError(
            "OpenBao уже инициализирован без SSH-центров доверия. "
            "OpenBao 2.7 не разрешает безопасно выпустить новый root token "
            "только по unseal-ключу; требуется явная миграция или "
            "переинициализация пустого хранилища"
        )

    configure_ssh_cas(root_token)
    if not ssh_cas_ready():
        raise OpenBaoHostError(
            "SSH-центры доверия OpenBao не прошли итоговую проверку"
        )
    log_detail("[ОК] Два SSH-центра доверия OpenBao созданы")


def read_ssh_access_credentials(*, allow_legacy: bool = False) -> dict[str, object]:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа OpenBao: {SSH_ACCESS_PATH}"
        )
    try:
        payload = json.loads(SSH_ACCESS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "Служебные данные доступа OpenBao содержат некорректный JSON"
        ) from exc
    required = {"ssh-ca-config", "ssh-signer", "ssh-otp-config"}
    legacy = {"ssh-ca-config", "ssh-signer"}
    if not isinstance(payload, dict) or (
        set(payload) != required and not (allow_legacy and set(payload) == legacy)
    ):
        raise OpenBaoHostError(
            "Служебные данные доступа OpenBao имеют некорректный состав"
        )
    return payload


def ensure_client_signing_role() -> None:
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-ca-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_CLIENT_SIGNING_ROLE_CODE,
        capture=True,
        input_text=json.dumps(config_access, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат настройки роли подписи"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Роль подписи клиентских SSH-сертификатов не прошла проверку"
        )
    log_detail("[ОК] Роль подписи SSH-клиента infra-manager настроена")


def sign_client_public_key(public_key: str) -> str:
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise OpenBaoHostError("Для подписи ожидается открытый ключ Ed25519")

    credentials = read_ssh_access_credentials()
    signer_access = credentials.get("ssh-signer")
    request_payload = json.dumps(
        {
            "credentials": signer_access,
            "public_key": normalized,
        },
        separators=(",", ":"),
    )
    result = pct_exec(
        "python3",
        "-c",
        SIGN_CLIENT_KEY_CODE,
        capture=True,
        input_text=request_payload,
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise OpenBaoHostError(
            "OpenBao вернул некорректный SSH-сертификат клиента"
        )
    return certificate



def sync_otp_contract(sources: list[dict[str, object]]) -> None:
    """Синхронизировать ssh-otp и auth/machine по подготовленному контракту."""
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-otp-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_OTP_CONTRACT_CODE,
        capture=True,
        input_text=json.dumps(
            {"credentials": config_access, "sources": sources},
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат синхронизации SSH OTP"
        ) from exc
    expected_roles = sorted(
        f"guest-{item['vmid']}"
        for item in sources
        if isinstance(item, dict) and isinstance(item.get("vmid"), int)
    )
    if payload != {
        "roles": expected_roles,
        "policy": "machine-ssh-otp",
    }:
        raise OpenBaoHostError(
            "SSH OTP/AppRole не соответствуют переданному access-контракту"
        )
    log_status("[ОК] OpenBao SSH OTP и машинные AppRole синхронизированы")


def issue_machine_credentials(vmid: int) -> dict[str, str]:
    """Выпустить новый SecretID только для одной существующей машинной роли."""
    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Некорректный VMID машинной идентичности")
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-otp-config")
    result = pct_exec(
        "python3",
        "-c",
        ISSUE_MACHINE_CREDENTIALS_CODE,
        capture=True,
        input_text=json.dumps(
            {"credentials": config_access, "vmid": vmid},
            separators=(",", ":"),
        ),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные данные машинной идентичности"
        ) from exc
    role_id = payload.get("role_id") if isinstance(payload, dict) else None
    secret_id = payload.get("secret_id") if isinstance(payload, dict) else None
    returned_vmid = payload.get("vmid") if isinstance(payload, dict) else None
    if (
        returned_vmid != vmid
        or not isinstance(role_id, str)
        or not role_id
        or not isinstance(secret_id, str)
        or not secret_id
    ):
        raise OpenBaoHostError(
            "OpenBao не выдал полную машинную идентичность"
        )
    return {"role_id": role_id, "secret_id": secret_id}


def ensure_host_signing_role() -> None:
    credentials = read_ssh_access_credentials()
    config_access = credentials.get("ssh-ca-config")
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_HOST_SIGNING_ROLE_CODE,
        capture=True,
        input_text=json.dumps(config_access, separators=(",", ":")),
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат настройки роли host-подписи"
        ) from exc
    if payload != {"ready": True}:
        raise OpenBaoHostError(
            "Роль подписи SSH-серверов managed-host не прошла проверку"
        )
    log_detail("[ОК] Роль подписи SSH-серверов managed-host настроена")


def validate_managed_host_target(
    vmid: int,
    hostname: str,
    address: str,
) -> list[str]:
    import ipaddress

    if not isinstance(vmid, int) or vmid <= 0:
        raise OpenBaoHostError("Некорректный VMID для host-сертификата")
    clean_hostname = hostname.strip()
    if (
        not clean_hostname
        or "," in clean_hostname
        or any(character.isspace() for character in clean_hostname)
    ):
        raise OpenBaoHostError("Некорректное имя SSH-сервера")
    try:
        ip = ipaddress.ip_address(address.strip())
    except ValueError as exc:
        raise OpenBaoHostError("Некорректный IP SSH-сервера") from exc
    if ip.version != 4 or not ip.is_private:
        raise OpenBaoHostError(
            "Host-сертификаты разрешены только для частных IPv4-адресов"
        )
    clean_address = str(ip)

    pct = run(
        ["pct", "config", str(vmid)],
        capture=True,
        check=False,
    )
    if pct.returncode == 0:
        config = pct.stdout
        actual_hostname = next(
            (
                line.removeprefix("hostname: ").strip()
                for line in config.splitlines()
                if line.startswith("hostname: ")
            ),
            "",
        )
        if actual_hostname != clean_hostname:
            raise OpenBaoHostError(
                f"LXC {vmid} не подтверждает hostname {clean_hostname}"
            )
        network_line = next(
            (line for line in config.splitlines() if line.startswith("net0: ")),
            "",
        )
        if f"ip={clean_address}/" not in network_line:
            raise OpenBaoHostError(
                f"LXC {vmid} не подтверждает IP {clean_address}"
            )
        return [clean_hostname, clean_address]

    qemu = run(
        ["qm", "config", str(vmid)],
        capture=True,
        check=False,
    )
    if qemu.returncode != 0:
        raise OpenBaoHostError(
            f"VMID {vmid} не найден как управляемый LXC или VM"
        )
    config = qemu.stdout
    actual_name = next(
        (
            line.removeprefix("name: ").strip()
            for line in config.splitlines()
            if line.startswith("name: ")
        ),
        "",
    )
    if actual_name != clean_hostname:
        raise OpenBaoHostError(
            f"VM {vmid} не подтверждает name {clean_hostname}"
        )
    ip_line = next(
        (line for line in config.splitlines() if line.startswith("ipconfig0: ")),
        "",
    )
    if f"ip={clean_address}/" not in ip_line:
        raise OpenBaoHostError(
            f"VM {vmid} не подтверждает IP {clean_address}"
        )
    return [clean_hostname, clean_address]


def sign_host_public_key(public_key: str, principals: list[str]) -> str:
    normalized = public_key.strip()
    if not normalized.startswith("ssh-ed25519 "):
        raise OpenBaoHostError(
            "Для подписи SSH-сервера ожидается открытый ключ Ed25519"
        )
    cleaned = []
    for principal in principals:
        value = principal.strip()
        if (
            not value
            or "," in value
            or any(character.isspace() for character in value)
        ):
            raise OpenBaoHostError("Некорректный principal SSH-сервера")
        cleaned.append(value)
    if not cleaned:
        raise OpenBaoHostError("Не заданы principals SSH-сервера")

    credentials = read_ssh_access_credentials()
    signer_access = credentials.get("ssh-signer")
    request_payload = json.dumps(
        {
            "credentials": signer_access,
            "public_key": normalized,
            "principals": cleaned,
        },
        separators=(",", ":"),
    )
    result = pct_exec(
        "python3",
        "-c",
        SIGN_HOST_KEY_CODE,
        capture=True,
        input_text=request_payload,
    )
    certificate = result.stdout.strip()
    if not certificate.startswith("ssh-ed25519-cert-v01@openssh.com "):
        raise OpenBaoHostError(
            "OpenBao вернул некорректный SSH-сертификат сервера"
        )
    return certificate

def ssh_access_credentials_complete() -> bool:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        return False
    try:
        read_ssh_access_credentials()
    except OpenBaoHostError:
        return False
    return True


def ssh_access_credentials_current() -> bool:
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        return False
    try:
        payload = json.loads(SSH_ACCESS_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and set(payload) == {
        "ssh-ca-config",
        "ssh-signer",
        "ssh-otp-config",
    }


def check_ssh_access() -> None:
    if not ssh_cas_ready():
        raise OpenBaoHostError(
            "Два независимых SSH-центра доверия OpenBao не готовы"
        )
    if not SSH_ACCESS_PATH.is_file() or SSH_ACCESS_PATH.stat().st_size == 0:
        raise OpenBaoHostError(
            f"Не найдены служебные данные доступа OpenBao: {SSH_ACCESS_PATH}"
        )
    credentials = SSH_ACCESS_PATH.read_text(encoding="utf-8")
    result = pct_exec(
        "python3",
        "-c",
        CHECK_SSH_ACCESS_CODE,
        capture=True,
        input_text=credentials,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный результат проверки служебного доступа"
        ) from exc
    if payload != {
        "ssh-ca-config": True,
        "ssh-signer": True,
        "ssh-otp-config": True,
    }:
        raise OpenBaoHostError(
            "Служебные доступы OpenBao не прошли проверку"
        )


def configure_ssh_access(
    root_token: str,
    *,
    rotate_secret_ids: bool = False,
) -> None:
    existing_credentials: dict[str, object] = {}
    if SSH_ACCESS_PATH.exists():
        existing_credentials = read_ssh_access_credentials(allow_legacy=True)
    result = pct_exec(
        "python3",
        "-c",
        CONFIGURE_SSH_ACCESS_CODE,
        capture=True,
        input_text=json.dumps(
            {
                "root_token": root_token,
                "existing_credentials": existing_credentials,
                "rotate_secret_ids": rotate_secret_ids,
            },
            separators=(",", ":"),
        ),
    )
    try:
        credentials = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа"
        ) from exc
    if not isinstance(credentials, dict):
        raise OpenBaoHostError(
            "OpenBao вернул некорректные служебные данные доступа"
        )
    write_ssh_access_credentials(credentials)
    check_ssh_access()
    log_detail("[ОК] Ограниченный служебный доступ к SSH-центрам настроен")


def client_ca_published() -> bool:
    result = pct_exec(
        "cat",
        str(CT_CLIENT_CA_PATH),
        capture=True,
        check=False,
    )
    key = result.stdout.strip() if result.returncode == 0 else ""
    return key.startswith(("ssh-", "ecdsa-", "sk-"))


def publish_client_ca(root_token: str) -> None:
    result = pct_exec(
        "python3",
        "-c",
        READ_CLIENT_CA_CODE,
        capture=True,
        input_text=root_token,
    )
    key = result.stdout.strip()
    if not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный открытый ключ центра доступа"
        )

    install_code = r"""
import os
import pathlib
import sys
import tempfile

target = pathlib.Path("/etc/infra-manager/ca/ssh-client-ca.pub")
key = sys.stdin.read().strip()
if not key.startswith(("ssh-", "ecdsa-", "sk-")):
    raise SystemExit("invalid SSH CA public key")

target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(
    prefix=".ssh-client-ca.",
    dir=target.parent,
    text=True,
)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    os.chmod(target, 0o644)
finally:
    temporary.unlink(missing_ok=True)
"""
    pct_exec(
        "python3",
        "-c",
        install_code,
        capture=True,
        input_text=key,
    )
    if not client_ca_published():
        raise OpenBaoHostError(
            "Открытый ключ центра доступа не прошёл проверку после публикации"
        )
    log_detail(
        f"[ОК] Открытый ключ центра доступа опубликован: {CT_CLIENT_CA_PATH}"
    )


def host_ca_published() -> bool:
    result = pct_exec(
        "cat",
        str(CT_HOST_CA_PATH),
        capture=True,
        check=False,
    )
    key = result.stdout.strip() if result.returncode == 0 else ""
    return key.startswith(("ssh-", "ecdsa-", "sk-"))


def publish_host_ca() -> None:
    result = pct_exec(
        "python3",
        "-c",
        READ_HOST_CA_CODE,
        capture=True,
    )
    key = result.stdout.strip()
    if not key.startswith(("ssh-", "ecdsa-", "sk-")):
        raise OpenBaoHostError(
            "OpenBao не вернул корректный открытый ключ центра SSH-серверов"
        )

    install_code = r"""
import os
import pathlib
import sys
import tempfile

target = pathlib.Path("/etc/infra-manager/ca/ssh-host-ca.pub")
key = sys.stdin.read().strip()
if not key.startswith(("ssh-", "ecdsa-", "sk-")):
    raise SystemExit("invalid SSH host CA public key")

target.parent.mkdir(parents=True, exist_ok=True)
fd, temporary_name = tempfile.mkstemp(
    prefix=".ssh-host-ca.",
    dir=target.parent,
    text=True,
)
temporary = pathlib.Path(temporary_name)
try:
    os.fchmod(fd, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(key)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, target)
    os.chmod(target, 0o644)
finally:
    temporary.unlink(missing_ok=True)
"""
    pct_exec(
        "python3",
        "-c",
        install_code,
        capture=True,
        input_text=key,
    )
    if not host_ca_published():
        raise OpenBaoHostError(
            "Открытый ключ центра SSH-серверов не прошёл проверку после публикации"
        )
    log_detail(
        f"[ОК] Открытый ключ центра SSH-серверов опубликован: {CT_HOST_CA_PATH}"
    )


def ensure_existing_openbao_ssh() -> None:
    ssh_access_broken = False
    kv_access_broken = False

    if (
        ssh_cas_ready()
        and ssh_access_credentials_complete()
        and KV_ACCESS_PATH.is_file()
        and client_ca_published()
    ):
        try:
            check_ssh_access()
        except OpenBaoHostError:
            ssh_access_broken = True
        try:
            check_kv_access()
        except OpenBaoHostError:
            kv_access_broken = True

        if not ssh_access_broken and not kv_access_broken:
            materialize_runtime_secrets()
            publish_host_ca()
            ensure_client_signing_role()
            ensure_host_signing_role()
            log_detail(
                "[ОК] SSH-центры, KV v2 и служебные доступы OpenBao уже готовы"
            )
            return

        log_status(
            "[ИНФО] Служебный AppRole OpenBao требует восстановления"
        )

    root_token = generate_temporary_root_token()
    try:
        ensure_ssh_cas(root_token)

        if ssh_access_credentials_complete() and not ssh_access_broken:
            check_ssh_access()
        else:
            configure_ssh_access(
                root_token,
                rotate_secret_ids=ssh_access_broken,
            )

        if KV_ACCESS_PATH.is_file() and not kv_access_broken:
            check_kv_access()
        elif KV_ACCESS_PATH.is_file():
            reconcile_kv_access(root_token)
        else:
            configure_kv_access(root_token)

        materialize_runtime_secrets()
        publish_client_ca(root_token)
        publish_host_ca()
        ensure_client_signing_role()
        ensure_host_signing_role()
    finally:
        revoke_temporary_root_token(root_token)
        del root_token


def initialize() -> None:
    status = read_status(wait=True)
    if bool(status["initialized"]):
        if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
            raise OpenBaoHostError(
                "OpenBao уже инициализирован, но ключ разблокировки "
                "на PVE отсутствует"
            )
        unseal()
        ensure_existing_openbao_ssh()
        log_detail("[ОК] Инициализация OpenBao уже выполнена")
        return

    stale = [
        path
        for path in (KEY_PATH, SSH_ACCESS_PATH, KV_ACCESS_PATH)
        if path.exists()
    ]
    if stale:
        raise OpenBaoHostError(
            "OpenBao не инициализирован, но найдены старые PVE-only данные: "
            + ", ".join(str(path) for path in stale)
            + "; автоматическая перезапись запрещена"
        )

    result = pct_exec(
        "python3",
        "-c",
        INIT_CODE,
        capture=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise OpenBaoHostError(
            "OpenBao вернул некорректный ответ инициализации"
        ) from exc

    keys = payload.get("keys_base64")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        keys = payload.get("keys")
    if not isinstance(keys, list) or len(keys) != 1 or not isinstance(keys[0], str):
        raise OpenBaoHostError(
            "OpenBao не вернул ровно один ожидаемый unseal-ключ"
        )

    root_token = payload.pop("root_token", None)
    if not isinstance(root_token, str) or not root_token:
        raise OpenBaoHostError("OpenBao не вернул initial root token")

    write_unseal_key(keys[0])
    del keys
    payload.clear()

    unseal()
    try:
        ensure_ssh_cas(root_token)
        configure_ssh_access(root_token)
        configure_kv_access(root_token)
        materialize_runtime_secrets()
        publish_client_ca(root_token)
        publish_host_ca()
        ensure_client_signing_role()
        ensure_host_signing_role()
    finally:
        revoke_initial_root_token(root_token)
        del root_token

    verified = read_status(wait=False)
    if verified.get("initialized") is not True or verified.get("sealed") is not False:
        raise OpenBaoHostError(
            "Состояние OpenBao после инициализации не подтверждено"
        )

    log_detail("[ОК] OpenBao инициализирован: 1 ключ, порог 1")
    log_detail(f"[ОК] Unseal-ключ сохранён только на PVE: {KEY_PATH}")
    log_detail("[ОК] SSH-центры доверия: ssh-client-signer и ssh-host-signer")
    log_detail("[ОК] Служебные AppRole-данные хранятся только на PVE")
    log_detail("[ОК] Рабочие PVE, Git и Semaphore credentials сохранены в KV v2")
    log_detail("[ОК] Initial root token не сохранён и отозван")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Инициализация и разблокировка OpenBao для infra-manager"
    )
    parser.add_argument(
        "--log-level",
        choices=("normal", "verbose", "quiet"),
        default="normal",
        help="Уровень вывода: normal, verbose или quiet",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--initialize",
        action="store_true",
        help="Однократно инициализировать пустой OpenBao перед разблокировкой",
    )
    mode.add_argument(
        "--prepare-tls",
        action="store_true",
        help="Подготовить TLS CA и серверный сертификат OpenBao на PVE",
    )
    mode.add_argument(
        "--sign-client-key",
        action="store_true",
        help="Подписать переданный через stdin открытый ключ SSH-клиента",
    )
    mode.add_argument(
        "--sign-host-key",
        action="store_true",
        help="Подписать переданный через stdin открытый ключ SSH-сервера",
    )
    mode.add_argument(
        "--sync-otp-contract",
        action="store_true",
        help="Синхронизировать ssh-otp и auth/machine из stdin",
    )
    mode.add_argument(
        "--check-ssh-access",
        action="store_true",
        help="Проверить служебные SSH-доступы, ssh-otp и auth/machine",
    )
    mode.add_argument(
        "--issue-machine-credentials",
        action="store_true",
        help="Выпустить RoleID/SecretID одной машинной AppRole из stdin",
    )
    mode.add_argument(
        "--check-kv",
        action="store_true",
        help="Проверить KV v2 и ограниченный доступ к рабочим секретам",
    )
    mode.add_argument(
        "--update-semaphore-api-token",
        action="store_true",
        help="Обновить API token Semaphore в KV v2 из stdin",
    )
    args = parser.parse_args()

    global LOG_LEVEL
    LOG_LEVEL = args.log_level

    if os.geteuid() != 0:
        raise OpenBaoHostError("Команда должна выполняться от root на PVE")

    load_host_identity()

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.prepare_tls:
            prepare_tls_material()
        elif args.initialize:
            prepare_tls_material()
            initialize()
        elif args.sign_client_key:
            require_active_openbao("подпись SSH")
            public_key = __import__("sys").stdin.read()
            print(sign_client_public_key(public_key))
        elif args.sign_host_key:
            require_active_openbao("подпись SSH")
            request_payload = json.loads(__import__("sys").stdin.read())
            if not isinstance(request_payload, dict):
                raise OpenBaoHostError("Некорректный запрос подписи SSH-сервера")
            public_key = request_payload.get("public_key")
            vmid = request_payload.get("vmid")
            hostname = request_payload.get("hostname")
            address = request_payload.get("address")
            if (
                not isinstance(public_key, str)
                or not isinstance(vmid, int)
                or not isinstance(hostname, str)
                or not isinstance(address, str)
            ):
                raise OpenBaoHostError("Неполный запрос подписи SSH-сервера")
            principals = validate_managed_host_target(
                vmid,
                hostname,
                address,
            )
            print(sign_host_public_key(public_key, principals))
        elif args.sync_otp_contract:
            require_active_openbao("синхронизация SSH OTP")
            payload = json.loads(__import__("sys").stdin.read())
            sources = payload.get("sources") if isinstance(payload, dict) else None
            if not isinstance(sources, list):
                raise OpenBaoHostError("Не передан OTP-контракт")
            sync_otp_contract(sources)
        elif args.check_ssh_access:
            require_active_openbao("проверка SSH-доступов")
            check_ssh_access()
            log_status(
                "[ОК] Служебные SSH-доступы, ssh-otp и auth/machine подтверждены"
            )
        elif args.issue_machine_credentials:
            require_active_openbao("выдача машинной идентичности")
            payload = json.loads(__import__("sys").stdin.read())
            vmid = payload.get("vmid") if isinstance(payload, dict) else None
            if not isinstance(vmid, int) or vmid <= 0:
                raise OpenBaoHostError("Не передан корректный VMID")
            print(
                json.dumps(
                    issue_machine_credentials(vmid),
                    separators=(",", ":"),
                )
            )
        elif args.check_kv:
            require_active_openbao("проверка KV")
            check_kv_access()
            log_status("[ОК] KV v2 и ограниченные AppRole подтверждены")
        elif args.update_semaphore_api_token:
            require_active_openbao("обновление Semaphore token")
            update_semaphore_api_token(__import__("sys").stdin.read())
        else:
            unseal()
            status = read_status(wait=False)
            if (
                status.get("initialized") is True
                and status.get("sealed") is False
            ):
                ensure_existing_openbao_ssh()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenBaoHostError as exc:
        print(f"ОШИБКА: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
