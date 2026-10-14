#!/usr/bin/env python3
"""Проверить состав Immich для первоначального запуска и Windows-работника."""

from __future__ import annotations

import copy
import subprocess
import tempfile
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


ROOT = Path(__file__).resolve().parents[2]
GUEST = ROOT / "infrastructure/guests/340-photo-archive/provision.yaml"
TEMPLATES = ROOT / "automation/ansible/roles/photo_archive/templates"


def test_compose() -> None:
    provision = yaml.safe_load(GUEST.read_text(encoding="utf-8"))
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=False,
    )
    template = env.get_template("docker-compose.yml.j2")
    assert provision["photo_archive"]["immich_version"] == "v3.3.1"
    assert provision["pve_storage"]["mounts"][0]["read_only"] is False
    assert provision["photo_archive"]["storage"]["originals_read_only"] is False
    assert provision["photo_archive"]["initial_setup"]["trash_days"] == 30

    for windows_worker in (False, True):
        description = copy.deepcopy(provision)
        description["photo_archive"]["remote_worker"]["enabled"] = windows_worker
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret = root / "immich.env"
            secret.write_text(
                "DB_PASSWORD=testtesttesttesttest\n"
                "POSTGRES_PASSWORD=testtesttesttesttest\n"
                "REDIS_PASSWORD=testtesttesttesttest\n"
                "DB_USERNAME=postgres\nDB_DATABASE_NAME=immich\n",
                encoding="utf-8",
            )
            description["photo_archive"]["paths"]["environment"] = str(secret)
            rendered = template.render(
                provision=description, ansible_host="192.168.3.40"
            )
            config = yaml.safe_load(rendered)
            services = config["services"]
            assert set(services) == {"immich-server", "redis", "database"}
            app = services["immich-server"]
            assert app["environment"]["IMMICH_WORKERS_INCLUDE"] == (
                "api" if windows_worker else "api,microservices"
            )
            assert app["image"].endswith(":v3.3.1")
            assert app["volumes"][0].endswith(":/data")
            assert app["volumes"][1].endswith(":/external/originals")
            assert not app["volumes"][1].endswith(":ro")
            assert services["database"]["volumes"][0].endswith(":/var/lib/postgresql/data")
            assert services["redis"]["command"][-1].count("$$REDIS_PASSWORD") == 1
            for service in ("database", "redis"):
                assert ("ports" in services[service]) is windows_worker
                if windows_worker:
                    assert services[service]["ports"][0].startswith("192.168.3.40:")
            # На машине с docker compose дополнительно проверить сам Compose.
            output = root / "compose.yml"
            output.write_text(rendered, encoding="utf-8")
            try:
                version = subprocess.run(
                    ["docker", "compose", "version"],
                    check=False, capture_output=True, text=True,
                )
            except FileNotFoundError:
                continue
            if version.returncode != 0:
                continue
            check = subprocess.run(
                ["docker", "compose", "-f", str(output), "config", "--quiet"],
                cwd=root, capture_output=True, text=True, check=False,
            )
            assert check.returncode == 0, check.stderr


if __name__ == "__main__":
    test_compose()
    print("OK: Immich compose template")
