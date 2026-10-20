#!/usr/bin/env python3
"""Проверка выбора Docker-образов 910 без Docker, PVE и сетевых запросов."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "automation/ansible/roles/infra_manager/tasks/runtime.yml"
PROVISION = ROOT / "infrastructure/guests/910-infra-manager/provision.yaml"
COMPOSE_SOURCE = "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose"


def named(tasks: list[dict], name: str) -> dict:
    matched = [item for item in tasks if item.get("name") == name]
    assert len(matched) == 1, name
    return matched[0]


def text_sha(value: str, method: str) -> str:
    return hashlib.new(method, value.encode()).hexdigest()


def current_tree(relative: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", f"HEAD:{relative}"],
        text=True,
    ).strip()


def main() -> None:
    tasks = yaml.safe_load(ROLE.read_text(encoding="utf-8"))
    manifest = yaml.safe_load(PROVISION.read_text(encoding="utf-8"))
    runtime = manifest["docker"]["services"]["runtime"]
    source_revision_task = named(tasks, "Получить отпечатки исходных файлов Docker и OpenBao")
    argv = source_revision_task["ansible.builtin.command"]["argv"]
    assert argv[:4] == ["git", "-C", "{{ provision.paths.bootstrap_repository }}", "rev-parse"]
    resolved = [current_tree(item.removeprefix("HEAD:").replace("{{ guest }}", "910-infra-manager"))
                for item in argv[4:]]
    assert len(resolved) == 4 and all(re.fullmatch("[0-9a-f]{40}", x) for x in resolved)

    jinja = Environment(undefined=StrictUndefined)
    jinja.filters["hash"] = text_sha
    jinja.filters["from_json"] = json.loads
    facts = named(tasks, "Рассчитать версии содержимого независимо от Git-версии всего проекта")
    variables = {
        "infra_component_sources": {"stdout_lines": resolved},
        "provision": manifest,
    }
    actual = {
        key: jinja.from_string(template).render(**variables).strip()
        for key, template in facts["ansible.builtin.set_fact"].items()
    }
    expected_runtime = text_sha(
        resolved[1] + resolved[2] + runtime["image"]
        + runtime["tools"]["opentofu"] + runtime["tools"]["packer_image"],
        "sha1",
    )
    assert actual == {
        "infra_web_content_revision": resolved[0],
        "infra_runtime_content_revision": expected_runtime,
        "infra_openbao_config_revision": resolved[3],
    }

    choices = named(tasks, "Выбрать только отсутствующие или устаревшие Docker-образы")
    image_vars = {
        "infra_runtime_content_revision": actual["infra_runtime_content_revision"],
        "infra_web_content_revision": actual["infra_web_content_revision"],
    }

    def choose(rt_sha: str | None, web_sha: str | None) -> tuple[bool, bool]:
        rt_env = [] if rt_sha is None else ["INFRA_RUNTIME_BUILD_REVISION=" + rt_sha]
        web_env = [] if web_sha is None else ["INFRA_WEB_BUILD_REVISION=" + web_sha]
        values = {
            **image_vars,
            "infra_runtime_image_inspect": {
                "rc": int(rt_sha is None),
                "stdout": json.dumps([{"Config": {"Env": rt_env}}]),
            },
            "infra_web_image_inspect": {
                "rc": int(web_sha is None),
                "stdout": json.dumps([{"Config": {"Env": web_env}}]),
            },
        }
        return tuple(
            jinja.from_string(choices["ansible.builtin.set_fact"][name])
            .render(**values).strip() == "True"
            for name in ("infra_build_runtime", "infra_build_web")
        )

    rt = actual["infra_runtime_content_revision"]
    web = actual["infra_web_content_revision"]
    assert choose(rt, web) == (False, False), "Чистое повторение не должно собирать Docker"
    assert choose(rt, "0" * 40) == (False, True), "Только web обновился"
    assert choose("0" * 40, web) == (True, False), "Только runtime обновился"
    assert choose(None, web) == (True, False), "Отсутствующий runtime должен собраться"
    assert choose(rt, None) == (False, True), "Отсутствующий web должен собраться"

    for filename in ("docker-compose.yml", "docker-compose-standalone.yml"):
        compose = yaml.safe_load((ROOT / COMPOSE_SOURCE / filename).read_text(encoding="utf-8"))
        assert compose["services"]["openbao"]["environment"]["INFRA_OPENBAO_CONFIG_REVISION"] == (
            "${INFRA_OPENBAO_CONFIG_REVISION:-unknown}"
        )
    print("[ОК] Отпечатки web/runtime/OpenBao и выборочный план Docker подтверждены")


if __name__ == "__main__":
    main()
