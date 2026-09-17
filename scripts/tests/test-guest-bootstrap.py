#!/usr/bin/env python3
"""Контрактные проверки Guest Bootstrap v1 без доступа к PVE."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from guest_config import GuestConfigError, resolve_effective_guest  # noqa: E402


def load_yaml(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def expect_error(source: dict, defaults: dict, needle: str) -> None:
    try:
        resolve_effective_guest(source, defaults)
    except GuestConfigError as exc:
        assert needle in str(exc), (needle, str(exc))
    else:
        raise AssertionError(f"Expected GuestConfigError containing {needle!r}")


def main() -> None:
    defaults = load_yaml(ROOT / "guests/defaults.yaml")
    source_311 = load_yaml(ROOT / "guests/311-dev-services/guest.yaml")

    resolved = resolve_effective_guest(source_311, defaults)
    assert resolved.bootstrap_capabilities == (
        "base",
        "git",
        "docker",
        "ansible_controller",
    )

    no_bootstrap = copy.deepcopy(source_311)
    no_bootstrap.pop("bootstrap")
    assert resolve_effective_guest(no_bootstrap, defaults).bootstrap_capabilities == ()

    docker_without_base = copy.deepcopy(source_311)
    docker_without_base["bootstrap"] = {"capabilities": {"docker": True}}
    expect_error(docker_without_base, defaults, "требует явно включить: base")

    ansible_without_dependencies = copy.deepcopy(source_311)
    ansible_without_dependencies["bootstrap"] = {
        "capabilities": {"base": True, "ansible_controller": True}
    }
    expect_error(
        ansible_without_dependencies,
        defaults,
        "требует явно включить: git, docker",
    )

    all_disabled = copy.deepcopy(source_311)
    all_disabled["bootstrap"] = {
        "capabilities": {
            "base": False,
            "git": False,
            "docker": False,
            "ansible_controller": False,
        }
    }
    expect_error(all_disabled, defaults, "не включена ни одна capability")

    stopped = copy.deepcopy(source_311)
    stopped["boot"] = {"start_after_deploy": False}
    expect_error(stopped, defaults, "bootstrap требует boot.start_after_deploy=true")

    print("Guest Bootstrap v1 contract tests passed.")


if __name__ == "__main__":
    main()
