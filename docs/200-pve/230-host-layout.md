# Структура PVE-хоста

**Тип:** справочник  
**Статус:** действующий  
**Назначение:** зафиксировать точные пути постоянных данных, корня восстановления и подключений управляющего контура на физическом PVE.

## Содержание

- [1. Канонический корень](#1-канонический-корень)
- [2. Область только для PVE](#2-область-только-для-pve)
- [3. Recovery-область](#3-recovery-область)
- [4. Данные PVE для чтения из 910](#4-данные-pve-для-чтения-из-910)
- [5. Изменяемое состояние 910](#5-изменяемое-состояние-910)
- [6. Подключения в 910 и OpenBao](#6-подключения-в-910-и-openbao)
- [7. Права доступа](#7-права-доступа)
- [8. Другие файлы PVE](#8-другие-файлы-pve)
- [9. Временные данные](#9-временные-данные)
- [10. Связанные документы](#10-связанные-документы)

## 1. Канонический корень

Постоянные данные проекта на PVE находятся под:

~~~text
/mnt/bindmounts/infra-manager/
├── pve-only/
│   ├── openbao/
│   └── recovery/
│       ├── ssh/
│       └── github/
├── access/
│   ├── pve-host/
│   └── ca/
└── state/
    ├── openbao/
    ├── opentofu/
    └── semaphore/
~~~

| Область | Доступ из 910 | Назначение |
|---|---|---|
| `pve-only/` | нет | корень управления OpenBao и независимое аварийное восстановление |
| `access/` | только чтение | PVE-owned данные, которые штатно нужны 910 |
| `state/` | чтение и запись | постоянное состояние сервисов 910 |

Рабочие PVE API, Git и сервисные secrets в целевой модели не хранятся отдельными файлами в `access/`; их рабочее представление находится в OpenBao KV внутри `state/openbao/`.

## 2. Область только для PVE

~~~text
/mnt/bindmounts/infra-manager/pve-only/openbao/
├── unseal.key
└── control-access.json
~~~

| Путь | Назначение |
|---|---|
| `pve-only/openbao/unseal.key` | ключ снятия блокировки соответствующего OpenBao state |
| `pve-only/openbao/control-access.json` | ограниченные служебные данные, позволяющие PVE настраивать или восстанавливать OpenBao |

Эти файлы никогда не монтируются в 910 постоянно.

`control-access.json` заменяет узкое представление, ориентированное только на SSH-подпись: OpenBao теперь обслуживает KV, SSH и централизованные policies.

## 3. Recovery-область

~~~text
/mnt/bindmounts/infra-manager/pve-only/recovery/
├── ssh/
│   ├── linux-admin-ed25519
│   └── linux-admin-ed25519.pub
└── github/
    ├── project-read-ed25519
    └── project-read-ed25519.pub
~~~

### 3.1. `recovery/ssh/`

`linux-admin-ed25519` — аварийный административный SSH private key для управляемых Linux-систем.

Его public key устанавливается на управляемые Linux-гости как отдельная проектная recovery-запись.

Private key:

- не монтируется в 910;
- не хранится в OpenBao;
- не используется штатным Ansible;
- применяется только явной процедурой восстановления.

### 3.2. `recovery/github/`

`project-read-ed25519` — защищённая восстановительная копия read-only GitHub Deploy Key.

Она нужна, чтобы получить проект до запуска или после полной потери OpenBao.

Рабочее представление этой же пары хранится в OpenBao KV. Recovery-копия не выдаётся обычным гостям.

## 4. Данные PVE для чтения из 910

~~~text
/mnt/bindmounts/infra-manager/access/
├── pve-host/
│   ├── root_ed25519
│   ├── root_ed25519.pub
│   └── known_hosts
└── ca/
    └── pve-root-ca.crt
~~~

| Путь на PVE | Путь внутри 910 | Назначение |
|---|---|---|
| `access/pve-host/root_ed25519` | `/mnt/pve-access/pve-host/root_ed25519` | отдельный root SSH private key `infra-manager → PVE` |
| `access/pve-host/root_ed25519.pub` | `/mnt/pve-access/pve-host/root_ed25519.pub` | public key той же пары |
| `access/pve-host/known_hosts` | `/mnt/pve-access/pve-host/known_hosts` | строгая проверка SSH-сервера PVE |
| `access/ca/pve-root-ca.crt` | `/mnt/pve-access/ca/pve-root-ca.crt` | доверенный корневой сертификат PVE API |

Root SSH identity остаётся вне OpenBao, потому что она участвует в PVE-only операциях и восстановлении самого OpenBao.

## 5. Изменяемое состояние 910

~~~text
/mnt/bindmounts/infra-manager/state/
├── openbao/
├── opentofu/
└── semaphore/
~~~

| Путь на PVE | Рабочий путь внутри 910 | Назначение |
|---|---|---|
| `state/openbao/` | `/var/lib/persistent/openbao/` | вся постоянная Raft-база OpenBao: KV, SSH CA, policies, auth methods |
| `state/opentofu/` | `/var/lib/infra-manager/opentofu/` | постоянное состояние OpenTofu |
| `state/semaphore/` | `/var/lib/infra-manager/semaphore/` | база и служебное состояние Semaphore |

Основной OpenTofu state доступен как:

~~~text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
~~~

Секреты OpenBao не выделяются в отдельный PVE-каталог: KV физически является частью `state/openbao/`.

## 6. Подключения в 910 и OpenBao

В LXC 910 подключаются только `access/` и `state/`:

~~~text
mp0: /mnt/bindmounts/infra-manager/access,mp=/mnt/pve-access,ro=1
mp1: /mnt/bindmounts/infra-manager/state,mp=/mnt/persistent-state
~~~

`pve-only/` не имеет mount point внутри 910.

Путь OpenBao проходит через несколько уровней без копирования базы:

~~~text
PVE
/mnt/bindmounts/infra-manager/state/openbao/
        ↓ bind mount

LXC 910
/mnt/persistent-state/openbao/
        ↓ рабочая привязка

/var/lib/persistent/openbao/
        ↓ Docker volume

OpenBao
~~~

Удаление или пересоздание rootfs 910 или контейнера OpenBao не должно удалять `state/openbao/`.

## 7. Права доступа

Верхний уровень:

| Путь | Режим | Владелец на PVE |
|---|---:|---:|
| `/mnt/bindmounts/infra-manager/` | `0755` | `0:0` |
| `pve-only/` | `0700` | `0:0` |
| `pve-only/openbao/` | `0700` | `0:0` |
| `pve-only/recovery/` | `0700` | `0:0` |
| `pve-only/recovery/ssh/` | `0700` | `0:0` |
| `pve-only/recovery/github/` | `0700` | `0:0` |
| `access/` | `0755` | `0:0` |
| `access/pve-host/` | `0700` | `101001:100000` |
| `access/ca/` | `0755` | `100000:100000` |
| `state/` | `0700` | `100000:100000` |

Закрытые PVE-only и recovery keys имеют режим `0600`; их public keys — `0644`.

Точные владельцы внутренних каталогов `state/`, отображаемых в непривилегированный LXC, задаются машинным контрактом 910.

## 8. Другие файлы PVE

Штатный PVE CA:

~~~text
/etc/pve/pve-root-ca.pem
~~~

Открытый host key PVE:

~~~text
/etc/ssh/ssh_host_ed25519_key.pub
~~~

Public key `access/pve-host/root_ed25519.pub` регистрируется в:

~~~text
/root/.ssh/authorized_keys
~~~

Хостовый служебный механизм OpenBao выполняет только операции, которым нужны полномочия PVE или `pve-only/`. Он не становится вторым общим средством развёртывания.

## 9. Временные данные

Временные private keys Ansible, сертификаты, OpenBao tokens, SSH OTP и служебные файлы под `/run/` не относятся к постоянному состоянию и не резервируются.

Git checkout, Docker images, кэши и воспроизводимый установленный код также не входят в канонический корень постоянных данных.

## 10. Связанные документы

- [`220-host-configuration.md`](220-host-configuration.md) — обязательные свойства постоянного состояния.
- [`../600-storage/610-backup.md`](../600-storage/610-backup.md) — что из этой структуры резервируется.
- [`../700-security/720-ssh-access.md`](../700-security/720-ssh-access.md) — назначение SSH keys.
- [`../700-security/740-openbao.md`](../700-security/740-openbao.md) — содержимое OpenBao state.
- [`../800-operations/830-recovery.md`](../800-operations/830-recovery.md) — использование recovery-данных.
