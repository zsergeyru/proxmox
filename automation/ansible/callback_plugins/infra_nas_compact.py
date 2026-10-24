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
    def _samba_reasons(output: str) -> list[str]:
        """Безопасная классификация причин отказа smbd по systemd-журналу."""

        lower = str(output or "").lower()[:50000]
        patterns = (
            (r"address already in use|failed to bind|bind.*failed|"
             r"error binding to.*(?:445|139)",
             "Порт Samba 445/139 занят другим процессом"),
            (r"failed to load services|error loading services|"
             r"failed to load .*smb\.conf|error in.*smb\.conf|"
             r"failed to parse.*smb\.conf|testparm.*failed",
             "Ошибка конфигурации Samba, созданной средствами OMV"),
            (r"unit .*smbd.*not found|smbd\.service.*not found|"
             r"could not be found|failed to find executable",
             "Не установлена или недоступна штатная служба Samba"),
            (r"unit .*smbd.*masked|smbd\.service is masked",
             "Служба Samba заблокирована в systemd (masked)"),
            (r"permission denied|operation not permitted",
             "Службе Samba не хватает разрешений для запуска"),
            (r"no space left on device",
             "Недостаточно места для работы службы Samba"),
            (r"start request repeated too quickly|start-limit-hit",
             "Samba несколько раз аварийно завершилась при запуске"),
            (r"out of memory|oom-kill|killed process.*smbd",
             "Процесс Samba завершён из-за нехватки памяти"),
            (r"timed out|timeout", "Превышено время запуска Samba"),
        )
        result = [description for pattern, description in patterns
                  if re.search(pattern, lower)]
        return result[:3] or [
            "Samba не стартовала; systemd-журнал не содержит распознанной причины"
        ]

    @staticmethod
    def _qbittorrent_reasons(output: str) -> list[str]:
        """Классифицировать docker/systemd без вывода текста журнала и секретов."""

        text = str(output or "").lower()[:50000]
        known = (
            (r"port is already allocated|bind: address already in use|"
             r"failed to bind host port|address already in use",
             "Docker не смог занять сетевой порт qBittorrent (8080 либо 6881)"),
            (r"manifest unknown|no matching manifest|manifest for .* not found",
             "Указанный образ или архитектура недоступны в реестре Docker"),
            (r"toomanyrequests|too many requests|rate limit",
             "Реестр Docker ограничил число запросов"),
            (r"pull access denied|requested access to the resource is denied|"
             r"unauthorized: authentication required",
             "Реестр Docker отклонил доступ к образу"),
            (r"lookup .* no such host|temporary failure in name resolution|"
             r"could not resolve host|network is unreachable|no route to host",
             "У NAS проблема с DNS либо маршрутом до реестра Docker"),
            (r"x509:|certificate verify failed|tls handshake|unknown authority",
             "Не удалось проверить TLS-сертификат реестра Docker"),
            (r"i/o timeout|context deadline exceeded|connection timed out|"
             r"client.timeout exceeded|net/http: request canceled",
             "Время ожидания сетевого соединения Docker истекло"),
            (r"no space left on device|insufficient space",
             "На диске для Docker недостаточно свободного места"),
            (r"cannot connect to the docker daemon|is the docker daemon running|"
             r"error during connect",
             "Docker Engine недоступен при запуске контейнера"),
            (r"permission denied|operation not permitted",
             "Отказ в правах Docker, контейнера либо подключённого каталога"),
            (r"invalid compose project|validating .*compose|additional properties "
             r"are not allowed|yaml: line|services\.qbittorrent",
             "Docker Compose отклонил описание приложения"),
            (r"failed to create shim task|failed to start containerd task",
             "Ошибка запуска контейнера в containerd"),
            (r"timed out waiting for.*start|start operation timed out|"
             r"start operation timed out\. terminating|start-pre operation timed out",
             "Превышено время запуска службы systemd"),
            (r"conditionpathismountpoint|not a mountpoint|"
             r"failed at step exec|executable .* not found",
             "Не выполнено условие подключения HDD либо отсутствует команда Docker"),
        )
        result = [label for pattern, label in known if re.search(pattern, text)]
        return result[:3] or [
            "Причина запуска qBittorrent не определена по журналу; "
            "доступна безопасная диагностика systemctl status и journalctl на NAS"
        ]

    @staticmethod
    def _urbackup_download_reasons(output: str) -> list[str]:
        """Распознавать ошибки HTTPS, не раскрывая ответ сервера и URL."""
        lower = str(output or "").lower()[:20000]
        known = (
            (r"name or service not known|temporary failure in name resolution|"
             r"nodename nor servname provided|could not resolve|"
             r"getaddrinfo failed",
             "DNS NAS не разрешает имя сервера установщика UrBackup"),
            (r"certificate.verify.failed|certificate verification failed|"
             r"certificate verify failed|unable to get local issuer|"
             r"self.signed certificate|unknown ca",
             "NAS не может проверить TLS-сертификат сервера установщика"),
            (r"ssl.*handshake|tls.*handshake|wrong version number|"
             r"ssl.*protocol",
             "Ошибка TLS-соединения NAS с сервером установщика"),
            (r"connection timed out|read timed out|timed out|timeout",
             "Превышено время загрузки установщика UrBackup"),
            (r"network is unreachable|no route to host",
             "Отсутствует маршрут NAS до сервера загрузки"),
            (r"connection refused|failed to establish a new connection",
             "Не удалось установить соединение с сервером загрузки"),
            (r"http error 404|status code.*404|\b404 not found\b",
             "Адрес установщика вернул HTTP 404"),
            (r"http error 403|status code.*403|\b403 forbidden\b",
             "Сервер установщика отклонил загрузку (HTTP 403)"),
            (r"http error 429|status code.*429",
             "Сервер установщика ограничил число запросов (HTTP 429)"),
            (r"http error 5[0-9][0-9]|status code.*5[0-9][0-9]",
             "Ошибка HTTP 5xx на сервере установщика"),
            (r"no space left on device|insufficient space",
             "Недостаточно места на системном SSD NAS"),
            (r"permission denied|operation not permitted",
             "Нет прав для сохранения установщика на NAS"),
        )
        result = [description for pattern, description in known if re.search(pattern, lower)]
        return result[:2] or [
            "Не удалось скачать официальный установщик; причина не классифицирована "
            "без вывода потенциально чувствительных деталей"
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
        if task in (
            "Обновить перечень пакетов после установки OMV-Extras",
            "Повторить обновление APT через проверенные зеркала OMV",
        ):
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
            elif task == "Повторить обновление APT через проверенные зеркала OMV":
                self._display.display(
                    "[ОК] Индексы APT обновлены через официальный список зеркал OMV"
                )
        elif task == "Проверить DNS основного репозитория OMV после ошибки APT":
            rc = data.get("rc")
            if isinstance(rc, int):
                self._display.display(
                    "[ПРОВЕРКА] DNS packages.openmediavault.org: "
                    + ("работает" if rc == 0 else "не удалось разрешить имя")
                )
        elif task == "Проверить HTTPS-ответ основного репозитория OMV после ошибки APT":
            # Только фиксированные числовые значения curl: stdout может быть
            # недоверенным и никогда не должен выводиться без фильтрации.
            code = str(data.get("stdout", "")).strip()
            if re.fullmatch(r"[1-5][0-9]{2}", code):
                self._display.display(
                    f"[ПРОВЕРКА] HTTPS-запрос к OMV: HTTP {code} "
                    "(независимая проверка, не ответ APT)"
                )
            elif code == "000":
                self._display.display(
                    "[ПРОВЕРКА] HTTPS-запрос к OMV: HTTP-ответ не получен"
                )
            rc = data.get("rc")
            curl_reasons = {
                6: "не удалось разрешить DNS-имя",
                7: "не удалось подключиться к серверу",
                28: "время ожидания соединения истекло",
                35: "ошибка TLS-соединения",
                60: "сертификат TLS не прошёл проверку",
            }
            if isinstance(rc, int) and rc in curl_reasons:
                self._display.display("[ПРИЧИНА] Проверка HTTPS: " + curl_reasons[rc])
        elif task == "Собрать ZFS для работающего ядра через DKMS":
            rc = data.get("rc")
            if isinstance(rc, int) and rc != 0:
                self._display.display(f"[ОШИБКА] Сборка DKMS завершилась с кодом {rc}")
                reason = (
                    str(data.get("stdout", "")) + "\n"
                    + str(data.get("stderr", ""))
                ).lower()
                causes = (
                    ("missing kernel headers", "Не найдены заголовки ядра"),
                    ("kernel headers", "Проблема с заголовками ядра"),
                    ("bad return status for module build",
                     "Компилятор не смог собрать модуль для этого ядра"),
                    ("error!  bad return status",
                     "Компилятор не смог собрать модуль для этого ядра"),
                    ("gcc: command not found", "Не установлен компилятор GCC"),
                    ("no space left on device", "Недостаточно места на системном SSD"),
                )
                for hint, description in causes:
                    if hint in reason:
                        self._display.display("[ПРИЧИНА] " + description)
                        break
                else:
                    self._display.display(
                        "[ПРИЧИНА] DKMS не собрал ZFS: проверьте совместимость "
                        "версий ядра, ZFS и заголовков"
                    )
        elif task == "Прочитать журнал отказа запуска qBittorrent":
            if data.get("rc") != 0:
                self._display.display(
                    "[ПРИЧИНА] Не удалось прочитать журнал systemd для qBittorrent"
                )
            else:
                for reason in self._qbittorrent_reasons(data.get("stdout", "")):
                    self._display.display("[ПРИЧИНА] " + reason)
        elif task == "Прочитать журнал отказа Samba":
            if data.get("rc") != 0:
                self._display.display(
                    "[ПРИЧИНА] Не удалось прочитать журнал systemd службы Samba"
                )
            else:
                for reason in self._samba_reasons(data.get("stdout", "")):
                    self._display.display("[ПРИЧИНА] " + reason)
        elif task == "Проверить запущенную службу Samba":
            self._display.display("[ОК] Samba запущена и доступна как smbd.service")
        elif task == "Подтвердить загрузку ZFS в работающее ядро":
            self._display.display("[ОК] ZFS загружен для работающего ядра NAS")
        elif task == "Загрузить проверенный модуль ZFS в работающее ядро":
            rc = data.get("rc")
            if isinstance(rc, int) and rc != 0:
                self._display.display(f"[ОШИБКА] Загрузка модуля ZFS: код {rc}")
                self._display.display(
                    "[ПРИЧИНА] modprobe zfs не смог загрузить модуль "
                    "(возможны несовместимость ядра или запрет Secure Boot)"
                )
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
        # Перед разбором конкретного этапа строго не показываем произвольные
        # msg/stderr/stdout Ansible: они могут содержать пароли или пути.
        output = " ".join(
            str(data.get(field, "")) for field in ("msg", "stderr", "stdout")
        )
        if task == "Включить автозапуск и запустить Samba":
            self._display.display(
                "[ПРОВЕРКА] Читаем системный журнал отказа службы Samba"
            )
            return
        if task == "Проверить итоговую конфигурацию Samba":
            self._display.display(
                "[ПРИЧИНА] Проверка samba-tool testparm завершилась ошибкой; "
                "необходимо проверить модель конфигурации OMV"
            )
            return
        if task == "Прервать настройку при ошибке службы Samba":
            self._display.display(
                "[ПРИЧИНА] Samba не запущена; данные дисков не изменялись"
            )
            return
        if task == "Проверить запущенную службу Samba":
            self._display.display(
                "[ПРИЧИНА] Samba не активна (systemctl is-active вернул "
                + ("inactive" if rc == 3 else "ошибку") + ")"
            )
            return
        if task == "Зарегистрировать папки и ресурсы через базу OMV":
            # Скрипт печатает только фиксированную стадию и код возврата.
            # Сырые stderr/stdout OMV (включая возможные секреты) скрываем.
            stage_match = re.search(
                r"NAS_SMB_STEP=(read-folders|read-shares|read-settings|"
                r"create-folder|create-share|enable-smb|apply-samba|unknown) "
                r"RC=([0-9]{1,3})", output,
            )
            descriptions = {
                "read-folders": "Не удалось прочитать список общих папок OMV",
                "read-shares": "Не удалось прочитать SMB-ресурсы OMV",
                "read-settings": "Не удалось прочитать общие настройки SMB",
                "create-folder": "OMV отказала в регистрации новой общей папки",
                "create-share": "OMV отказала в регистрации нового SMB-ресурса",
                "enable-smb": "OMV отказала при включении службы Samba",
                "apply-samba": "Не удалось применить настройки Samba через omv-salt",
                "unknown": "Неизвестный этап обработки SMB",
            }
            if stage_match:
                self._display.display(
                    "[ПРИЧИНА] " + descriptions[stage_match.group(1)] +
                    " (код " + stage_match.group(2) + ")"
                )
            else:
                self._display.display(
                    "[ПРИЧИНА] Регистрация SMB прервана из-за ошибки "
                    "модели OMV, существующего конфликта или неподтверждённого пути"
                )
            return
        if task == "Подготовить отдельную shared mount-точку для ZFS снимков":
            # Root-owned helper prints ONLY a fixed stage code. Never expose
            # arbitrary stdout/stderr from Ansible or the mount utilities.
            match = re.search(
                r"NAS_URBACKUP_MOUNT_STAGE=(preflight|directory|occupied|"
                r"inspect|bind|propagation|missing|not-shared)(?:\\b|$)",
                output,
            )
            stages = {
                "preflight": "Проверка GUID или подключений ZFS не прошла",
                "directory": "Недопустимый или недоступный каталог staging в /run",
                "occupied": "Каталог staging не пуст: не скрываем данные монтированием",
                "inspect": "Не удалось проверить mountpoint командой findmnt",
                "bind": "Не удалось создать bind mount staging",
                "propagation": "Не удалось включить mount propagation shared",
                "missing": "Монтирование staging отсутствует в таблице mountpoint",
                "not-shared": "Staging смонтирован, но propagation не shared",
            }
            self._display.display(
                "[ПРИЧИНА] " + (
                    stages[match.group(1)] if match else
                    "Ошибка подготовки ZFS staging; безопасный код причины отсутствует"
                )
            )
            return
        if task == "Скачать UrBackup Client 2.5.31 с официального архивного зеркала":
            for reason in self._urbackup_download_reasons(output):
                self._display.display("[ПРИЧИНА] " + reason)
            return
        if task == "Загрузить образ qBittorrent при отсутствии":
            for reason in self._qbittorrent_reasons(
                " ".join(str(data.get(field, "")) for field in ("msg", "stderr", "stdout"))
            ):
                self._display.display("[ПРИЧИНА] " + reason)
            return
        if task == "Подключить службу qBittorrent после проверки HDD":
            self._display.display(
                "[ПРОВЕРКА] Читаем журнал systemd, чтобы определить причину отказа"
            )
            return
        if task == "Остановить развёртывание после ошибки запуска qBittorrent":
            self._display.display(
                "[ПРИЧИНА] Контейнер не запущен; диски NAS не изменялись"
            )
            return
        # Тип ошибки выводится только из заранее утверждённого набора,
        # чтобы не переслать в консоль пароли из msg/args/stdout.
        if task in {
            "Подтвердить готовность ZFS после сборки DKMS",
            "Подтвердить загрузку ZFS в работающее ядро",
            "Проверить безопасное имя работающего ядра для ZFS",
        }:
            descriptions = {
                "Подтвердить готовность ZFS после сборки DKMS":
                    "ZFS не собран для работающего ядра. Проверьте заголовки, DKMS и совместимость; ядро не менялось.",
                "Подтвердить загрузку ZFS в работающее ядро":
                    "Модуль ZFS не загрузился; работа с дисками не начиналась.",
                "Проверить безопасное имя работающего ядра для ZFS":
                    "Не удалось безопасно определить работающую версию ядра.",
            }
            self._display.display("[ПРИЧИНА] " + descriptions[task])
            return
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
