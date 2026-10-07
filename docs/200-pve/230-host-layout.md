# Структура PVE-хоста

**Тип:** справочник  
**Статус:** действующий  
**Назначение:** зафиксировать точные пути постоянных и служебных данных проекта на физическом PVE.

Этот документ отвечает на вопрос:

> Где именно находится конкретный файл или каталог управляющего контура?

Правила, зачем эти области существуют и какие гарантии от них требуются, находятся в спецификации постоянного состояния PVE.

## Содержание

- [1. Канонический корень постоянных данных](#1-канонический-корень-постоянных-данных)
- [2. Область только для PVE](#2-область-только-для-pve)
- [3. Данные PVE для чтения из 910](#3-данные-pve-для-чтения-из-910)
- [4. Изменяемое состояние 910](#4-изменяемое-состояние-910)
- [5. Связи каталогов внутри 910](#5-связи-каталогов-внутри-910)
- [6. Другие файлы на PVE](#6-другие-файлы-на-pve)
- [7. Каталог первоначальной подготовки](#7-каталог-первоначальной-подготовки)
- [8. Временные файлы](#8-временные-файлы)
- [9. Связанные документы](#9-связанные-документы)

## 1. Канонический корень постоянных данных

Основной корень проекта на PVE:

```text
/mnt/bindmounts/infra-manager/
├── pve-only/
├── access/
└── state/
```

Назначение верхнего уровня:

| Каталог | Доступ из 910 | Назначение |
|---|---|---|
| `pve-only/` | нет | секреты и служебные данные только PVE |
| `access/` | только чтение | данные PVE, необходимые 910 |
| `state/` | чтение и запись | постоянное состояние сервисов 910 |

## 2. Область только для PVE

Текущая структура:

```text
/mnt/bindmounts/infra-manager/pve-only/
├── openbao/
│   ├── unseal.key
│   ├── ssh-access.json
│   ├── kv-access.json
│   └── operator-access.json
└── recovery/
    └── github_proxmox_repo_ed25519
```

| Полный путь на PVE | Назначение |
|---|---|
| `/mnt/bindmounts/infra-manager/pve-only/openbao/unseal.key` | ключ снятия блокировки OpenBao |
| `/mnt/bindmounts/infra-manager/pve-only/openbao/ssh-access.json` | служебные RoleID/SecretID для операций SSH-подписи OpenBao |
| `/mnt/bindmounts/infra-manager/pve-only/openbao/kv-access.json` | служебные RoleID/SecretID для чтения KV и узкого обновления Semaphore token |
| `/mnt/bindmounts/infra-manager/pve-only/openbao/operator-access.json` | логин и текущий пароль человека для ограниченного входа в OpenBao UI |
| `/mnt/bindmounts/infra-manager/pve-only/recovery/github_proxmox_repo_ed25519` | аварийная PVE-only копия read-only GitHub Deploy Key для восстановления проекта до запуска 910/OpenBao |

Весь каталог `pve-only/` остаётся только на PVE и в 910 не монтируется.

## 3. Данные PVE для чтения из 910

Физически данные находятся на PVE под:

```text
/mnt/bindmounts/infra-manager/access/
```

Весь каталог подключается в 910 как `/mnt/pve-access/` только для чтения.

| Полный путь на PVE | Полный путь внутри 910 | Назначение |
|---|---|---|
| `/mnt/bindmounts/infra-manager/access/pve-host/root_ed25519` | `/mnt/pve-access/pve-host/root_ed25519` | закрытый root SSH-ключ 910 для PVE |
| `/mnt/bindmounts/infra-manager/access/pve-host/root_ed25519.pub` | `/mnt/pve-access/pve-host/root_ed25519.pub` | производный открытый ключ той же SSH-идентичности |
| `/mnt/bindmounts/infra-manager/access/pve-host/known_hosts` | `/mnt/pve-access/pve-host/known_hosts` | доверенный ключ SSH-сервера PVE |
| `/mnt/bindmounts/infra-manager/access/ca/pve-root-ca.crt` | `/mnt/pve-access/ca/pve-root-ca.crt` | копия корневого сертификата PVE |

## 4. Изменяемое состояние 910

Физически состояние находится на PVE под:

```text
/mnt/bindmounts/infra-manager/state/
```

Весь каталог подключается в 910 как `/mnt/persistent-state/` с правом чтения и записи.

| Полный путь на PVE | Путь после подключения в 910 | Назначение |
|---|---|---|
| `/mnt/bindmounts/infra-manager/state/ansible/` | `/mnt/persistent-state/ansible/` | постоянная Ansible-идентичность |
| `/mnt/bindmounts/infra-manager/state/semaphore/` | `/mnt/persistent-state/semaphore/` | база и служебные данные Semaphore |
| `/mnt/bindmounts/infra-manager/state/opentofu/` | `/mnt/persistent-state/opentofu/` | постоянные данные и состояние OpenTofu |
| `/mnt/bindmounts/infra-manager/state/locks/` | `/mnt/persistent-state/locks/` | общие координационные lock-файлы PVE CLI и `infra-runtime` |
| `/mnt/bindmounts/infra-manager/state/openbao/` | `/mnt/persistent-state/openbao/` | постоянные Raft-данные OpenBao |

## 5. Связи каталогов внутри 910

В конфигурации LXC 910 области подключаются как две точки монтирования каталогов:

```text
mp0: /mnt/bindmounts/infra-manager/access,mp=/mnt/pve-access,ro=1
mp1: /mnt/bindmounts/infra-manager/state,mp=/mnt/persistent-state
```

`mp0` обязательно доступен только для чтения. `mp1` доступен 910 для чтения и записи. `pve-only/` не имеет mount point внутри 910.

Постоянные каталоги из `state/` доступны внутри 910 через следующие рабочие пути:

| Источник внутри 910 | Рабочий путь |
|---|---|
| `/mnt/persistent-state/ansible` | `/etc/infra-manager/ansible` |
| `/mnt/persistent-state/semaphore` | `/var/lib/infra-manager/semaphore` |
| `/mnt/persistent-state/opentofu` | `/var/lib/infra-manager/opentofu` |
| `/mnt/persistent-state/locks` | `/var/lib/infra-manager/locks` |
| `/mnt/persistent-state/openbao` | `/var/lib/persistent/openbao` |

Основной файл состояния OpenTofu доступен внутри 910 по пути:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Эти пути являются рабочими путями 910. Физические данные при этом находятся в `state/` на PVE.

### 5.1. Права верхнего уровня на PVE

910 является непривилегированным LXC, поэтому числовые владельцы на PVE учитывают отображение UID/GID контейнера.

| Путь на PVE | Режим | Владелец на PVE |
|---|---:|---:|
| `/mnt/bindmounts/infra-manager/` | `0755` | `0:0` |
| `pve-only/` | `0700` | `0:0` |
| `pve-only/openbao/` | `0700` | `0:0` |
| `pve-only/recovery/` | `0700` | `0:0` |
| `access/` | `0755` | `0:0` |
| `access/pve-host/` | `0700` | `101001:100000` |
| `access/ca/` | `0755` | `100000:100000` |
| `state/` | `0700` | `100000:100000` |

Основные файлы доступа имеют следующие режимы:

| Файл | Режим | Владелец на PVE |
|---|---:|---:|
| `pve-only/openbao/unseal.key` | `0600` | `0:0` |
| `pve-only/openbao/ssh-access.json` | `0600` | `0:0` |
| `pve-only/openbao/kv-access.json` | `0600` | `0:0` |
| `pve-only/openbao/operator-access.json` | `0600` | `0:0` |
| `pve-only/recovery/github_proxmox_repo_ed25519` | `0600` | `0:0` |
| `access/pve-host/root_ed25519` | `0600` | `101001:100000` |
| `access/pve-host/root_ed25519.pub` | `0644` | `101001:100000` |
| `access/pve-host/known_hosts` | `0644` | `101001:100000` |
| `access/ca/pve-root-ca.crt` | `0644` | `100000:100000` |

Права внутренних каталогов `state/`, которые привязываются к рабочим путям 910, задаются машинным контрактом `provision.yaml` и здесь повторно не описываются.

## 6. Другие файлы на PVE

Штатный корневой сертификат Proxmox:

```text
/etc/pve/pve-root-ca.pem
```

Открытый ключ SSH-сервера PVE, из которого формируется доверенная запись для 910:

```text
/etc/ssh/ssh_host_ed25519_key.pub
```

Открытый root SSH-ключ управляющего контура добавляется с проектной меткой в:

```text
/root/.ssh/authorized_keys
```

Операторская команда PVE:

```text
/usr/local/sbin/infra-manager
```

является минимальной оболочкой и единственной штатной точкой входа человека. Пользовательский набор команд остаётся:

```text
guests
status [VMID]
deploy VMID
sync VMID
repair [VMID]
test VMID
recover
```

Для всех команд кроме `recover` оболочка только читает VMID управляющего LXC из `/etc/infra-manager/openbao-host.json`, проверяет проектную метку объекта и передаёт аргументы через `pct exec` операторскому слою внутри 910. `update` синхронизирует уже существующую Git-копию внутри 910, а гостевые операции выполняются общим диспетчером напрямую. OpenTofu, Ansible, Semaphore, Docker и обычная логика состояния на PVE не размещаются.

`repair` без VMID может запустить остановленный управляющий LXC перед передачей команды внутрь него. `recover` выполняется отдельно PVE-only recovery helper и поэтому не зависит от состояния 910.

Низкоуровневые PVE-механизмы сохраняются отдельно:

```text
/usr/local/sbin/infra-manager-openbao-unseal
/usr/local/sbin/infra-manager-recovery
```

Они считаются внутренней реализацией. `infra-manager-openbao-unseal` выполняет только узкие PVE-only операции OpenBao по запросу 910 и bootstrap. `infra-manager-recovery` хранит независимую аварийную логику: проверку постоянного состояния, восстановление bootstrap Git-доступа и запуск bootstrap recovery.

Технический журнал первоначального контура:

```text
/var/log/proxmox-bootstrap.log
```

Он содержит диагностический вывод первоначальной подготовки и не является источником требуемого состояния.

Эти объекты не заменяют каноническое постоянное хранилище `/mnt/bindmounts/infra-manager/`.

## 7. Каталог первоначальной подготовки

Текущая реализация первоначального контура также использует:

```text
/root/.config/proxmox-bootstrap/
```

В нём могут находиться:

```text
github_proxmox_repo_ed25519
debian13-template.ref
```

Исходный GitHub Deploy Key создаётся первоначальным контуром в этом каталоге. Каноническая аварийная копия сохраняется в `pve-only/recovery/`. Постоянная копия ключа в `access/` не создаётся.

Каталог `/root/.config/proxmox-bootstrap/` не является каноническим хранилищем recovery-данных. Его Git key восстанавливается автоматически при `infra-manager recover`; внутренний helper `infra-manager-recovery` оператору напрямую не требуется.

## 8. Временные файлы

Блокировки PVE-only сценариев:

```text
/run/lock/infra-manager-openbao-unseal.lock
/run/lock/infra-manager-recovery.lock
```

Сам хостовый сценарий может создавать краткоживущие временные файлы на PVE под `/run/`; их имена не являются частью постоянного контракта.

Внутри LXC 910 рабочие секреты OpenBao материализуются в:

```text
/run/infra-manager/secrets/
├── pve-api.env
├── github_proxmox_repo_ed25519
├── semaphore-server.env
├── initial-admin-password
├── semaphore-api-token
└── .openbao-materialized
```

Каталог находится под `/run`, не является постоянным состоянием и после перезапуска создаётся заново из OpenBao.

Во время первоначального создания или восстановления 910 bootstrap может временно использовать:

```text
/run/infra-manager/bootstrap-secrets/
├── pve-api.env
└── github_proxmox_repo_ed25519
```

Эта область существует только до успешной инициализации OpenBao. После переноса секретов в OpenBao bootstrap удаляет её полностью.

`infra-runtime` получает только `/run/infra-manager/secrets/` и только для чтения.

Маркер `.openbao-materialized` не содержит секретных данных.

Другие временные файлы PVE-only механизма также могут создаваться под `/run/infra-manager/` и удаляются после операции.

## 9. Связанные документы

- [`200-overview.md`](200-overview.md) — общая роль PVE-хоста.
- [`210-host-bootstrap.md`](210-host-bootstrap.md) — первоначальный контур.
- [`220-host-configuration.md`](220-host-configuration.md) — обязательные свойства постоянного состояния.
- [`290-decisions.md`](290-decisions.md) — причины разделения областей состояния.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — назначение данных административного доступа.
- [`../800-operations/830-recovery.md`](../800-operations/830-recovery.md) — восстановление и резервное копирование.
- [`../../infrastructure/guests/910-infra-manager/README.md`](../../infrastructure/guests/910-infra-manager/README.md) — рабочие пути и внутреннее устройство 910.
