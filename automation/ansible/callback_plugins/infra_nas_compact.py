"""Короткий безопасный вывод Ansible для физического NAS.

Все имена шагов статические и взяты из playbook. Произвольный msg,
stderr, stdout и аргументы задач не выводятся: там могут быть секреты.
APT диагностируется только классификацией типовых ошибок.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ansible.plugins.callback import CallbackBase


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "infra_nas_compact"

    _COUNT = 0

    @staticmethod
    def _title(task) -> str:
        title = str(task.get_name() or "").strip()
        if (
            "{{" in title or "}}" in title
            or len(title) > 140
            or any(ord(char) < 32 or ord(char) == 127 for char in title)
        ):
            return "проверка NAS"
        return title

    @staticmethod
    def _apt_reasons(output: str) -> list[str]:
        """Классификация только по ключевым словам без раскрытия URL/токенов."""

        patterns = [
            (r"temporary failure resolving|could not resolve|name or service not known",
             "DNS не разрешает имя репозитория"),
            (r"network is unreachable|no route to host",
             "отсутствует сетевой маршрут"),
            (r"connection timed out|connection timeout|timed out",
             "тайм-аут подключения к репозиторию"),
            (r"connection refused", "сервер репозитория отклонил соединение"),
            (r"certificate verification failed|ssl certificate|tls handshake",
             "ошибка TLS/сертификата репозитория"),
            (r"no_pubkey|expkeysig|gpg error|signatures couldn't be verified",
             "ошибка GPG-подписи/публичного ключа репозитория"),
            (r"not signed|does not have a release file|release file .*not valid",
             "репозиторий не подписан или отсутствует Release"),
            (r"(?:^|[^0-9])404(?:[^0-9]|$)", "репозиторий вернул HTTP 404"),
            (r"(?:^|[^0-9])403(?:[^0-9]|$)", "репозиторий вернул HTTP 403"),
            (r"(?:^|[^0-9])401(?:[^0-9]|$)", "репозиторий вернул HTTP 401"),
            (r"hash sum mismatch", "контрольная сумма пакета не совпала"),
            (r"could not get lock|unable to acquire.*lock",
             "пакетный менеджер занят другим процессом"),
            (r"no space left on device", "недостаточно места на системном диске"),
            (r"conflicting values set for option signed-by",
             "конфликт Signed-By: для одного источника APT указаны разные ключи"),
            (r"malformed entry|type .* is not known on line",
             "ошибка синтаксиса файла источников APT"),
            (r"clearsigned file isn't valid|invalid signature",
             "источник APT вернул некорректную подпись"),
            (r"repository .*changed its|release file changed",
             "репозиторий изменил сведения Release"),
            (r"unable to locate package|has no installation candidate",
             "пакет не найден в настроенных источниках APT"),
            (r"failed to fetch|some index files failed",
             "не удалось загрузить один или несколько индексов пакетов"),
            (r"could not open lock file|permission denied",
             "недостаточно прав для работы пакетного менеджера"),
        ]
        text = str(output or "").lower()[:50000]
        descriptions = [
            label for expression, label in patterns
            if re.search(expression, text)
        ]
        return descriptions[:4] or [
            "APT вернул ошибку; причина не распознана автоматической диагностикой"
        ]

    @staticmethod
    def _apt_hosts(output: str) -> list[str]:
        """Показывать только DNS-имена репозиториев, без userinfo/путей/токенов."""

        found: list[str] = []
        # urlsplit.hostname отбрасывает username, password, порт, query и path.
        for line in str(output or "").splitlines():
            if not re.match(r"^\s*(?:Err:\d+|E:|W:|Failed to fetch)", line):
                continue
            for match in re.findall(r"https?://[^\s<>]+", line):
                try:
                    host = urlsplit(match.rstrip("',;.)")).hostname
                except ValueError:
                    continue
                if (
                    isinstance(host, str)
                    and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}", host)
                    and host not in found
                ):
                    found.append(host)
                if len(found) >= 4:
                    return found
        return found


    def __init__(self):
        super().__init__()
        self._task_name = ""

    def v2_playbook_on_task_start(self, task, is_conditional):
        del is_conditional
        self._task_name = self._title(task)
        # Подробная проверка выполняется, но стандартные ok/skipped не печатаются.
        if any(word in self._task_name.casefold() for word in (
            "установить", "обновить", "получить", "настроить",
            "запустить", "включить", "создать", "подготовить",
            "применить",
        )):
            self._display.display("[ШАГ] " + self._task_name)

    def v2_runner_on_ok(self, result):
        task = self._title(result._task)
        data = result._result or {}
        if result._task.no_log or data.get("_ansible_no_log"):
            return
        # apt-get update намеренно завершается "ok" с ненулевым rc:
        # нужно успеть показать безопасную классификацию перед fail-задачей.
        if task == "Обновить перечень пакетов после установки OMV-Extras":
            rc = data.get("rc")
            if isinstance(rc, int) and rc != 0:
                self._display.display(f"[ОШИБКА] APT update: код возврата {rc}")
                apt_output = (
                    str(data.get("stdout", "")) + "\n" + str(data.get("stderr", ""))
                )
                for host in self._apt_hosts(apt_output):
                    self._display.display("[РЕПОЗИТОРИЙ] " + host)
                for reason in self._apt_reasons(apt_output):
                    self._display.display("[ПРИЧИНА] " + reason)
        elif data.get("changed"):
            self._display.display("[ОК] " + task)

    def v2_runner_on_failed(self, result, ignore_errors=False):
        if ignore_errors:
            return
        task = self._title(result._task)
        data = result._result or {}
        self._display.display("[ОШИБКА] Задание NAS: " + task)
        if result._task.no_log or data.get("_ansible_no_log"):
            self._display.display(
                "[ПРИЧИНА] Детали скрыты, поскольку задание содержит секреты"
            )
            return
        rc = data.get("rc")
        if isinstance(rc, int):
            self._display.display(f"[ОШИБКА] Код возврата: {rc}")
        if task == "Прервать настройку из-за ошибки обновления APT":
            return
        # Тип ошибки выводится только из заранее утверждённого набора,
        # чтобы не переслать в консоль пароли из msg/args/stdout.
        output = " ".join(
            str(data.get(field, "")) for field in ("msg", "stderr", "stdout")
        )
        action = str(getattr(result._task, "action", ""))
        is_apt = action in ("apt", "ansible.builtin.apt") or "apt" in task.casefold()
        if is_apt:
            for host in self._apt_hosts(output):
                self._display.display("[РЕПОЗИТОРИЙ] " + host)
            for reason in self._apt_reasons(output):
                self._display.display("[ПРИЧИНА] " + reason)
        else:
            lower = output.lower()
            known_errors = (
                ("permission denied", "Недостаточно прав на целевом сервере"),
                ("no such file or directory", "Не найден обязательный файл или каталог"),
                ("network is unreachable", "Сетевой маршрут недоступен"),
                ("connection timed out", "Тайм-аут при обращении к серверу"),
                ("no space left on device", "На системном разделе закончилось место"),
            )
            for key, description in known_errors:
                if key in lower:
                    self._display.display("[ПРИЧИНА] " + description)
                    break
            else:
                self._display.display(
                    "[ПРИЧИНА] Ошибка не распознана безопасным фильтром; "
                    "сырой stdout/stderr скрыт, так как может содержать секреты"
                )

    def v2_runner_on_unreachable(self, result):
        self._display.display(
            "[ОШИБКА] NAS недоступен через SSH на шаге: "
            + self._title(result._task)
        )

    def v2_playbook_on_stats(self, stats):
        for host in sorted(stats.processed):
            summary = stats.summarize(host)
            self._display.display(
                "[ИТОГ] NAS: изменено {changed}, ошибок {failures}, "
                "недоступно {unreachable}".format(
                    changed=summary.get("changed", 0),
                    failures=summary.get("failures", 0),
                    unreachable=summary.get("unreachable", 0),
                )
            )
