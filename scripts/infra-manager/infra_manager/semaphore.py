"""Работа с Semaphore API и декларативная настройка проекта."""

from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .common import InfraManagerError, console

SEMAPHORE_URL = "http://127.0.0.1:3000"
PROJECT_NAME = "Proxmox Infrastructure"
OPENTOFU_ENV_NAME = "OpenTofu PVE"
PROJECT_ID_FILE = Path("/var/lib/infra-manager/semaphore-project-id")

SECRET_DIR = Path("/etc/infra-manager/secrets")
ADMIN_PASSWORD_FILE = SECRET_DIR / "initial-admin-password"
SEMAPHORE_API_TOKEN_FILE = SECRET_DIR / "semaphore-api-token"
PVE_API_ENV = SECRET_DIR / "pve-api.env"
GITHUB_KEY = Path("/root/.ssh/github_proxmox_repo_ed25519")
GITHUB_KEY_COPY = SECRET_DIR / "github_project_ed25519"

PROJECT_REPO = "git@github.com:zsergeyru/proxmox.git"


def nonempty(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def read_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key] = value
    return result


class SemaphoreClient:
    def __init__(self, base_url: str = SEMAPHORE_URL) -> None:
        self.base_url = base_url.rstrip("/")
        self.cookie_jar = http.cookiejar.CookieJar()
        self.cookie_opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookie_jar)
        )
        self.auth_mode = "cookie"

    @staticmethod
    def _decode_json(body: bytes, context: str) -> Any:
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InfraManagerError(
                f"{context}: Semaphore вернул некорректный JSON"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        payload: Any | None = None,
        *,
        auth: str | None = None,
        timeout: int = 20,
    ) -> Any:
        body = None
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")

        mode = auth or self.auth_mode
        opener = urllib.request.build_opener()
        if mode == "token":
            if not nonempty(SEMAPHORE_API_TOKEN_FILE):
                raise InfraManagerError(
                    f"Не найден Semaphore API token: {SEMAPHORE_API_TOKEN_FILE}"
                )
            token = SEMAPHORE_API_TOKEN_FILE.read_text(
                encoding="utf-8"
            ).strip()
            headers["Authorization"] = f"Bearer {token}"
        elif mode == "cookie":
            opener = self.cookie_opener
        elif mode != "none":
            raise ValueError(f"Неизвестный режим Semaphore auth: {mode}")

        request = urllib.request.Request(
            f"{self.base_url}/api{path}",
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace").strip()
            suffix = error_body or "(пустой ответ)"
            raise InfraManagerError(
                f"Semaphore API {method} {path} вернул HTTP "
                f"{exc.code}: {suffix}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InfraManagerError(
                f"Semaphore API {method} {path} недоступен: {exc}"
            ) from exc

        return self._decode_json(raw, f"Semaphore API {method} {path}")

    def get(self, path: str, *, auth: str | None = None) -> Any:
        return self._request("GET", path, auth=auth)

    def post(
        self,
        path: str,
        payload: Any,
        *,
        auth: str | None = None,
    ) -> Any:
        return self._request("POST", path, payload, auth=auth)

    def put(self, path: str, payload: Any) -> Any:
        return self._request("PUT", path, payload)

    def ping(self) -> None:
        request = urllib.request.Request(
            f"{self.base_url}/api/ping",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=10):
                return
        except urllib.error.HTTPError as exc:
            raise InfraManagerError(
                f"Semaphore API ping вернул HTTP {exc.code}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise InfraManagerError(
                f"Semaphore API ping недоступен: {exc}"
            ) from exc

    def login(self) -> None:
        if not nonempty(ADMIN_PASSWORD_FILE):
            raise InfraManagerError("Не найден первичный пароль Semaphore")
        password = ADMIN_PASSWORD_FILE.read_text(
            encoding="utf-8"
        ).strip()
        self.post(
            "/auth/login",
            {"auth": "admin", "password": password},
            auth="cookie",
        )
        self.auth_mode = "cookie"

    def token_valid(self) -> bool:
        if not nonempty(SEMAPHORE_API_TOKEN_FILE):
            return False
        try:
            self.get("/user/", auth="token")
        except InfraManagerError:
            return False
        return True

    def ensure_api_token(self) -> None:
        if self.token_valid():
            self.auth_mode = "token"
            return

        self.login()
        response = self.post(
            "/user/tokens",
            {"name": "infra-manager setup"},
            auth="cookie",
        )
        token = response.get("id") if isinstance(response, dict) else None
        if not token:
            raise InfraManagerError("Semaphore не вернул API token")

        SEMAPHORE_API_TOKEN_FILE.write_text(
            f"{token}\n",
            encoding="utf-8",
        )
        SEMAPHORE_API_TOKEN_FILE.chmod(0o600)
        self.auth_mode = "token"
        self.get("/user/", auth="token")

    @staticmethod
    def unique_by_name(
        items: Any,
        name: str,
        kind: str,
    ) -> dict[str, Any] | None:
        if not isinstance(items, list):
            raise InfraManagerError(
                f"Не удалось проверить объекты Semaphore: {kind}"
            )
        matches = [
            item
            for item in items
            if isinstance(item, dict) and item.get("name") == name
        ]
        if len(matches) > 1:
            raise InfraManagerError(
                f"В Semaphore найдено несколько объектов {kind} "
                f"с именем '{name}'"
            )
        return matches[0] if matches else None

    def ensure_project(self) -> int:
        existing = self.unique_by_name(
            self.get("/projects"),
            PROJECT_NAME,
            "project",
        )
        if existing is None:
            existing = self.post(
                "/projects",
                {
                    "name": PROJECT_NAME,
                    "alert": False,
                    "max_parallel_tasks": 1,
                    "demo": False,
                },
            )
        project_id = (
            existing.get("id") if isinstance(existing, dict) else None
        )
        if not isinstance(project_id, int):
            raise InfraManagerError(
                "Semaphore создал проект, но не вернул id"
            )
        PROJECT_ID_FILE.write_text(
            f"{project_id}\n",
            encoding="utf-8",
        )
        PROJECT_ID_FILE.chmod(0o640)
        return project_id

    def ensure_ssh_key(
        self,
        project_id: int,
        name: str,
        login: str,
        private_key_file: Path,
    ) -> int:
        keys = self.get(
            f"/project/{project_id}/keys?sort=name&order=asc"
        )
        existing = self.unique_by_name(keys, name, "SSH key")
        private_key = private_key_file.read_text(encoding="utf-8")

        if existing is None:
            response = self.post(
                f"/project/{project_id}/keys",
                {
                    "name": name,
                    "type": "ssh",
                    "project_id": project_id,
                    "ssh": {
                        "login": login,
                        "passphrase": "",
                        "private_key": private_key,
                    },
                },
            )
            key_id = (
                response.get("id")
                if isinstance(response, dict)
                else None
            )
        else:
            key_id = existing.get("id")
            if not isinstance(key_id, int):
                raise InfraManagerError(
                    f"Semaphore SSH key '{name}' имеет некорректный id"
                )
            self.put(
                f"/project/{project_id}/keys/{key_id}",
                {
                    "id": key_id,
                    "name": name,
                    "type": "ssh",
                    "project_id": project_id,
                    "override_secret": True,
                    "ssh": {
                        "login": login,
                        "passphrase": "",
                        "private_key": private_key,
                    },
                },
            )

        if not isinstance(key_id, int):
            raise InfraManagerError(
                f"Не удалось создать SSH key '{name}' в Semaphore"
            )
        return key_id

    def ensure_repository(
        self,
        project_id: int,
        ssh_key_id: int,
        branch: str,
    ) -> int:
        repositories = self.get(
            f"/project/{project_id}/repositories?sort=name&order=asc"
        )
        existing = self.unique_by_name(
            repositories,
            "proxmox",
            "Git repository",
        )

        payload = {
            "name": "proxmox",
            "project_id": project_id,
            "git_url": PROJECT_REPO,
            "git_branch": branch,
            "ssh_key_id": ssh_key_id,
        }
        if existing is None:
            response = self.post(
                f"/project/{project_id}/repositories",
                payload,
            )
            repository_id = (
                response.get("id")
                if isinstance(response, dict)
                else None
            )
        else:
            repository_id = existing.get("id")
            if not isinstance(repository_id, int):
                raise InfraManagerError(
                    "Git repository Semaphore имеет некорректный id"
                )
            self.put(
                f"/project/{project_id}/repositories/{repository_id}",
                {"id": repository_id, **payload},
            )

        if not isinstance(repository_id, int):
            raise InfraManagerError(
                "Semaphore не вернул id Git repository"
            )
        return repository_id

    def ensure_opentofu_environment(self, project_id: int) -> int:
        if not nonempty(PVE_API_ENV):
            raise InfraManagerError(
                "Не найден постоянный PVE API credential"
            )
        values = read_env_file(PVE_API_ENV)
        endpoint = values.get("PVE_API_URL", "")
        token_id = values.get("PVE_API_TOKEN_ID", "")
        token_secret = values.get("PVE_API_TOKEN_SECRET", "")
        if not endpoint or not token_id or not token_secret:
            raise InfraManagerError(
                "PVE API credential неполон для OpenTofu"
            )

        environments = self.get(
            f"/project/{project_id}/environment?sort=name&order=asc"
        )
        existing = self.unique_by_name(
            environments,
            OPENTOFU_ENV_NAME,
            "Variable Group",
        )

        env_json = json.dumps(
            {"TF_VAR_pve_endpoint": endpoint},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        api_token = f"{token_id}={token_secret}"

        if existing is None:
            response = self.post(
                f"/project/{project_id}/environment",
                {
                    "name": OPENTOFU_ENV_NAME,
                    "project_id": project_id,
                    "password": None,
                    "json": "{}",
                    "env": env_json,
                    "secrets": [
                        {
                            "name": "TF_VAR_pve_api_token",
                            "secret": api_token,
                            "type": "env",
                            "operation": "create",
                        }
                    ],
                },
            )
            environment_id = (
                response.get("id")
                if isinstance(response, dict)
                else None
            )
            if not isinstance(environment_id, int):
                raise InfraManagerError(
                    "Semaphore не вернул id Variable Group OpenTofu"
                )
            return environment_id

        environment_id = existing.get("id")
        if not isinstance(environment_id, int):
            raise InfraManagerError(
                "Variable Group OpenTofu имеет некорректный id"
            )
        full = self.get(
            f"/project/{project_id}/environment/{environment_id}"
        )
        secrets = (
            full.get("secrets", [])
            if isinstance(full, dict)
            else []
        )
        secret_matches = [
            secret
            for secret in secrets
            if isinstance(secret, dict)
            and secret.get("name") == "TF_VAR_pve_api_token"
            and secret.get("type") == "env"
        ]
        if len(secret_matches) > 1:
            raise InfraManagerError(
                "Variable Group содержит несколько "
                "TF_VAR_pve_api_token"
            )

        secret_payload: dict[str, Any] = {
            "name": "TF_VAR_pve_api_token",
            "secret": api_token,
            "type": "env",
            "operation": "create",
        }
        if secret_matches:
            secret_id = secret_matches[0].get("id")
            if secret_id is not None:
                secret_payload["id"] = secret_id
            secret_payload["operation"] = "update"

        self.put(
            f"/project/{project_id}/environment/{environment_id}",
            {
                "id": environment_id,
                "name": OPENTOFU_ENV_NAME,
                "project_id": project_id,
                "password": None,
                "json": "{}",
                "env": env_json,
                "secrets": [secret_payload],
            },
        )
        return environment_id

    def ensure_template(
        self,
        project_id: int,
        repository_id: int,
        environment_id: int,
        *,
        name: str,
        playbook: str,
        branch: str,
        arguments: str,
    ) -> int:
        templates = self.get(
            f"/project/{project_id}/templates?sort=name&order=asc"
        )
        existing = self.unique_by_name(templates, name, "template")
        payload = {
            "name": name,
            "project_id": project_id,
            "repository_id": repository_id,
            "environment_ids": [environment_id],
            "playbook": playbook,
            "app": "bash",
            "type": "",
            "git_branch": branch,
            "arguments": arguments,
            "allow_override_args_in_task": False,
            "allow_override_branch_in_task": False,
        }
        if existing is None:
            response = self.post(
                f"/project/{project_id}/templates",
                payload,
            )
            template_id = (
                response.get("id")
                if isinstance(response, dict)
                else None
            )
        else:
            template_id = existing.get("id")
            if not isinstance(template_id, int):
                raise InfraManagerError(
                    f"Semaphore template '{name}' имеет некорректный id"
                )
            self.put(
                f"/project/{project_id}/templates/{template_id}",
                {"id": template_id, **payload},
            )

        if not isinstance(template_id, int):
            raise InfraManagerError(
                f"Semaphore не вернул id шаблона {name}"
            )
        return template_id


def persist_github_key() -> None:
    if not nonempty(GITHUB_KEY):
        raise InfraManagerError(
            f"GitHub Deploy Key не найден: {GITHUB_KEY}"
        )
    if (
        not GITHUB_KEY_COPY.is_file()
        or GITHUB_KEY.read_bytes() != GITHUB_KEY_COPY.read_bytes()
    ):
        shutil.copyfile(GITHUB_KEY, GITHUB_KEY_COPY)
        os.chown(GITHUB_KEY_COPY, 0, 0)
        GITHUB_KEY_COPY.chmod(0o600)
        console.ok(
            "GitHub Deploy Key синхронизирован с постоянным ключом PVE"
        )


def configure_project(branch: str | None = None) -> int:
    if os.geteuid() != 0:
        raise InfraManagerError(
            "Настройка Semaphore должна выполняться от root"
        )
    if not nonempty(PVE_API_ENV):
        raise InfraManagerError(
            "Не найден постоянный PVE API credential"
        )

    branch = branch or os.environ.get("INFRA_PROJECT_BRANCH", "main")
    persist_github_key()

    client = SemaphoreClient()
    client.ensure_api_token()
    project_id = client.ensure_project()
    github_key_id = client.ensure_ssh_key(
        project_id,
        "GitHub project read-only",
        "git",
        GITHUB_KEY_COPY,
    )
    repository_id = client.ensure_repository(
        project_id,
        github_key_id,
        branch,
    )
    environment_id = client.ensure_opentofu_environment(project_id)
    client.ensure_template(
        project_id,
        repository_id,
        environment_id,
        name="OpenTofu Plan",
        playbook="scripts/infra-manager/jobs/opentofu-plan.sh",
        branch=branch,
        arguments="[]",
    )
    client.ensure_template(
        project_id,
        repository_id,
        environment_id,
        name="Build Template 9000",
        playbook="scripts/infra-manager/jobs/build-template.sh",
        branch=branch,
        arguments='["9000"]',
    )

    console.ok(
        "Проект Semaphore, Git repository, PVE Variable Group "
        "и инфраструктурные задания подготовлены"
    )
    return 0
