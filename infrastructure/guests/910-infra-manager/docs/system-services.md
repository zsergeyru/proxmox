# Системные службы и команды 910 infra-manager

**Тип:** локальная спецификация системного уровня
**Статус:** целевое состояние
**Назначение:** описать, какие системные службы и команды должны существовать на 910, как они запускают и проверяют управляющий контур и как восстановить системный слой после пересоздания LXC.

Этот документ описывает требуемое состояние системного слоя 910.

## 1. Состав системного уровня

### Системные службы

На 910 должны существовать:

```text
docker.service
infra-manager-openbao-startup-unseal.service
```

Для самообновления `infra-runtime` допускается временная systemd-служба:

```text
infra-manager-runtime-activate-<epoch>
```

Она создаётся через `systemd-run` только на время активации новой версии и не хранится как постоянный unit.

### Исходные файлы

Воспроизводимые systemd-файлы конкретного гостя хранятся в:

```text
infrastructure/guests/910-infra-manager/rootfs/etc/systemd/system/
```

После Ansible они устанавливаются в:

```text
/etc/systemd/system/
```

Целевой постоянный шаблон OpenBao:

```text
rootfs/etc/systemd/system/
└── infra-manager-openbao-startup-unseal.service.j2
```

## 2. Порядок запуска

### Загрузка 910

После старта LXC порядок должен быть таким:

```text
network-online
      ↓
Docker
      ↓
openbao + infra-runtime через restart: unless-stopped
      ↓
infra-manager-openbao-startup-unseal.service
      ↓
OpenBao initialized=true, sealed=false
      ↓
рабочие секреты материализованы
      ↓
Semaphore доступен
      ↓
infra-manager-status --full
```

Systemd-служба unseal не создаёт OpenBao-контейнер. Контейнер возвращает Docker, а служба проверяет его состояние и выполняет доверенную разблокировку через PVE.

### Docker

Ожидаемое состояние:

```text
docker.service = enabled + active
```

Проверка:

```bash
systemctl is-enabled docker
systemctl is-active docker
docker ps
```

Обязательные контейнеры:

```text
openbao
infra-runtime
```

## 3. OpenBao startup unseal

### Unit

Исходник:

```text
rootfs/etc/systemd/system/infra-manager-openbao-startup-unseal.service.j2
```

Установленный файл:

```text
/etc/systemd/system/infra-manager-openbao-startup-unseal.service
```

Unit должен иметь:

```text
Wants=network-online.target docker.service
After=network-online.target docker.service
Type=oneshot
TimeoutStartSec=180
WantedBy=multi-user.target
```

Команда:

```text
/usr/local/sbin/infra-manager-openbao-startup-unseal <PVE-node>
```

### Поведение

Служба должна:

1. дождаться локального API OpenBao;
2. проверить `sys/seal-status`;
3. не выполнять `sys/init`, если OpenBao пустой, и завершиться ошибкой;
4. для любого `initialized=true` вызвать PVE-only helper;
5. при `sealed=true` разблокировать OpenBao, а при `sealed=false` продолжить без повторной разблокировки;
6. подтвердить `initialized=true, sealed=false`;
7. восстановить рабочие секреты;
8. проверить обязательные функции OpenBao, включая оба SSH CA, `ssh-otp` и `auth/machine`;
9. проверить TLS-вход для SSH OTP;
10. завершиться ошибкой, если целевое состояние не достигнуто.

`sealed=false` является обязательной частью рабочего состояния OpenBao, но не основанием завершать startup-службу до восстановления секретов и остальных проверок.

Подробный контракт находится в [`openbao.md`](openbao.md).

### Проверка

```bash
systemctl is-enabled infra-manager-openbao-startup-unseal.service
systemctl status infra-manager-openbao-startup-unseal.service
journalctl -u infra-manager-openbao-startup-unseal.service
```

Ручной запуск:

```bash
systemctl start infra-manager-openbao-startup-unseal.service
```

## 4. Команды и самообновление

### Единая операторская команда

Человек управляет контуром с физического PVE через одну команду:

```bash
infra-manager status
infra-manager repair
infra-manager recover
```

Она устанавливается как:

```text
/usr/local/sbin/infra-manager
```

и является единственной штатной операторской точкой входа.

- `status` ничего не изменяет: проверяет PVE-only аварийный контур, объект infra-manager и полный внутренний status 910;
- `repair` разрешает только повторяемые безопасные действия: запуск существующего 910, Docker, восстановление OpenBao на прежнем Raft, материализацию секретов, запуск управляющей среды и синхронизацию Semaphore;
- `recover` сначала проверяет сохранность обязательного состояния, восстанавливает bootstrap Git-доступ и только затем запускает штатный bootstrap в режиме восстановления.

`repair` не должен удалять Raft, создавать новое пустое состояние OpenTofu, менять SSH CA из-за отсутствия прежнего состояния или пересоздавать 910. Если безопасного исправления недостаточно, команда завершается ошибкой и предлагает `infra-manager recover`.

### Внутренние команды 910

Ansible устанавливает в `/usr/local/sbin/` только технические команды, необходимые автоматике:

| Команда | Назначение |
|---|---|
| `infra-manager-status` | внутренняя полная проверка 910 |
| `infra-manager-activate-runtime` | безопасная отложенная активация нового `infra-runtime` |
| `infra-manager-openbao-startup-unseal` | проверка и разблокировка OpenBao после запуска |

Отдельные операторские оболочки `infra-manager-pve-access-check` и `infra-manager-pve-lifecycle-test` не устанавливаются. Соответствующая Python-логика остаётся частью внутренних модулей и тестов.

Для вызова внутреннего состояния с PVE через `pct exec` сохраняется:

```text
/usr/bin/infra-manager-status
```

Отдельные ссылки `/usr/local/bin/infra-manager-*` внутри 910 не создаются.

### Python и status.yaml

Ansible должен устанавливать:

```text
status.yaml
→ /etc/infra-manager/status.yaml

scripts/infra-manager/infra_manager/
→ /usr/local/lib/infra-manager/infra_manager/
```

Проверка:

```bash
test -f /etc/infra-manager/status.yaml
test -d /usr/local/lib/infra-manager/infra_manager
PYTHONPATH=/usr/local/lib/infra-manager python3 -c 'import infra_manager'
```

### Самообновление infra-runtime

`Deploy Guest 910` выполняется внутри изменяемого `infra-runtime`, поэтому новый контейнер нельзя активировать до завершения текущего задания.

После Ansible должен запускаться:

```text
systemd-run
  --unit=infra-manager-runtime-activate-<epoch>
  --collect
  env
  INFRA_PROJECT_BRANCH=<branch>
  INFRA_PVE_NODE=<pve>
  /usr/local/sbin/infra-manager-activate-runtime
```

Команда активации должна:

1. дождаться завершения старого задания;
2. поднять новый Compose;
3. выполнить OpenBao startup unseal;
4. дождаться Semaphore;
5. синхронизировать проект Semaphore;
6. выполнить `infra-manager-status --full --quiet`;
7. удалить признак незавершённой активации.

Подробности контейнерного уровня находятся в [`infra-runtime.md`](infra-runtime.md).

## 5. Проверка, журналы и восстановление

### Итоговая проверка

Основная операторская команда выполняется на PVE:

```bash
infra-manager status
```

Она сама вызывает внутреннюю проверку 910. Для диагностики реализации остаётся технический вызов:

```bash
pct exec 910 -- infra-manager-status --full
```

Целевой status должен проверять:

- Docker;
- `openbao`;
- `infra-runtime`;
- состояние OpenBao;
- TLS-вход OpenBao для OTP;
- Semaphore;
- OpenTofu, Ansible и Packer;
- PVE-доступ;

### Журналы

| Что | Где смотреть |
|---|---|
| Docker | `journalctl -u docker` |
| OpenBao | `docker logs openbao` |
| Semaphore / infra-runtime | `docker logs infra-runtime` |
| startup unseal | `journalctl -u infra-manager-openbao-startup-unseal.service` |
| активация runtime | `/var/log/infra-manager/runtime-activation.log` |

Секреты не должны выводиться в обычные журналы.

### Восстановление системного слоя

После пересоздания 910 нужно:

1. восстановить подключения `access` и `state`;
2. получить проект;
3. установить Docker;
4. установить Compose-файлы из `rootfs`;
5. установить Python-код и `status.yaml`;
6. установить служебные команды;
7. установить `infra-manager-openbao-startup-unseal.service`;
8. выполнить `systemctl daemon-reload`;
9. включить Docker и startup-unseal;
10. собрать и запустить контейнеры;
11. разблокировать и проверить OpenBao;
12. проверить Semaphore;
13. выполнить полный status.

### Типичные неисправности

Если OpenBao после перезагрузки остаётся запечатанным, сначала выполнить с PVE:

```bash
infra-manager status
infra-manager repair
```

Для углублённой диагностики внутри 910:

```bash
systemctl status infra-manager-openbao-startup-unseal.service
journalctl -u infra-manager-openbao-startup-unseal.service -n 100
```

Если `infra-manager-status` не находится через `pct exec`:

```bash
ls -l /usr/local/sbin/infra-manager-status
ls -l /usr/local/bin/infra-manager-status
ls -l /usr/bin/infra-manager-status
```

Если самообновление завершилось ошибкой:

```bash
tail -n 200 /var/log/infra-manager/runtime-activation.log
systemctl --failed
```

Обновление не считается завершённым, пока `infra-manager-status --full` не проходит.

## 6. Источники и связанные документы

Целевое состояние должно быть реализовано через:

- [`../rootfs/etc/systemd/system/`](../rootfs/etc/systemd/system/) — unit-файлы 910;
- [`../status.yaml`](../status.yaml) — итоговые проверки;
- Ansible-роль `infra_manager`;
- `scripts/infra-manager/commands/` — служебные команды.

Связанные документы:

- [`openbao.md`](openbao.md) — полная спецификация OpenBao и OTP;
- [`infra-runtime.md`](infra-runtime.md) — контейнеры и самообновление;
- [`data-and-access.md`](data-and-access.md) — постоянные и подключаемые данные;
- [`automation-tools.md`](automation-tools.md) — инструменты внутри `infra-runtime`;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — временные расхождения кода с целевой схемой.
