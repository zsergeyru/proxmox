#!/usr/bin/env python3
"""Проверить быстрый deploy 910 без Docker и root на CI runner."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/infra-manager"))
from infra_manager import self_update_fast as fast
from infra_manager.common import InfraManagerError
from infra_manager.operation_store import OperationStore


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def commit(repo: Path, filename: str, content: str) -> str:
    file = repo / filename
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")
    subprocess.check_call(["git", "-C", str(repo), "add", "--all"],
                          stdout=subprocess.DEVNULL)
    subprocess.check_call(["git", "-C", str(repo), "commit", "-qm", "test-change"])
    return git(repo, "rev-parse", "HEAD")


def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        repo = base / "repo"
        repo.mkdir()
        subprocess.check_call(["git", "-C", str(repo), "init", "-q"])
        subprocess.check_call(["git", "-C", str(repo), "config", "user.email", "ci@example.test"])
        subprocess.check_call(["git", "-C", str(repo), "config", "user.name", "CI"])
        webfile = fast.WEB + "admin.py"
        sysfile = fast.PY + "admin_socket.py"
        critical = "infrastructure/hosts/pve/backup.yaml"
        doc = "docs/update.md"
        initial = commit(repo, webfile, "print('web v1')\n")
        commit(repo, sysfile, "print('gateway v1')\n")
        commit(repo, critical, "backup: v1\n")
        version = git(repo, "rev-parse", "HEAD")

        dest = base / "installed"
        compose = dest / "compose"
        web_dest = compose / "web"
        py_dest = dest / "python" / "infra_manager"
        web_dest.mkdir(parents=True)
        py_dest.mkdir(parents=True)
        shutil.copy2(repo / webfile, web_dest / "admin.py")
        shutil.copy2(repo / sysfile, py_dest / "admin_socket.py")
        versions = compose / ".versions.env"
        versions.write_text(
            "INFRA_GIT_REVISION=" + version + "\n"
            "INFRA_WEB_CONTENT_REVISION="
            + git(repo, "rev-parse", "HEAD:" + fast.WEB.rstrip("/")) + "\n"
            "RUNTIME_MODE=standalone\n", encoding="utf-8",
        )
        (base / "state").mkdir()
        store = OperationStore(base / "state" / "operations", require_mount=False)

        # На CI выполняем те же IO, но не требуем root-UID на файловой системе runner.
        original_read = fast._read
        def test_read(path: Path, limit: int) -> bytes:
            if path.is_symlink():
                raise OSError("symlink forbidden")
            data = path.read_bytes()
            assert len(data) <= limit
            return data

        paths = SimpleNamespace(repo_root=repo, compose_dir=compose,
                                python_install_root=dest / "python")
        with (patch.object(fast, "PATHS", paths),
              patch.object(fast, "VERSIONS", versions),
              patch.object(fast, "_read", side_effect=test_read),
              patch.object(fast.os, "geteuid", return_value=0),
              patch.object(fast, "project_checkout_lock",
                           side_effect=lambda exclusive: contextlib.nullcontext())):
            missing = fast.plan(repo, store=store, versions=versions)
            assert missing.mode == "full" and missing.reason
            fast.attest(repo, store=store, versions=versions)
            assert fast.plan(repo, store=store, versions=versions).mode == "fast"
            assert fast.plan(repo, store=store, versions=versions).web == ()
            commit(repo, doc, "Only documentation\n")
            assert fast.plan(repo, store=store, versions=versions).mode == "fast"
            assert fast.plan(repo, store=store, versions=versions).web == ()
            commit(repo, webfile, "print('web v2')\n")
            commit(repo, sysfile, "print('gateway v2')\n")
            selected = fast.plan(repo, store=store, versions=versions)
            assert selected.mode == "fast" and selected.web == (webfile,)
            assert selected.python == (sysfile,)
            # Без запуска реальных Docker/systemd: тестируем точные действия.
            calls = []
            def fake_command(argv, title):
                calls.append(argv)
                if argv[:3] == ["docker", "image", "inspect"]:
                    return "sha256:" + "a" * 64
                return ""
            with (patch.object(fast, "_command", side_effect=fake_command),
                  patch.object(fast, "_compose", side_effect=lambda *a, **kw: calls.append(a))):
                fast.apply(selected, lambda: True, repo=repo, store=store)
            assert (web_dest / "admin.py").read_text() == "print('web v2')\n"
            assert (py_dest / "admin_socket.py").read_text() == "print('gateway v2')\n"
            assert any(item[:2] == ("build", "web") for item in calls)
            assert any(item[:2] == ("up", "-d") for item in calls)
            assert any(item[:2] == ["systemctl", "restart"] for item in calls)
            assert fast.plan(repo, store=store, versions=versions).mode == "fast"

            # Python-only update MUST NOT invoke Docker or rebuild runtime/web.
            commit(repo, sysfile, "print('gateway v3')\n")
            selected = fast.plan(repo, store=store, versions=versions)
            assert selected.mode == "fast" and not selected.web and selected.python
            calls.clear()
            with (patch.object(fast, "_command", side_effect=fake_command),
                  patch.object(fast, "_compose", side_effect=lambda *a, **kw: calls.append(a))):
                fast.apply(selected, lambda: True, repo=repo, store=store)
            assert not any("docker" in str(item) for item in calls)
            assert any(item[:2] == ["systemctl", "restart"] for item in calls)

            # При ошибке status данные и версия откатываются, baseline НЕ меняется.
            commit(repo, webfile, "print('bad web')\n")
            selected = fast.plan(repo, store=store, versions=versions)
            previous = (web_dest / "admin.py").read_bytes()
            previous_env = versions.read_bytes()
            calls.clear()
            with (patch.object(fast, "_command", side_effect=fake_command),
                  patch.object(fast, "_compose", side_effect=lambda *a, **kw: calls.append(a))):
                try:
                    fast.apply(selected, lambda: False, repo=repo, store=store)
                    raise AssertionError("Неуспешная проверка должна отклонить быстрый deploy")
                except InfraManagerError as exc:
                    assert "status 910" in str(exc)
            assert (web_dest / "admin.py").read_bytes() == previous
            assert versions.read_bytes() == previous_env
            assert fast.plan(repo, store=store, versions=versions).web == (webfile,)

            # Любая неизвестная или критическая правка => полный Ansible.
            commit(repo, critical, "backup: changed\n")
            assert fast.plan(repo, store=store, versions=versions).mode == "full"
            # Даже при прежнем revision в .versions.env чужой baseline не принимаем.
            versions.write_text(versions.read_text().replace(
                "INFRA_GIT_REVISION=", "INFRA_GIT_REVISION=not-valid-"
            ))
            assert fast.plan(repo, store=store, versions=versions).mode == "full"
    print("[ОК] Быстрый deploy: Git baseline, Python/web, откат, критические изменения")


if __name__ == "__main__":
    main()
