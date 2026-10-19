"""Изолированные PVE-only команды резервирования и подготовки носителей."""

from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path("/usr/local/lib/infra-manager/backup")
OPENBAO_HELPER = Path("/usr/local/sbin/infra-manager-openbao-unseal")

SCRIPTS = {
    "inspect": ("backup-device.py", "inspect"),
    "plan-ssd": ("backup-device.py", "plan"),
    "prepare-ssd": ("backup-device.py", "prepare"),
    "mount-ssd": ("backup-mount.py", "fast"),
    "mount-nas": ("backup-mount.py", "nas"),
    "init": ("backup-setup.py", "init"),
    "status": ("backup-setup.py", "status"),
    "enable": ("backup-setup.py", "enable"),
    "disable": ("backup-setup.py", "disable"),
    "export-key": ("backup-setup.py", "export-key"),
    "run": ("backup-pve.py", "--run"),
}


def dispatch_backup(args: list[str], error_type: type[Exception]) -> int:
    """Пропускать только явно объявленные команды на физическом PVE."""

    if not args:
        raise error_type(
            "Команды: inspect, plan-ssd, prepare-ssd, mount-ssd, mount-nas, "
            "openbao-access, init, run, status, enable, disable, export-key, verify"
        )
    if args[0] == "openbao-access":
        if len(args) != 1:
            raise error_type("openbao-access не принимает дополнительных аргументов")
        argv = [str(OPENBAO_HELPER), "--configure-backup-access"]
    elif args[0] == "verify":
        argv = ["python3", str(ROOT / "backup-restore.py"), *args[1:]]
    else:
        spec = SCRIPTS.get(args[0])
        if spec is None:
            raise error_type("Неизвестная команда резервирования")
        name, action = spec
        script = ROOT / name
        if not script.is_file() or script.is_symlink():
            raise error_type(f"Программа резервирования не установлена: {script}")
        argv = ["python3", str(script)]
        if action.startswith("--"):
            argv.append(action)
        else:
            argv.append(action)
        argv.extend(args[1:])
    return subprocess.run(argv, check=False).returncode
