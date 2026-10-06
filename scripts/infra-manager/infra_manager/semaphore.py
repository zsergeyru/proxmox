"""Работа с Semaphore API и декларативная настройка проекта."""

from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import LOG_LEVEL_ENV, LOG_LEVELS, InfraManagerError, console
from .guest_operations import operation_guests
from .pve_host import update_openbao_semaphore_api_token
from .settings import PATHS, SETTINGS

SEMAPHORE_URL = SETTINGS.semaphore_url
PROJECT_NAME = SETTINGS.project_name
OPENTOFU_ENV_NAME = SETTINGS.opentofu_env_name
INFRA_MANAGER_ENV_NAME = SETTINGS.infra_manager_env_name
PROJECT_ID_FILE = PATHS.semaphore_project_id_file
ADMIN_PASSWORD_FILE = PATHS.admin_password_file
SEMAPHORE_API_TOKEN_FILE = PATHS.semaphore_api_token_file
OPENBAO_MATERIALIZED_MARKER = PATHS.openbao_materialized_marker
PVE_API_ENV = PATHS.pve_api_env
GITHUB_KEY = PATHS.github_key
GITHUB_KEY_COPY = PATHS.github_key_copy
ANSIBLE_PUBLIC_KEY = PATHS.ansible_public_key
PROJECT_REPO = SETTINGS.project_repo


@dataclass(frozen=True)
class ViewSpec:
    """Декларативное описание группы шаблонов Semaphore."""

    title: str
    position: int


@dataclass(frozen=True)
class TemplateSpec:
    """Декларативное описание задания Semaphore."""

    name: str
    playbook: str
    arguments: str
    view: str
    app: str = "python"
    survey_vars: tuple[dict[str, Any], ...] = ()


def semaphore_views() -> tuple[ViewSpec, ...]:
    """Вернуть целевые группы шаблонов в порядке интерфейса."""

    return (
        ViewSpec(title="Guests", position=1),
        ViewSpec(title="Infrastructure", position=2),
        ViewSpec(title="Security", position=3),
    )


def _guest_survey(
    repo_root: Path,
    operation: str,
) -> tuple[dict[str, Any], ...]:
    """Сформировать безопасный список гостей для одной операции."""

    guests = operation_guests(repo_root, operation)
    values = [
        {
            "name": f"{guest.vmid} — {guest.name}",
            "value": str(guest.vmid),
        }
        for guest in guests
    ]
    if not values:
        raise InfraManagerError(
            f"Для операции {operation} не найдено поддерживаемых гостей"
        )
    return (
        {
            "name": "GUEST_VMID",
            "title": "Гость",
            "description": "Выберите гостя из машинного каталога проекта",
            "type": "enum",
            "required": True,
            "values": values,
        },
    )


def semaphore_templates(
    repo_root: Path | None = None,
) -> tuple[TemplateSpec, ...]:
    """Собрать компактный набор инфраструктурных заданий Semaphore."""

    root = repo_root or PATHS.repo_root

    return (
        TemplateSpec(
            name="OpenTofu Plan",
            playbook="scripts/infra-manager/jobs/opentofu-plan.py",
            arguments="[]",
            view="Infrastructure",
        ),
        TemplateSpec(
            name="Build Template 9000",
            playbook="scripts/infra-manager/jobs/build-template.py",
            arguments='["9000"]',
            view="Infrastructure",
        ),
        TemplateSpec(
            name="Deploy Guest",
            playbook="scripts/infra-manager/jobs/guest-operation.py",
            arguments='["deploy"]',
            view="Guests",
            survey_vars=_guest_survey(root, "deploy"),
        ),
        TemplateSpec(
            name="Status Guest",
            playbook="scripts/infra-manager/jobs/guest-operation.py",
            arguments='["status"]',
            view="Guests",
            survey_vars=_guest_survey(root, "status"),
        ),
        TemplateSpec(
            name="Repair Guest",
            playbook="scripts/infra-manager/jobs/guest-operation.py",
            arguments='["repair"]',
            view="Guests",
            survey_vars=_guest_survey(root, "repair"),
        ),
        TemplateSpec(
            name="Test Guest",
            playbook="scripts/infra-manager/jobs/guest-operation.py",
            arguments='["test"]',
            view="Guests",
            survey_vars=_guest_survey(root, "test"),
        ),
        TemplateSpec(
            name="Sync Guest",
            playbook="scripts/infra-manager/jobs/guest-operation.py",
            arguments='["sync"]',
            view="Guests",
            survey_vars=_guest_survey(root, "sync"),
        ),
        TemplateSpec(
            name="Sync SSH Access",
            playbook="scripts/infra-manager/jobs/sync-ssh-access.py",
            arguments="[]",
            view="Security",
        ),
    )


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


def find_unique_by_name(
    items: Any,
    name: str,
    kind: str,
) -> dict[str, Any] | None:
    """Найти единственный именованный объект или вернуть None."""

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


def find_unique_by_title(
    items: Any,
    title: str,
    kind: str,
) -> dict[str, Any] | None:
    """Найти единственный объект Semaphore по полю title."""

    if not isinstance(items, list):
        raise InfraManagerError(
            f"Не удалось проверить объекты Semaphore: {kind}"
        )
    matches = [
        item
        for item in items
        if isinstance(item, dict) and item.get("title") == title
    ]
    if len(matches) > 1:
        raise InfraManagerError(
            f"В Semaphore найдено несколько объектов {kind} "
            f"с названием '{title}'"
        )
    return matches[0] if matches else None


def require_unique_by_name(
    items: Any,
    name: str,
    kind: str,
) -> dict[str, Any]:
    """Вернуть единственный именованный объект или сообщить об отсутствии."""

    item = find_unique_by_name(items, name, kind)
    if item is None:
        raise InfraManagerError(
            f"В Semaphore отсутствует {kind} '{name}'"
        )
    return item


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

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

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

    def _sync_api_token_to_openbao(self) -> None:
        if not OPENBAO_MATERIALIZED_MARKER.is_file():
            return
        if not nonempty(SEMAPHORE_API_TOKEN_FILE):
            raise InfraManagerError("Отсутствует API token Semaphore для OpenBao")
        values = read_env_file(PVE_API_ENV)
        endpoint = values.get("PVE_API_URL", "")
        node = urllib.parse.urlparse(endpoint).hostname if endpoint else None
        if not node:
            raise InfraManagerError(
                "Не удалось определить узел PVE для обновления OpenBao"
            )
        token = SEMAPHORE_API_TOKEN_FILE.read_text(
            encoding="utf-8"
        ).strip()
        update_openbao_semaphore_api_token(node, token)

    def ensure_api_token(self) -> None:
        if self.token_valid():
            self.auth_mode = "token"
            self._sync_api_token_to_openbao()
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
        self._sync_api_token_to_openbao()

    def ensure_project(self) -> int:
        existing = find_unique_by_name(
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
        existing = find_unique_by_name(keys, name, "SSH key")
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
        existing = find_unique_by_name(
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
        existing = find_unique_by_name(
            environments,
            OPENTOFU_ENV_NAME,
            "Variable Group",
        )

        if not nonempty(ANSIBLE_PUBLIC_KEY):
            raise InfraManagerError(
                f"Не найден открытый ключ Ansible: {ANSIBLE_PUBLIC_KEY}"
            )
        ansible_public_key = ANSIBLE_PUBLIC_KEY.read_text(
            encoding="utf-8"
        ).strip()

        env_json = json.dumps(
            {
                "TF_VAR_pve_endpoint": endpoint,
                "TF_VAR_ansible_ssh_public_key": ansible_public_key,
            },
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

    def ensure_infra_manager_environment(self, project_id: int) -> int:
        """Создать общие настройки заданий и сохранить ручной выбор уровня вывода."""
        environments = self.get(
            f"/project/{project_id}/environment?sort=name&order=asc"
        )
        existing = find_unique_by_name(
            environments,
            INFRA_MANAGER_ENV_NAME,
            "Variable Group",
        )

        if existing is None:
            response = self.post(
                f"/project/{project_id}/environment",
                {
                    "name": INFRA_MANAGER_ENV_NAME,
                    "project_id": project_id,
                    "password": None,
                    "json": "{}",
                    "env": json.dumps(
                        {LOG_LEVEL_ENV: "normal"},
                        separators=(",", ":"),
                    ),
                    "secrets": [],
                },
            )
            environment_id = (
                response.get("id")
                if isinstance(response, dict)
                else None
            )
            if not isinstance(environment_id, int):
                raise InfraManagerError(
                    "Semaphore не вернул id Variable Group Infra Manager"
                )
            return environment_id

        environment_id = existing.get("id")
        if not isinstance(environment_id, int):
            raise InfraManagerError(
                "Variable Group Infra Manager имеет некорректный id"
            )

        full = self.get(
            f"/project/{project_id}/environment/{environment_id}"
        )
        raw_env = full.get("env", "{}") if isinstance(full, dict) else "{}"
        if isinstance(raw_env, str):
            try:
                values = json.loads(raw_env or "{}")
            except json.JSONDecodeError as exc:
                raise InfraManagerError(
                    "Variable Group Infra Manager содержит некорректный JSON"
                ) from exc
        elif isinstance(raw_env, dict):
            values = dict(raw_env)
        else:
            raise InfraManagerError(
                "Variable Group Infra Manager имеет некорректный env"
            )
        if not isinstance(values, dict):
            raise InfraManagerError(
                "Variable Group Infra Manager должен содержать объект env"
            )

        level = values.get(LOG_LEVEL_ENV)
        if level is None:
            values[LOG_LEVEL_ENV] = "normal"
            self.put(
                f"/project/{project_id}/environment/{environment_id}",
                {
                    "id": environment_id,
                    "name": INFRA_MANAGER_ENV_NAME,
                    "project_id": project_id,
                    "password": None,
                    "json": "{}",
                    "env": json.dumps(values, separators=(",", ":")),
                    "secrets": [],
                },
            )
        elif level not in LOG_LEVELS:
            allowed = ", ".join(sorted(LOG_LEVELS))
            raise InfraManagerError(
                f"{INFRA_MANAGER_ENV_NAME}: {LOG_LEVEL_ENV} "
                f"должен быть одним из значений: {allowed}"
            )

        return environment_id

    def ensure_view(
        self,
        project_id: int,
        *,
        title: str,
        position: int,
    ) -> int:
        """Создать или привести к целевому состоянию группу шаблонов."""

        views = self.get(f"/project/{project_id}/views")
        existing = find_unique_by_title(views, title, "View")
        payload = {
            "project_id": project_id,
            "title": title,
            "position": position,
        }
        if existing is None:
            response = self.post(
                f"/project/{project_id}/views",
                payload,
            )
            view_id = response.get("id") if isinstance(response, dict) else None
        else:
            view_id = existing.get("id")
            if not isinstance(view_id, int):
                raise InfraManagerError(
                    f"Semaphore View '{title}' имеет некорректный id"
                )
            self.put(
                f"/project/{project_id}/views/{view_id}",
                {"id": view_id, **payload},
            )

        if not isinstance(view_id, int):
            raise InfraManagerError(
                f"Semaphore не вернул id View '{title}'"
            )
        return view_id

    def ensure_template(
        self,
        project_id: int,
        repository_id: int,
        environment_ids: list[int],
        *,
        name: str,
        playbook: str,
        branch: str,
        arguments: str,
        view_id: int,
        app: str = "python",
        survey_vars: tuple[dict[str, Any], ...] = (),
    ) -> int:
        templates = self.get(
            f"/project/{project_id}/templates?sort=name&order=asc"
        )
        existing = find_unique_by_name(templates, name, "template")
        payload = {
            "name": name,
            "project_id": project_id,
            "repository_id": repository_id,
            "environment_ids": environment_ids,
            "view_id": view_id,
            "playbook": playbook,
            "app": app,
            "type": "",
            "git_branch": branch,
            "arguments": arguments,
            "survey_vars": [dict(item) for item in survey_vars],
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

    def remove_obsolete_managed_templates(
        self,
        project_id: int,
        expected_names: set[str],
    ) -> None:
        """Удалить старые шаблоны Deploy Guest N и Initialize OpenBao N."""

        templates = self.get(
            f"/project/{project_id}/templates?sort=name&order=asc"
        )
        if not isinstance(templates, list):
            raise InfraManagerError(
                "Semaphore вернул некорректный список шаблонов"
            )
        legacy_name = re.compile(
            r"^(?:Deploy Guest|Initialize OpenBao) [0-9]+$"
        )
        for template in templates:
            if not isinstance(template, dict):
                raise InfraManagerError(
                    "Semaphore вернул некорректный объект шаблона"
                )
            name = template.get("name")
            if not isinstance(name, str) or not legacy_name.fullmatch(name):
                continue
            if name in expected_names:
                continue
            template_id = template.get("id")
            if not isinstance(template_id, int):
                raise InfraManagerError(
                    f"Устаревший шаблон Semaphore '{name}' имеет неверный id"
                )
            self.delete(
                f"/project/{project_id}/templates/{template_id}"
            )


def persist_github_key() -> None:
    if not nonempty(GITHUB_KEY):
        raise InfraManagerError(
            f"GitHub Deploy Key не найден: {GITHUB_KEY}"
        )
    if GITHUB_KEY_COPY != GITHUB_KEY:
        raise InfraManagerError(
            "GitHub Deploy Key не должен иметь постоянную копию внутри infra-manager"
        )


def _require_existing_task_git(
    client: SemaphoreClient,
    project_id: int,
    branch: str,
) -> tuple[int, int]:
    """Проверить существующий Git-контур без изменения SSH-ключа."""

    key = require_unique_by_name(
        client.get(f"/project/{project_id}/keys?sort=name&order=asc"),
        "GitHub project read-only",
        "SSH key",
    )
    key_id = key.get("id")
    if not isinstance(key_id, int):
        raise InfraManagerError(
            "GitHub project read-only имеет некорректный id; "
            "выполните Repair Guest для infra-manager"
        )

    repository = require_unique_by_name(
        client.get(
            f"/project/{project_id}/repositories?sort=name&order=asc"
        ),
        "proxmox",
        "Git repository",
    )
    repository_id = repository.get("id")
    if not isinstance(repository_id, int):
        raise InfraManagerError(
            "Git repository proxmox имеет некорректный id; "
            "выполните Repair Guest для infra-manager"
        )
    if (
        repository.get("git_url") != PROJECT_REPO
        or repository.get("ssh_key_id") != key_id
    ):
        raise InfraManagerError(
            "Git-контур Semaphore не соответствует проекту; "
            "выполните Repair Guest для infra-manager"
        )

    if repository.get("git_branch") != branch:
        client.put(
            f"/project/{project_id}/repositories/{repository_id}",
            {
                "id": repository_id,
                "name": "proxmox",
                "project_id": project_id,
                "git_url": PROJECT_REPO,
                "git_branch": branch,
                "ssh_key_id": key_id,
            },
        )

    return key_id, repository_id


def _sync_project_objects(
    client: SemaphoreClient,
    project_id: int,
    branch: str,
    repo_root: Path,
    *,
    reuse_existing_git: bool = False,
) -> None:
    """Синхронизировать управляемые объекты внутри существующего проекта."""

    if reuse_existing_git:
        github_key_id, repository_id = _require_existing_task_git(
            client,
            project_id,
            branch,
        )
    else:
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
    opentofu_environment_id = client.ensure_opentofu_environment(project_id)
    infra_manager_environment_id = client.ensure_infra_manager_environment(
        project_id
    )
    environment_ids = [
        opentofu_environment_id,
        infra_manager_environment_id,
    ]
    views = semaphore_views()
    view_ids = {
        view.title: client.ensure_view(
            project_id,
            title=view.title,
            position=view.position,
        )
        for view in views
    }

    templates = semaphore_templates(repo_root)
    for template in templates:
        client.ensure_template(
            project_id,
            repository_id,
            environment_ids,
            name=template.name,
            playbook=template.playbook,
            branch=branch,
            arguments=template.arguments,
            view_id=view_ids[template.view],
            app=template.app,
            survey_vars=template.survey_vars,
        )
    client.remove_obsolete_managed_templates(
        project_id,
        {template.name for template in templates},
    )


def configure_project(branch: str | None = None) -> int:
    """Полностью синхронизировать Semaphore из доверенного root-контура."""

    if os.geteuid() != 0:
        raise InfraManagerError(
            "Настройка Semaphore должна выполняться от root"
        )
    if not nonempty(PVE_API_ENV):
        raise InfraManagerError(
            "Не найден постоянный PVE API credential"
        )

    branch = branch or SETTINGS.project_branch()
    persist_github_key()

    client = SemaphoreClient()
    client.ensure_api_token()
    project_id = client.ensure_project()
    _sync_project_objects(
        client,
        project_id,
        branch,
        PATHS.repo_root,
    )

    console.ok(
        "Проект Semaphore, общие настройки и инфраструктурные задания подготовлены"
    )
    return 0


def sync_project_from_task(
    branch: str | None = None,
    repo_root: Path | None = None,
) -> int:
    """Обновить проект из задания Semaphore без root и ротации секретов."""

    if not nonempty(PVE_API_ENV):
        raise InfraManagerError(
            "Не найден рабочий PVE API credential"
        )

    branch = branch or SETTINGS.project_branch()
    persist_github_key()

    client = SemaphoreClient()
    if not client.token_valid():
        raise InfraManagerError(
            "Рабочий API token Semaphore отсутствует или недействителен; "
            "выполните Repair Guest для infra-manager"
        )
    client.auth_mode = "token"

    project = require_unique_by_name(
        client.get("/projects"),
        PROJECT_NAME,
        "project",
    )
    project_id = project.get("id")
    if not isinstance(project_id, int):
        raise InfraManagerError(
            "Проект Semaphore имеет некорректный id"
        )

    _sync_project_objects(
        client,
        project_id,
        branch,
        repo_root or PATHS.repo_root,
        reuse_existing_git=True,
    )
    console.ok(
        "Проект Semaphore синхронизирован без изменения root-секретов"
    )
    return 0
