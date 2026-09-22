# Структура PVE-хоста

**Тип:** справочник  
**Статус:** проектируется  
**Назначение:** зафиксировать локальные проектные файлы и объекты, которые остаются непосредственно на PVE после перехода к `910 infra-deployer`.

## 1. Основной принцип

На PVE не хранится постоянная копия закрытого репозитория и не работает собственный проектный движок развёртывания.

Локальная структура проекта на PVE должна оставаться минимальной.

## 2. Постоянные области

### 2.1. Резервные копии bootstrap

Для снимков критичной конфигурации PVE используется:

```text
/var/backups/proxmox-bootstrap/
```

Каталог принадлежит `root` и не используется как рабочее состояние развёртывания.

### 2.2. Штатные файлы Proxmox

Bootstrap может читать и резервировать штатные файлы:

```text
/etc/network/interfaces
/etc/hosts
/etc/hostname
/etc/pve/storage.cfg
/etc/pve/user.cfg
/etc/pve/datacenter.cfg
/etc/pve/pve-root-ca.pem
/etc/apt/sources.list
/etc/apt/sources.list.d/
```

Эти пути принадлежат Proxmox/Debian, а не отдельной файловой структуре проекта.

### 2.3. Временная блокировка

Во время запуска используется:

```text
/run/lock/proxmox-bootstrap.lock
```

Это временный runtime-файл и не является постоянным состоянием.

## 3. Объекты Proxmox

К обязательным проектным объектам на PVE относятся:

```text
LXC 910 infra-deployer
API identity infra-deployer@pve!automation
pool managed
проектные роли и ACL
```

Точные права описывает раздел безопасности.

`910` не входит в обычный управляемый пул.

## 4. LXC templates

В `local:vztmpl` должен быть доступен Debian 13 standard LXC template, используемый для создания `910` и других Debian LXC.

Кэш старых LXC templates не является собственным проектным состоянием.

## 5. Что удаляется из старой структуры

Новая архитектура не использует как постоянный контракт:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/lib/pvedeploy/
/var/log/proxmox-deployer/
/usr/local/sbin/pve-configuration-status
/usr/local/sbin/deploy-guest
/usr/local/sbin/sync-management-keys
```

Также не требуются:

- Linux-пользователь `pvedeploy`;
- GitHub Deploy Key закрытого проекта на PVE;
- локальный `config.yaml` разворачивателя;
- локальный OpenTofu state;
- реестр `<VMID>.pub`;
- `management-authorized-keys`;
- собственные журналы PVE Configuration.

Наследованные файлы не удаляются автоматически только из-за появления новой архитектуры. Их очистка выполняется отдельным этапом перехода.

## 6. Где теперь находятся постоянные данные развёртывания

Постоянные данные инфраструктурного контура находятся внутри `910`:

```text
/etc/infra-deployer/
/var/lib/infra-deployer/
/opt/infra-deployer/
```

Их назначение описано в [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md).

## 7. Связанные документы

- [`210-host-bootstrap.md`](210-host-bootstrap.md) — создание и проверка локальной основы.
- [`220-host-configuration.md`](220-host-configuration.md) — обязательное состояние PVE.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — структура и назначение конкретного `910`.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — PVE-идентичности, роли и ACL.