#!/usr/bin/env python3
"""Ошибки Homarr классифицируются, но содержимое журнала не раскрывается."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import contextlib
import io

ROOT = Path(__file__).resolve().parents[2]
FILE = ROOT / "automation/ansible/roles/web_gateway/files/homarr-diagnostics.py"


def test():
    spec = importlib.util.spec_from_file_location("homarr_diagnostics_140", FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    raw = (
        "ERROR request failed token=private-secret-123\n"
        "ERROR econnrefused http://db:5432 user=anna@example.org\n"
        "ERROR custom-widget invalid integration key=another-secret\n"
    )
    messages = mod.summarize(raw)
    joined = "\n".join(messages)
    assert "подключение" in joined and "виджеты" in joined and "прочее" in joined
    assert "private-secret" not in joined and "anna@example" not in joined
    assert mod.summarize("INFO ready\n") == []
    with patch.object(mod.subprocess, "run", return_value=SimpleNamespace(
        returncode=0, stdout=raw, stderr=""
    )):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            assert mod.main() == 0
        assert "private-secret" not in output.getvalue()
    print("140: диагностика Homarr не раскрывает содержимое журналов")


if __name__ == "__main__":
    test()
