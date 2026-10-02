"""TLS-материалы OpenBao, которыми владеет доверенный PVE-хост."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .errors import OpenBaoHostError

TLS_PVE_ONLY_DIR = Path("/mnt/bindmounts/infra-manager/pve-only/openbao-tls")
TLS_ACCESS_DIR = Path("/mnt/bindmounts/infra-manager/access/openbao-tls")
TLS_CA_KEY = TLS_PVE_ONLY_DIR / "ca.key"
TLS_CA_CERT = TLS_PVE_ONLY_DIR / "ca.crt"
TLS_ACCESS_CA_CERT = TLS_ACCESS_DIR / "ca.crt"
TLS_SERVER_KEY = TLS_ACCESS_DIR / "server.key"
TLS_SERVER_CERT = TLS_ACCESS_DIR / "server.crt"


def _openssl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["openssl", *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise OpenBaoHostError(
            "OpenSSL завершился ошибкой"
            + (f": {detail}" if detail else "")
        )
    return result


def _public_key_from_certificate(path: Path) -> str:
    return _openssl("x509", "-in", str(path), "-noout", "-pubkey").stdout.strip()


def _public_key_from_private_key(path: Path) -> str:
    return _openssl("pkey", "-in", str(path), "-pubout").stdout.strip()


def _atomic_copy(source: Path, target: Path, *, mode: int, uid: int, gid: int) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            with source.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _tls_ca_ready() -> bool:
    if not TLS_CA_KEY.is_file() or not TLS_CA_CERT.is_file():
        return False
    try:
        return (
            _public_key_from_certificate(TLS_CA_CERT)
            == _public_key_from_private_key(TLS_CA_KEY)
        )
    except OpenBaoHostError:
        return False


def _tls_ca_has_signing_usage() -> bool:
    usage = _openssl(
        "x509", "-in", str(TLS_CA_CERT), "-noout", "-ext", "keyUsage"
    ).stdout
    return "Certificate Sign" in usage and "CRL Sign" in usage


def _repair_legacy_tls_ca_certificate(log_detail) -> None:
    """Перевыпустить только сертификат старого CA с тем же закрытым ключом."""
    details = _openssl("x509", "-in", str(TLS_CA_CERT), "-noout", "-text").stdout
    subject = _openssl("x509", "-in", str(TLS_CA_CERT), "-noout", "-subject").stdout
    if "CA:TRUE" not in details or not subject.replace(" = ", "=").strip().endswith(
        "CN=infra-manager OpenBao TLS CA"
    ):
        raise OpenBaoHostError(
            "Существующий TLS CA не соответствует проектному центру доверия"
        )
    _openssl("verify", "-CAfile", str(TLS_CA_CERT), str(TLS_CA_CERT))
    with tempfile.TemporaryDirectory(
        prefix=".openbao-ca-repair.", dir=TLS_PVE_ONLY_DIR
    ) as temporary_dir:
        repaired = Path(temporary_dir) / "ca.crt"
        _openssl(
            "req", "-x509", "-new", "-key", str(TLS_CA_KEY),
            "-sha256", "-days", "3650",
            "-subj", "/CN=infra-manager OpenBao TLS CA",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-out", str(repaired),
        )
        _openssl("verify", "-CAfile", str(repaired), str(repaired))
        if TLS_SERVER_CERT.is_file():
            _openssl("verify", "-CAfile", str(repaired), str(TLS_SERVER_CERT))
        _atomic_copy(
            TLS_CA_CERT,
            TLS_PVE_ONLY_DIR / "ca.pre-key-usage.crt",
            mode=0o644, uid=0, gid=0,
        )
        _atomic_copy(repaired, TLS_CA_CERT, mode=0o644, uid=0, gid=0)
    log_detail("[ОК] TLS CA перевыпущен с keyCertSign без смены ключа")


def _tls_server_ready(address: str, dns_name: str) -> bool:
    if not TLS_SERVER_KEY.is_file() or not TLS_SERVER_CERT.is_file():
        return False
    verify = _openssl(
        "verify",
        "-CAfile",
        str(TLS_CA_CERT),
        str(TLS_SERVER_CERT),
        check=False,
    )
    if verify.returncode:
        return False
    checkend = _openssl(
        "x509",
        "-in",
        str(TLS_SERVER_CERT),
        "-checkend",
        "86400",
        "-noout",
        check=False,
    )
    if checkend.returncode:
        return False
    details = _openssl(
        "x509",
        "-in",
        str(TLS_SERVER_CERT),
        "-noout",
        "-text",
    ).stdout
    if f"IP Address:{address}" not in details:
        return False
    if f"DNS:{dns_name}" not in details:
        return False
    try:
        return (
            _public_key_from_certificate(TLS_SERVER_CERT)
            == _public_key_from_private_key(TLS_SERVER_KEY)
        )
    except OpenBaoHostError:
        return False


def _otp_tls_was_initialized(ssh_access_path: Path) -> bool:
    if not ssh_access_path.is_file() or ssh_access_path.stat().st_size == 0:
        return False
    try:
        payload = json.loads(ssh_access_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and "ssh-otp-config" in payload


def prepare_tls_material(
    *,
    address: str,
    dns_name: str,
    ssh_access_path: Path,
    log_detail,
) -> None:
    """Подготовить TLS CA на PVE и серверный комплект infra-manager."""
    TLS_PVE_ONLY_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_PVE_ONLY_DIR, 0o700)
    os.chown(TLS_PVE_ONLY_DIR, 0, 0)

    TLS_ACCESS_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(TLS_ACCESS_DIR, 0o755)
    os.chown(TLS_ACCESS_DIR, 100000, 100000)

    ca_parts = [TLS_CA_KEY.exists(), TLS_CA_CERT.exists()]
    if any(ca_parts) and not all(ca_parts):
        raise OpenBaoHostError(
            "TLS CA OpenBao найден частично; автоматическая замена запрещена"
        )

    if not any(ca_parts):
        if _otp_tls_was_initialized(ssh_access_path):
            raise OpenBaoHostError(
                "TLS CA OpenBao потерян после ввода OTP/TLS-схемы; "
                "автоматическая ротация центра доверия запрещена"
            )
        with tempfile.TemporaryDirectory(
            prefix=".openbao-ca.",
            dir=TLS_PVE_ONLY_DIR,
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            key = temporary / "ca.key"
            cert = temporary / "ca.crt"
            _openssl(
                "req",
                "-x509",
                "-newkey",
                "rsa:4096",
                "-nodes",
                "-sha256",
                "-days",
                "3650",
                "-subj",
                "/CN=infra-manager OpenBao TLS CA",
                "-addext",
                "basicConstraints=critical,CA:TRUE",
                "-addext",
                "keyUsage=critical,keyCertSign,cRLSign",
                "-keyout",
                str(key),
                "-out",
                str(cert),
            )
            _atomic_copy(key, TLS_CA_KEY, mode=0o600, uid=0, gid=0)
            _atomic_copy(cert, TLS_CA_CERT, mode=0o644, uid=0, gid=0)

    if not _tls_ca_ready():
        raise OpenBaoHostError(
            "Существующий TLS CA OpenBao повреждён или ключ не соответствует сертификату"
        )
    if not _tls_ca_has_signing_usage():
        _repair_legacy_tls_ca_certificate(log_detail)

    _atomic_copy(
        TLS_CA_CERT,
        TLS_ACCESS_CA_CERT,
        mode=0o644,
        uid=100000,
        gid=100000,
    )

    if not _tls_server_ready(address, dns_name):
        with tempfile.TemporaryDirectory(
            prefix=".openbao-server.",
            dir=TLS_PVE_ONLY_DIR,
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            key = temporary / "server.key"
            csr = temporary / "server.csr"
            cert = temporary / "server.crt"
            extensions = temporary / "server.ext"
            extensions.write_text(
                "[v3_req]\n"
                f"subjectAltName=IP:{address},DNS:{dns_name}\n"
                "extendedKeyUsage=serverAuth\n"
                "keyUsage=digitalSignature,keyEncipherment\n",
                encoding="utf-8",
            )
            _openssl(
                "req",
                "-new",
                "-newkey",
                "rsa:3072",
                "-nodes",
                "-sha256",
                "-subj",
                f"/CN={dns_name}",
                "-keyout",
                str(key),
                "-out",
                str(csr),
            )
            serial = "0x" + os.urandom(16).hex()
            _openssl(
                "x509",
                "-req",
                "-in",
                str(csr),
                "-CA",
                str(TLS_CA_CERT),
                "-CAkey",
                str(TLS_CA_KEY),
                "-set_serial",
                serial,
                "-days",
                "825",
                "-sha256",
                "-extfile",
                str(extensions),
                "-extensions",
                "v3_req",
                "-out",
                str(cert),
            )
            _atomic_copy(key, TLS_SERVER_KEY, mode=0o600, uid=100000, gid=100000)
            _atomic_copy(cert, TLS_SERVER_CERT, mode=0o644, uid=100000, gid=100000)

    if not _tls_server_ready(address, dns_name):
        raise OpenBaoHostError("TLS server material OpenBao не прошёл проверку")

    log_detail(
        "[ОК] TLS OpenBao подготовлен: CA остаётся на PVE, серверный комплект доступен infra-manager"
    )
