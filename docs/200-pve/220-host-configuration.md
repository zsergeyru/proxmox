# Постоянное состояние PVE-хоста

**Тип:** спецификация  
**Статус:** проектируется  
**Назначение:** зафиксировать минимальное постоянное состояние самого PVE.

## 1. Основной принцип

PVE должен оставаться максимально близким к штатной установке Proxmox VE.

На физическом PVE не размещаются закрытый Git-репозиторий, инфраструктурный исполнитель или постоянные инструменты развёртывания.

```text
PVE
├─ штатная конфигурация Proxmox
├─ минимальные объекты доступа
└─ LXC 910 infra-deployer
```

## 2. Обязательное состояние

### 2.1. Сеть и хранилища

Должны существовать:

```text
vmbr0
local
local-lvm
```

`local` хранит LXC template.

`local-lvm` используется для корневого диска 910 и дисков обычных управляемых гостей.

Имя PVE-узла должно разрешаться в его локальный адрес.

### 2.2. Debian LXC template

На `local` должен быть доступен Debian 13 standard LXC template, достаточный для создания 910.

Если его нет, public bootstrap получает его автоматически.

Это не VM template `9000`.

### 2.3. 910 infra-deployer

На PVE существует специальный LXC:

```text
VMID 910
hostname infra-deployer
```

Его виртуальным объектом владеет public bootstrap.

910:

- unprivileged;
- имеет `nesting=1,keyctl=1`;
- запускается вместе с PVE;
- защищён `protection=1`;
- не входит в pool `managed`.

### 2.4. Управляемая область

Для обычных VM/LXC существует:

```text
pool managed
```

Это граница изменяющих прав инфраструктурной автоматизации.

### 2.5. PVE API token

Для 910 существует:

```text
root@pam!infra-deployer
```

с `privsep=1`.

Отдельный пользователь `infra-deployer@pve` не создаётся.

Token получает только прямые ACL, описанные в разделе безопасности.

## 3. Чего на PVE не должно требоваться

Новая архитектура не требует:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/lib/pvedeploy/
/var/log/proxmox-deployer/

pvedeploy
deploy-guest
sync-management-keys
pve-configuration-status
```

Также на PVE не должны храниться:

- закрытая Git-копия проекта;
- GitHub Deploy Key проекта;
- OpenTofu state;
- Semaphore;
- Docker;
- Ansible;
- Packer.

Оставшиеся от старой реализации объекты удаляются как наследие после подтверждённого перехода.

## 4. Что PVE не управляет

OpenTofu внутри 910 не управляет:

- физическим PVE;
- объектом 910;
- своим PVE API token;
- ACL самого token;
- базовыми хранилищами и мостом PVE.

Эта минимальная основа принадлежит public bootstrap и штатной конфигурации Proxmox.

## 5. Проверка

Проверка PVE должна подтверждать:

- наличие PVE;
- `vmbr0`;
- `local` и `local-lvm`;
- принадлежность VMID 910;
- запуск 910;
- pool `managed`;
- наличие `root@pam!infra-deployer`;
- `privsep=1`;
- успешную внутреннюю проверку 910.

Детальная проверка Semaphore, Runner, OpenTofu и других средств выполняется внутри 910 командой `infra-deployer-status`.

## 6. Повторное применение

Повторный public bootstrap:

- повторно использует существующий корректный 910;
- не пересоздаёт его;
- не перевыпускает token без причины;
- обновляет закрытый проект внутри 910;
- передаёт управление внутреннему setup только при новой ревизии;
- останавливается при неоднозначном состоянии.

## 7. Связанные документы

- [`210-host-bootstrap.md`](210-host-bootstrap.md) — первоначальная подготовка.
- [`230-host-layout.md`](230-host-layout.md) — локальная структура PVE.
- [`../700-security/710-pve-access.md`](../700-security/710-pve-access.md) — ACL token.
- [`../../guests/910-infra-deployer/README.md`](../../guests/910-infra-deployer/README.md) — паспорт 910.
