#!/usr/bin/env python3
from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "infra-manager"))

module = importlib.import_module("infra_manager.pve_host")


def main() -> None:
    parsed = module._features_from_config(
        "hostname: demo\nfeatures: nesting=1,keyctl=1\n"
    )
    assert parsed == {"nesting": "1", "keyctl": "1"}

    parsed = module._features_from_config(
        "features: fuse=1,nesting=1\n"
    )
    parsed["keyctl"] = "1"
    rendered = module._render_features(parsed)
    assert rendered == "fuse=1,keyctl=1,nesting=1"

    assert module._features_from_config("hostname: demo\n") == {}
    print("[ОК] Проверки pve_host.py пройдены")


if __name__ == "__main__":
    main()
