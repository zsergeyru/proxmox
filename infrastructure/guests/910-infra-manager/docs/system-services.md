# Системные службы и команды 910 infra-manager

**Назначение:** практическое руководство по установке, запуску и диагностике системных служб и команд, которые связывают Debian 910, Docker, OpenBao и `infra-runtime`.

## 1. Что устанавливается в систему 910

Постоянные файлы гостя берутся из:

```text
infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/
```

Сейчас там находятся шаблоны:

```text
infra-manager-openbao-startup-unseal.service.j2
infra-manager-machine-ssh-refresh.service.j2
infra-manager-machine-ssh-refresh.timer.j2
```

Ansible устанавливает их в:

```text
/etc/systemd/system/
```

Фактическая установка выполняется в:

```text
automation/ansible/roles/infra_manager/tasks/runtime.yml
```

## 2. Основной порядок запуска 910

После запуска LXC ожидается такой порядок:

```text
network-online
      ↓
Docker
      ↓
контейнеры restart: unless-stopped
      ↓
infra-manager-openbao-startup-unseal.service
      ↓
OpenBao разблокирован + рабочие секреты материализованы
      ↓
infra-runtime / Semaphore готовы
      ↓
проверка состояния
```

При обычной загрузке Docker сам возвращает контейнеры благодаря `restart: unless-stopped`.

Systemd-служба OpenBao не создаёт контейнер — она работает уже после запуска Docker и проверяет/разблокирует OpenBao.

## 3. Docker

Docker устанавливается общей Ansible-ролью из официального репозитория.

Ожидаемое состояние:

```text
docker.service: enabled + running
```

Проверить:

```bash
systemctl is-enabled docker
systemctl is-active docker
docker ps
```

Проектные контейнеры:

```text
openbao
infra-runtime
```

## 4. Служба разблокировки OpenBao

Имя:

```text
infra-manager-openbao-startup-unseal.service
```

Исходник:

```text
rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2
```

После шаблонизации устанавливается:

```text
/etc/systemd/system/infra-manager-openbao-startup-unseal.service
```

### 4.1. Unit

Основные зависимости:

```text
Wants=network-online.target docker.service
After=network-online.target docker.service
```

Тип:

```text
Type=oneshot
```

Команда:

```text
/usr/local/sbin/infra-manager-openbao-startup-unseal <PVE-узел>
```

Ограничение:

```text
TimeoutStartSec=180
```

Служба включается в `multi-user.target`.

### 4.2. Установка

Ansible:

1. шаблонизирует unit с текущим `infra_pve_node`;
2. записывает файл с режимом `0644`;
3. выполняет `daemon_reload`;
4. включает службу.

Проверить установленный unit:

```bash
systemctl cat infra-manager-openbao-startup-unseal.service
systemctl is-enabled infra-manager-openbao-startup-unseal.service
```

### 4.3. Ручной запуск

```bash
systemctl start infra-manager-openbao-startup-unseal.service
systemctl status infra-manager-openbao-startup-unseal.service
```

Журнал:

```bash
journalctl -u infra-manager-openbao-startup-unseal.service
```

Подробная логика unseal описана в [`openbao.md`](openbao.md).

## 5. Переходный таймер межмашинного SSH

Сейчас устанавливаются:

```text
infra-manager-machine-ssh-refresh.service
infra-manager-machine-ssh-refresh.timer
```

Это **переходная**, а не целевая схема.

### 5.1. Service

Служба зависит от:

```text
network-online.target
docker.service
infra-manager-openbao-startup-unseal.service
```

Команда:

```text
/usr/local/sbin/infra-manager-machine-ssh-refresh /var/lib/infra-manager/bootstrap-repo
```

Timeout:

```text
300 секунд
```

### 5.2. Timer

Расписание:

```text
OnBootSec=5min
OnUnitActiveSec=30min
RandomizedDelaySec=2min
Persistent=true
```

Ansible включает timer и сразу переводит его в `started`.

Проверить:

```bash
systemctl status infra-manager-machine-ssh-refresh.timer
systemctl list-timers infra-manager-machine-ssh-refresh.timer
systemctl cat infra-manager-machine-ssh-refresh.service
```

Запустить разово:

```bash
systemctl start infra-manager-machine-ssh-refresh.service
```

Журнал:

```bash
journalctl -u infra-manager-machine-ssh-refresh.service
```

После завершения перехода проекта на OpenBao SSH OTP этот service/timer должны быть удалены из `rootfs` и `runtime.yml` вместе со старой логикой.

## 6. Удаление старого PVE-таймера unseal

Ранее OpenBao разблокировался периодическим таймером непосредственно на PVE.

При настройке 910 `runtime.yml` через отдельный root SSH канал выполняет на PVE:

```text
systemctl disable --now infra-manager-openbao-unseal.timer
systemctl disable --now infra-manager-openbao-unseal.service
```

и удаляет старые unit-файлы и ссылки.

После этого выполняется `systemctl daemon-reload`.

Проверка на PVE:

```bash
systemctl status infra-manager-openbao-unseal.timer
systemctl status infra-manager-openbao-unseal.service
```

Ожидаемо старые unit не должны быть активным штатным механизмом.

## 7. Команды, устанавливаемые на 910

Ansible копирует исходники из:

```text
scripts/infra-manager/commands/
```

в:

```text
/usr/local/sbin/
```

с владельцем `root:root` и режимом `0755`.

Текущий набор:

| Команда | Назначение |
|---|---|
| `infra-manager-status` | общая проверка состояния 910 |
| `infra-manager-pve-access-check` | проверка PVE API и root SSH |
| `infra-manager-pve-lifecycle-test` | интеграционный тест жизненного цикла PVE-объекта |
| `infra-manager-activate-runtime` | отложенная активация нового `infra-runtime` |
| `infra-manager-openbao-startup-unseal` | проверка и unseal OpenBao после запуска |
| `infra-manager-machine-ssh-refresh` | переходное обновление машинных SSH-сертификатов |

Для операторских команд создаются ссылки:

```text
/usr/local/bin/infra-manager-status
/usr/local/bin/infra-manager-pve-access-check
/usr/local/bin/infra-manager-pve-lifecycle-test
```

`infra-manager-status` дополнительно доступен как:

```text
/usr/bin/infra-manager-status
```

чтобы команда гарантированно находилась при вызове через `pct exec` с PVE.

## 8. Установка Python-кода и status.yaml

В `runtime.yml` также устанавливаются:

```text
status.yaml
    → /etc/infra-manager/status.yaml

scripts/infra-manager/infra_manager/
    → /usr/local/lib/infra-manager/infra_manager/
```

Таким образом, команды `/usr/local/sbin/infra-manager-*` используют локально установленную версию Python-кода, соответствующую текущей версии проекта 910.

Проверить:

```bash
test -f /etc/infra-manager/status.yaml
test -d /usr/local/lib/infra-manager/infra_manager
python3 -c 'import sys; sys.path.insert(0, "/usr/local/lib/infra-manager"); import infra_manager'
```

## 9. Временная systemd-служба самообновления

Для `Deploy Guest 910` постоянного unit-файла активации нет.

В конце `configure-guest.yml`, если `infra_self_update=true`, выполняется:

```text
systemd-run
  --unit=infra-manager-runtime-activate-<epoch>
  --collect
  env
  INFRA_PROJECT_BRANCH=<branch>
  INFRA_PVE_NODE=<pve>
  /usr/local/sbin/infra-manager-activate-runtime
```

Такая служба существует только на время активации.

Проверить выполняющуюся активацию:

```bash
systemctl list-units 'infra-manager-runtime-activate-*'
journalctl -u 'infra-manager-runtime-activate-*'
```

Основной журнал команды находится отдельно:

```text
/var/log/infra-manager/runtime-activation.log
```

## 10. Как полностью установить системную часть после пересоздания 910

Штатно это делает Ansible. Для понимания порядок такой:

1. установить пакеты Linux и Docker;
2. подготовить `/etc/infra-manager`, `/usr/local/lib/infra-manager`, `/opt/infra-manager/compose`;
3. получить рабочую копию Git;
4. скопировать Compose-файлы из `rootfs`;
5. скопировать `status.yaml` в `/etc/infra-manager/status.yaml`;
6. скопировать Python-пакет `infra_manager` в `/usr/local/lib/infra-manager/`;
7. установить команды в `/usr/local/sbin`;
8. создать операторские symlink;
9. шаблонизировать `infra-manager-openbao-startup-unseal.service`;
10. выполнить `systemctl daemon-reload` и включить службу;
11. установить переходные `machine-ssh-refresh.service/timer`;
12. включить и запустить timer;
13. удалить старую схему PVE timer unseal;
14. собрать и запустить контейнеры;
15. выполнить полную проверку.

Ручная установка допустима для аварийной диагностики, но после неё следует прогнать штатный Ansible, чтобы система снова соответствовала репозиторию.

## 11. Основная проверка состояния

Короткая:

```bash
infra-manager-status
```

Полная:

```bash
infra-manager-status --full
```

С PVE:

```bash
pct exec 910 -- infra-manager-status --full
```

Порядок и состав проверок берутся из [`../status.yaml`](../status.yaml).

Сейчас проверяются:

1. Docker и `infra-runtime`;
2. OpenBao;
3. Semaphore;
4. OpenTofu, Ansible и Packer;
5. PVE-доступ.

Итоговый экран также показывает адрес 910, адрес Semaphore, admin/password, ветку и ревизию проекта.

## 12. Диагностика после перезагрузки 910

Проверять лучше в таком порядке:

```bash
systemctl status docker
docker ps -a
systemctl status infra-manager-openbao-startup-unseal.service
curl -fsS http://127.0.0.1:8200/v1/sys/seal-status | jq
curl -fsS http://127.0.0.1:3000/api/ping
infra-manager-status --full
```

Если проблема относится к переходному межмашинному SSH:

```bash
systemctl status infra-manager-machine-ssh-refresh.timer
journalctl -u infra-manager-machine-ssh-refresh.service -n 100
```

## 13. Журналы

Основные места:

| Что | Где смотреть |
|---|---|
| Docker daemon | `journalctl -u docker` |
| OpenBao container | `docker logs openbao` |
| Semaphore/infra-runtime | `docker logs infra-runtime` |
| startup unseal | `journalctl -u infra-manager-openbao-startup-unseal.service` |
| machine SSH refresh | `journalctl -u infra-manager-machine-ssh-refresh.service` |
| самообновление runtime | `/var/log/infra-manager/runtime-activation.log` |

Секреты не должны выводиться в журналы.

## 14. Типичные неисправности

### 14.1. После перезагрузки OpenBao запечатан

Проверить Docker, затем:

```bash
systemctl status infra-manager-openbao-startup-unseal.service
journalctl -u infra-manager-openbao-startup-unseal.service -n 100
```

Если служба не включена:

```bash
systemctl enable infra-manager-openbao-startup-unseal.service
systemctl daemon-reload
systemctl start infra-manager-openbao-startup-unseal.service
```

После ручного исправления повторно прогнать Ansible.

### 14.2. `infra-manager-status` не найден через pct exec

Проверить ссылки:

```bash
ls -l /usr/local/sbin/infra-manager-status
ls -l /usr/local/bin/infra-manager-status
ls -l /usr/bin/infra-manager-status
```

Их создаёт `runtime.yml`.

### 14.3. Временная служба самообновления завершилась ошибкой

Проверить:

```bash
tail -n 200 /var/log/infra-manager/runtime-activation.log
systemctl --failed
```

После исправления причины нельзя считать обновление завершённым, пока `infra-manager-status --full` не проходит.

## 15. Связанные документы

- [`../rootfs/etc/systemd/system/`](../rootfs/etc/systemd/system/) — шаблоны постоянных unit.
- [`../status.yaml`](../status.yaml) — машинный состав итоговой проверки.
- [`openbao.md`](openbao.md) — контейнер и процедура unseal.
- [`infra-runtime.md`](infra-runtime.md) — безопасное самообновление контейнера.
- [`data-and-access.md`](data-and-access.md) — необходимые mount и доступы.
- [`automation-tools.md`](automation-tools.md) — инструменты внутри `infra-runtime`.
- [`../../../../automation/ansible/roles/infra_manager/tasks/runtime.yml`](../../../../automation/ansible/roles/infra_manager/tasks/runtime.yml) — фактическая установка команд и unit.
- [`../../../../automation/ansible/playbooks/configure-guest.yml`](../../../../automation/ansible/playbooks/configure-guest.yml) — запуск временной службы активации.
- [`../../../../scripts/infra-manager/commands/`](../../../../scripts/infra-manager/commands/) — исходники устанавливаемых команд.
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — состояние переходного SSH-механизма.