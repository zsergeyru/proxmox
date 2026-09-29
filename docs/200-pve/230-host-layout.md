# Структура PVE-хоста

**Тип:** справочник  
**Статус:** действующий  
**Назначение:** зафиксировать постоянные каталоги, файлы и объекты проекта, которые находятся непосредственно на PVE.

## 1. Основной принцип

На PVE нет постоянной закрытой Git-копии проекта и не работает отдельный движок развёртывания.

При этом PVE хранит постоянные данные, необходимые для работы и восстановления `910 infra-manager`.

Каноническая схема:

```text
PVE
├── штатные объекты Proxmox
├── LXC 910 infra-manager
├── доступы 910 к PVE
└── /mnt/bindmounts/infra-manager/
    ├── pve-only/
    ├── access/
    └── state/
```

Средства управления этими данными работают внутри 910. PVE предоставляет хранилище, bind mount и узкие host-only операции.

## 2. Канонический корень постоянных данных

Постоянные данные 910 физически находятся на PVE:

```text
/mnt/bindmounts/infra-manager/
├── pve-only/
│   └── openbao/
│       ├── unseal.key
│       └── ssh-access.json
├── access/
│   ├── github/
│   │   └── github_proxmox_repo_ed25519
│   ├── pve-host/
│   │   ├── root_ed25519
│   │   └── known_hosts
│   ├── pve-api/
│   │   └── pve-api.env
│   └── ca/
│       └── pve-root-ca.crt
└── state/
    ├── secrets/
    ├── ansible/
    ├── semaphore/
    ├── opentofu/
    └── openbao/
```

Состав отдельных подкаталогов может расширяться, но назначение трёх верхних областей фиксировано.

### 2.1. pve-only

`pve-only/` содержит данные, которые 910 не должен видеть даже при полном штатном доступе к своим постоянным данным.

Сейчас сюда относятся:

```text
/mnt/bindmounts/infra-manager/pve-only/openbao/unseal.key
/mnt/bindmounts/infra-manager/pve-only/openbao/ssh-access.json
```

Каталог `pve-only/` в 910 не монтируется.

### 2.2. access

`access/` содержит данные, владельцем которых является PVE, но которые необходимы 910 для работы.

Он подключается в 910 только для чтения:

```text
/mnt/bindmounts/infra-manager/access/
→
/mnt/pve-access/
```

Основные соответствия:

```text
access/github/github_proxmox_repo_ed25519
→ /mnt/pve-access/github/github_proxmox_repo_ed25519

access/pve-host/root_ed25519
→ /mnt/pve-access/pve-host/root_ed25519

access/pve-host/known_hosts
→ /mnt/pve-access/pve-host/known_hosts

access/pve-api/pve-api.env
→ /mnt/pve-access/pve-api/pve-api.env

access/ca/pve-root-ca.crt
→ /mnt/pve-access/ca/pve-root-ca.crt
```

### 2.3. state

`state/` содержит постоянное состояние сервисов 910.

Он подключается в 910 для чтения и записи:

```text
/mnt/bindmounts/infra-manager/state/
→
/mnt/persistent-state/
```

Из него внутри 910 используются постоянные пути:

```text
/mnt/persistent-state/secrets
→ /etc/infra-manager/secrets

/mnt/persistent-state/ansible
→ /etc/infra-manager/ansible

/mnt/persistent-state/semaphore
→ /var/lib/infra-manager/semaphore

/mnt/persistent-state/opentofu
→ /var/lib/infra-manager/opentofu

/mnt/persistent-state/openbao
→ /var/lib/persistent/openbao
```

Поэтому привычные пути внутри 910 могут выглядеть локальными, хотя их данные физически находятся на PVE.

## 3. GitHub Deploy Key

Для первоначального получения закрытого проекта публичный сценарий создаёт read-only GitHub Deploy Key.

Сейчас исходный ключ первоначальной подготовки может сохраняться по прежнему пути:

```text
/root/.config/proxmox-bootstrap/github_proxmox_repo_ed25519
```

Рабочая копия для штатного 910 хранится в:

```text
/mnt/bindmounts/infra-manager/access/github/github_proxmox_repo_ed25519
```

и доступна 910 только для чтения через `/mnt/pve-access/github/`.

Закрытая Git-копия проекта на PVE постоянно не хранится.

Каталог `/root/.config/proxmox-bootstrap/` не является каноническим хранилищем состояния 910. Режим полной очистки старого первоначального контура может удалить его, не удаляя `/mnt/bindmounts/infra-manager/`.

## 4. Штатные файлы и объекты Proxmox

Из штатных файлов Proxmox используется корневой сертификат:

```text
/etc/pve/pve-root-ca.pem
```

Постоянные объекты проекта в Proxmox:

```text
LXC 910 infra-manager
pool managed
API token root@pam!infra-manager с privsep=0
Debian 13 LXC template в local:vztmpl
```

Отдельный пользователь PVE, отдельная роль и отдельная ACL-матрица для 910 не используются.

910 находится вне pool `managed`.

Для root SSH-доступа 910 открытый проектный ключ присутствует в:

```text
/root/.ssh/authorized_keys
```

Закрытая часть этого ключа находится в `access/pve-host/root_ed25519`.

## 5. Host-only файлы проекта

После настройки OpenBao на PVE существует узкий служебный сценарий:

```text
/usr/local/sbin/infra-manager-openbao-unseal
```

Он используется 910 через отдельный root SSH-доступ для операций OpenBao, которые намеренно остаются только на стороне PVE.

Для взаимного исключения используется:

```text
/run/lock/infra-manager-openbao-unseal.lock
```

Постоянная служба или периодический таймер OpenBao на PVE не требуются. Штатный вызов выполняется из 910.

## 6. Чего на PVE больше нет в текущей схеме

Не являются частью действующего контура:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/lib/pvedeploy/
/var/log/proxmox-deployer/

/usr/local/sbin/pve-configuration-status
/usr/local/sbin/deploy-guest
/usr/local/sbin/sync-management-keys
```

Также не используются как действующий контракт:

```text
/run/proxmox-bootstrap/
/run/lock/proxmox-bootstrap.lock
/run/lock/proxmox-orchestration.lock
```

Эти пути могут встречаться только в коде очистки или исторической документации.

На PVE не устанавливаются как постоянные средства управления:

- Docker;
- Semaphore;
- OpenTofu;
- Ansible;
- Packer.

На PVE также нет:

- Linux-пользователя `pvedeploy`;
- общего реестра management SSH-ключей;
- `management-authorized-keys`;
- постоянной закрытой Git-копии проекта;
- отдельного состояния PVE Configuration.

При этом OpenTofu state 910 физически хранится на PVE внутри `state/opentofu/`. Это хранилище данных 910, а не отдельный OpenTofu-контур на самом PVE.

## 7. Что остаётся локальным внутри 910

Воспроизводимые данные остаются в rootfs 910 и при необходимости создаются заново:

```text
/var/lib/infra-manager/bootstrap-repo/
/opt/infra-manager/compose/
/usr/local/lib/infra-manager/
Docker images
кэши
временные файлы
```

Постоянные пути внутри 910:

```text
/etc/infra-manager/secrets/
/etc/infra-manager/ansible/
/var/lib/infra-manager/semaphore/
/var/lib/infra-manager/opentofu/
/var/lib/persistent/openbao/
```

имеют постоянную основу в `state/` на PVE.

Вынос этих данных на PVE защищает их от пересоздания rootfs 910, но не заменяет резервное копирование.

## 8. Связанные документы

- [`210-host-bootstrap.md`](210-host-bootstrap.md) — создание и восстановление 910.
- [`220-host-configuration.md`](220-host-configuration.md) — минимальный контракт PVE.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — API-токен и root SSH.
- [`../800-operations/830-recovery.md`](../800-operations/830-recovery.md) — восстановление постоянных данных.
- [`../../infrastructure/guests/910-infra-manager/README.md`](../../infrastructure/guests/910-infra-manager/README.md) — постоянные данные и работа 910.
