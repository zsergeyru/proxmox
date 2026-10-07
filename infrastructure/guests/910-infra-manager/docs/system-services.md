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
8. проверить обязательные функции OpenBao, включая оба SSH CA, `ssh-otp`, `auth/machine` и ограниченный `userpass` оператора;
9. проверить TLS-вход для SSH OTP и встроенного UI;
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
infra-manager guests
infra-manager status [VMID]
infra-manager deploy VMID
infra-manager sync VMID
infra-manager repair [VMID]
infra-manager test VMID
infra-manager recover
```

На физическом PVE `/usr/local/sbin/infra-manager` является только минимальной оболочкой. Она читает VMID управляющего LXC из PVE-only описания, проверяет метку владения и для обычных операций вызывает через `pct exec`:

```text
/usr/local/sbin/infra-manager
```

уже внутри 910. Внутренняя команда реализована модулем `infra_manager.operator`.

- `guests` показывает поддерживаемых гостей и состояние их объектов Proxmox;
- `status` без VMID выполняет полную проверку управляющего контура из 910, включая PVE-only проверки через ограниченные helper-механизмы;
- `status VMID` выполняет общий статус выбранного гостя;
- `deploy VMID`, `sync VMID`, `repair VMID` и `test VMID` внутри 910 через `docker exec` запускают `guest-operation.py` в `infra-runtime`; Semaphore вызывает ту же точку входа из своих заданий, но не участвует в маршруте PVE-команды;
- перед прямым `deploy` оператор автоматически выполняет `fetch/checkout/clean` постоянной Git-копии под исключительной блокировкой и создаёт снимок; `sync/repair/test` создают снимок текущей копии под разделяемой блокировкой, после чего блокировка проекта освобождается;
- изменяющие гостевые операции используют общий для PVE CLI и `infra-runtime` lock по VMID в `/var/lib/infra-manager/locks`; `status` не блокирует VMID;
- PVE-оболочка передаёт скрытый признак доверенного вызова, поэтому после операции пароль может появиться только в текущем root-терминале PVE; журналы Semaphore и Homepage секретов не содержат;
- `repair` без VMID при необходимости запускает остановленный LXC на PVE, но Docker, OpenBao и активация управляющей среды выполняются операторским слоем внутри 910;
- `recover` не передаётся в 910: его выполняет PVE-only `infra-manager-recovery`, который проверяет постоянное состояние, восстанавливает bootstrap Git-доступ и запускает штатный bootstrap recovery.

Отдельной операторской команды `openbao-operator` нет. Показ данных входа OpenBao входит в доверенный `infra-manager status`; низкоуровневая работа с учётными данными остаётся внутренней реализацией.

`repair` без VMID не должен удалять Raft, создавать новое пустое состояние OpenTofu, менять SSH CA из-за отсутствия прежнего состояния или пересоздавать 910. Если безопасного исправления недостаточно, команда завершается ошибкой и предлагает `infra-manager recover`.

### Команды внутри 910

Ansible устанавливает в `/usr/local/sbin/` одну операторскую команду и технические команды автоматизации:

| Команда | Назначение |
|---|---|
| `infra-manager` | операторский слой 910, принимающий команды от тонкой PVE-оболочки |
| `infra-manager-status` | внутренняя полная проверка 910 |
| `infra-manager-activate-runtime` | безопасная отложенная активация нового `infra-runtime` |
| `infra-manager-openbao-startup-unseal` | проверка и разблокировка OpenBao после запуска |

Операторская `infra-manager` внутри 910 не является вторым независимым интерфейсом PVE: штатный человек вызывает её через одноимённую оболочку на физическом PVE. При настройке сначала устанавливается эта внутренняя команда, затем Ansible отдельным шагом `operator-wrapper-install` обновляет тонкую оболочку на PVE. Поэтому PVE не переключается на новую оболочку до успешной установки совместимого операторского слоя внутри управляющего гостя.

Отдельные оболочки `infra-manager-pve-access-check` и `infra-manager-pve-lifecycle-test` не устанавливаются. Соответствующая Python-логика остаётся частью внутренних модулей и тестов.

Для вызова внутреннего состояния с PVE через `pct exec` сохраняется:

```text
/usr/bin/infra-manager-status
```

Отдельные ссылки `/usr/local/bin/infra-manager-*` внутри 910 не создаются.

### Python и внутреннее состояние

Ansible должен устанавливать:

```text
infra-manager-status.yaml
→ /etc/infra-manager/status.yaml

scripts/infra-manager/infra_manager/
→ /usr/local/lib/infra-manager/infra_manager/
```

Файл `../status.yaml` остаётся общим описанием `Status Guest` и внутрь 910 под этим именем не устанавливается.

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

После успешной проверки штатный status должен также показать текущие данные входа OpenBao UI.

Целевой status должен проверять:

- Docker;
- `openbao`;
- `infra-runtime`;
- состояние OpenBao;
- TLS-вход OpenBao для OTP и UI;
- вход ограниченного оператора OpenBao;
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

Служебные машинные секреты не должны выводиться в обычные журналы. Исключение для домашнего контура — текущий пароль ограниченного пользователя OpenBao UI: он намеренно показывается в пользовательском результате успешного Deploy/Status, но не в технических журналах OpenBao, startup-unseal или systemd.

### Восстановление системного слоя

После пересоздания 910 нужно:

1. восстановить подключения `access` и `state`;
2. получить проект;
3. установить Docker;
4. установить Compose-файлы из `rootfs`;
5. установить Python-код и внутренний `infra-manager-status.yaml`;
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
- [`../status.yaml`](../status.yaml) — подключение 910 к общему `Status Guest`;
- [`../infra-manager-status.yaml`](../infra-manager-status.yaml) — внутренняя глубокая проверка 910;
- Ansible-роль `infra_manager`;
- `scripts/infra-manager/commands/` — служебные команды.

Связанные документы:

- [`openbao.md`](openbao.md) — полная спецификация OpenBao и OTP;
- [`infra-runtime.md`](infra-runtime.md) — контейнеры и самообновление;
- [`data-and-access.md`](data-and-access.md) — постоянные и подключаемые данные;
- [`automation-tools.md`](automation-tools.md) — инструменты внутри `infra-runtime`;
- [`../../../../docs/700-security/780-implementation-status.md`](../../../../docs/700-security/780-implementation-status.md) — временные расхождения кода с целевой схемой.
