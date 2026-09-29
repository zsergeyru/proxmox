#!/usr/bin/env python3
"""Инициализация и разблокировка OpenBao из доверенного PVE-хоста."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

VMID = 910
OPENBAO_URL = "http://127.0.0.1:8200"
KEY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao")
KEY_PATH = KEY_DIR / "unseal.key"
LOCK_PATH = Path("/run/lock/infra-manager-openbao-unseal.lock")
CT_RUNTIME_DIR = Path("/run/infra-manager")
CT_KEY_PATH = CT_RUNTIME_DIR / "openbao-unseal.key"

STATUS_CODE = r"""
import urllib.request
with urllib.request.urlopen(
    "http://127.0.0.1:8200/v1/sys/seal-status",
    timeout=5,
) as response:
    print(response.read().decode("utf-8"))
"""

INIT_CODE = r"""
import json
import urllib.request
payload = json.dumps(
    {"secret_shares": 1, "secret_threshold": 1},
    separators=(",", ":"),
).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/sys/init",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
"""

UNSEAL_CODE = r"""
import json
import pathlib
import urllib.request
key = pathlib.Path("/run/infra-manager/openbao-unseal.key").read_text(
    encoding="utf-8"
).strip()
payload = json.dumps({"key": key}, separators=(",", ":")).encode("utf-8")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/sys/unseal",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    print(response.read().decode("utf-8"))
"""

REVOKE_ROOT_CODE = r"""
import sys
import urllib.request
token = sys.stdin.read().strip()
if not token:
    raise SystemExit("empty token")
request = urllib.request.Request(
    "http://127.0.0.1:8200/v1/auth/token/revoke-self",
    headers={"X-Vault-Token": token},
    method="POST",
)
with urllib.request.urlopen(request, timeout=30) as response:
    response.read()
"""


class OpenBaoHostError(RuntimeError):
    pass


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
        "OpenBao в LXC 910 не стал доступен"
        + (f": {last_error}" if last_error else "")
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


def unseal() -> None:
    status = read_status(wait=True)
    if not bool(status["initialized"]):
        print("[ИНФО] OpenBao ещё не инициализирован")
        return
    if not bool(status["sealed"]):
        print("[ОК] OpenBao уже разблокирован")
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

    print("[ОК] OpenBao разблокирован")


def revoke_initial_root_token(token: str) -> None:
    if not token:
        raise OpenBaoHostError("OpenBao вернул пустой initial root token")
    pct_exec(
        "python3",
        "-c",
        REVOKE_ROOT_CODE,
        capture=True,
        input_text=token,
    )
    print("[ОК] Initial root token отозван")


def initialize() -> None:
    status = read_status(wait=True)
    if bool(status["initialized"]):
        if not KEY_PATH.is_file() or KEY_PATH.stat().st_size == 0:
            raise OpenBaoHostError(
                "OpenBao уже инициализирован, но ключ разблокировки "
                "на PVE отсутствует"
            )
        unseal()
        print("[ОК] Инициализация OpenBao уже выполнена")
        return

    if KEY_PATH.exists():
        raise OpenBaoHostError(
            f"OpenBao не инициализирован, но найден старый ключ {KEY_PATH}; "
            "автоматическая перезапись запрещена"
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
    revoke_initial_root_token(root_token)
    del root_token

    verified = read_status(wait=False)
    if verified.get("initialized") is not True or verified.get("sealed") is not False:
        raise OpenBaoHostError(
            "Состояние OpenBao после инициализации не подтверждено"
        )

    print("[ОК] OpenBao инициализирован: 1 ключ, порог 1")
    print(f"[ОК] Unseal-ключ сохранён только на PVE: {KEY_PATH}")
    print("[ОК] Initial root token не сохранён и отозван")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Инициализация и разблокировка OpenBao для LXC 910"
    )
    parser.add_argument(
        "--initialize",
        action="store_true",
        help="Однократно инициализировать пустой OpenBao перед разблокировкой",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise OpenBaoHostError("Команда должна выполняться от root на PVE")

    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if args.initialize:
            initialize()
        else:
            unseal()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpenBaoHostError as exc:
        print(f"ОШИБКА: {exc}", file=__import__("sys").stderr)
        raise SystemExit(1)
