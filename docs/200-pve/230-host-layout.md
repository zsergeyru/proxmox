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
└── openbao/
    ├── unseal.key
    └── ssh-access.json
```

| Путь | Назначение |
|---|---|
| `pve-only/openbao/unseal.key` | ключ снятия блокировки OpenBao |
| `pve-only/openbao/ssh-access.json` | служебные данные ограниченного доступа к операциям SSH-подписи OpenBao |

Весь каталог `pve-only/` остаётся только на PVE и в 910 не монтируется.

## 3. Данные PVE для чтения из 910

Текущая структура:

```text
/mnt/bindmounts/infra-manager/access/
├── github/
│   └── github_proxmox_repo_ed25519
├── pve-host/
│   ├── root_ed25519
│   └── known_hosts
├── pve-api/
│   └── pve-api.env
└── ca/
    └── pve-root-ca.crt
```

Назначение:

| Путь | Назначение |
|---|---|
| `access/github/github_proxmox_repo_ed25519` | read-only Deploy Key репозитория проекта |
| `access/pve-host/root_ed25519` | закрытый root SSH-ключ 910 для PVE |
| `access/pve-host/known_hosts` | доверенный SSH host key PVE |
| `access/pve-api/pve-api.env` | данные постоянного API-доступа 910 |
| `access/ca/pve-root-ca.crt` | копия корневого сертификата PVE |

Весь каталог подключается в 910 как:

```text
/mnt/bindmounts/infra-manager/access/
→ /mnt/pve-access/
```

Режим — только чтение.

## 4. Изменяемое состояние 910

Текущая структура верхнего уровня:

```text
/mnt/bindmounts/infra-manager/state/
├── secrets/
├── ansible/
├── semaphore/
├── opentofu/
└── openbao/
```

Назначение:

| Каталог | Назначение |
|---|---|
| `state/secrets/` | постоянные секреты 910 |
| `state/ansible/` | постоянная Ansible-идентичность |
| `state/semaphore/` | база и служебные данные Semaphore |
| `state/opentofu/` | постоянные данные и состояние OpenTofu |
| `state/openbao/` | постоянные Raft-данные OpenBao |

Весь каталог подключается в 910 как:

```text
/mnt/bindmounts/infra-manager/state/
→ /mnt/persistent-state/
```

Режим — чтение и запись.

## 5. Связи каталогов внутри 910

Постоянные каталоги из `state/` доступны внутри 910 через следующие рабочие пути:

| Источник внутри 910 | Рабочий путь |
|---|---|
| `/mnt/persistent-state/secrets` | `/etc/infra-manager/secrets` |
| `/mnt/persistent-state/ansible` | `/etc/infra-manager/ansible` |
| `/mnt/persistent-state/semaphore` | `/var/lib/infra-manager/semaphore` |
| `/mnt/persistent-state/opentofu` | `/var/lib/infra-manager/opentofu` |
| `/mnt/persistent-state/openbao` | `/var/lib/persistent/openbao` |

Основной файл состояния OpenTofu доступен внутри 910 по пути:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Эти пути являются рабочими путями 910. Физические данные при этом находятся в `state/` на PVE.

## 6. Другие файлы на PVE

Штатный корневой сертификат Proxmox:

```text
/etc/pve/pve-root-ca.pem
```

Открытый root SSH-ключ управляющего контура добавляется с проектной меткой в:

```text
/root/.ssh/authorized_keys
```

Хостовый сценарий для операций разблокировки OpenBao:

```text
/usr/local/sbin/infra-manager-openbao-unseal
```

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

Исходный GitHub Deploy Key создаётся первоначальным контуром в этом каталоге, а рабочая копия для штатного 910 помещается в `access/github/`.

Этот каталог не является хранилищем состояния сервисов 910 и может очищаться отдельным режимом полной очистки первоначального контура без удаления канонического корня постоянных данных.

## 8. Временные файлы

Блокировка хостового сценария OpenBao:

```text
/run/lock/infra-manager-openbao-unseal.lock
```

Временные файлы для передачи данных хостового сценария создаются под:

```text
/run/infra-manager/
```

Они не являются постоянным состоянием и должны удаляться после завершения операции.

Старые пути `/run/proxmox-bootstrap/`, `/run/lock/proxmox-bootstrap.lock` и `/run/lock/proxmox-orchestration.lock` не входят в действующий контракт.

## 9. Связанные документы

- [`200-overview.md`](200-overview.md) — общая роль PVE-хоста.
- [`210-host-bootstrap.md`](210-host-bootstrap.md) — первоначальный контур.
- [`220-host-configuration.md`](220-host-configuration.md) — обязательные свойства постоянного состояния.
- [`290-decisions.md`](290-decisions.md) — причины разделения областей состояния.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — назначение данных административного доступа.
- [`../800-operations/830-recovery.md`](../800-operations/830-recovery.md) — восстановление и резервное копирование.
- [`../../infrastructure/guests/910-infra-manager/README.md`](../../infrastructure/guests/910-infra-manager/README.md) — рабочие пути и внутреннее устройство 910.
