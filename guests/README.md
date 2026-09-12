# Guests

Каждый каталог соответствует одной VM/LXC и собирает в одном месте её документацию, машинно-читаемый паспорт и файлы, которые должны быть развернуты на этом госте.

Правило именования:

```text
<VMID>-<name>/
```

Актуальная целевая нумерация определяется только [`../docs/vmid-plan.md`](../docs/vmid-plan.md).

## Базовая структура

```text
<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/        # при необходимости
├── STATUS.md         # при необходимости
└── rootfs/
```

- `README.md` — назначение, зависимости и особенности гостя;
- `guest.yaml` — машинно-читаемый паспорт VM/LXC и желаемые параметры в Proxmox;
- `decisions/` — ADR конкретного гостя;
- `STATUS.md` — временное состояние длинной работы или миграции;
- `rootfs/` — дерево файлов, соответствующее корневой файловой системе гостя.

Формат `guest.yaml` утверждён как schema version 1. Не следует писать в README, что формат ещё «будет введён».

## `guest.yaml`

`guest.yaml` описывает сам объект VM/LXC в Proxmox, но не содержимое его ОС.

```text
guest.yaml
→ VMID, имя, тип, ресурсы, сеть, автозапуск, способ управления

rootfs/
→ /etc, /opt, приложения, Compose, конфиги и собственный код внутри гостя
```

### Минимальный формат

```yaml
schema_version: 1
vmid: 101
name: network-gateway
type: vm
```

Обязательные поля:

- `schema_version` — версия схемы, сейчас `1`;
- `vmid` — VMID/CTID; должен совпадать с номером каталога;
- `name` — имя гостя; должно совпадать с именем в каталоге;
- `type` — `vm` или `lxc`.

Допустимые дополнительные поля добавляются только тогда, когда ими реально нужно управлять.

Пример расширенного описания:

```yaml
schema_version: 1
vmid: 101
name: network-gateway
type: vm
node: pve

description: Network gateway, DNS, VPN and policy routing

resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 16
    storage: local-lvm

network:
  bridge: vmbr0
  ipv4:
    method: dhcp

boot:
  onboot: true

management:
  ssh:
    user: infra-agent
    port: 22
```

Значения в примере демонстрируют формат. Фактические параметры каждого гостя задаются только в его собственном `guest.yaml`.

### Ресурсы

При необходимости используются поля:

```yaml
resources:
  cpu:
    cores: 4
    type: host
  memory_mb: 8192
  ballooning_mb: 4096
  disk:
    size_gb: 64
    storage: local-lvm
    discard: true
```

Не следует копировать все значения Proxmox по умолчанию. В `guest.yaml` фиксируются параметры, важные для воспроизводимого создания или сознательно контролируемые проектом.

### Сеть

Простой вариант:

```yaml
network:
  bridge: vmbr0
  ipv4:
    method: dhcp
```

Статическая конфигурация:

```yaml
network:
  bridge: vmbr0
  ipv4:
    method: static
    address: 192.168.10.21/24
    gateway: 192.168.10.1
```

VLAN при необходимости:

```yaml
network:
  bridge: vmbr0
  vlan: 20
  ipv4:
    method: static
    address: 192.168.20.21/24
    gateway: 192.168.20.1
```

Если гостю потребуется несколько интерфейсов, схема может быть расширена до `network.interfaces`. Не усложнять её заранее без реального сценария.

### Автозапуск

```yaml
boot:
  onboot: true
```

Startup order не выводится из VMID и задаётся отдельно при необходимости.

### Управление

```yaml
management:
  ssh:
    user: infra-agent
    port: 22
```

Приватные SSH-ключи, пароли и токены в `guest.yaml` не хранятся.

### ОС и источник создания

При автоматическом создании можно добавить:

```yaml
os:
  family: debian
  version: "13"
  source:
    type: template
    template: debian-13-cloud
```

или:

```yaml
os:
  family: debian
  source:
    type: clone
    vmid: 9000
```

### VM/LXC-специфичные параметры

```yaml
vm:
  machine: q35
  bios: ovmf
  guest_agent: true
```

или:

```yaml
lxc:
  unprivileged: true
  nesting: true
```

Пустые секции не добавляются.

### Что не хранить в `guest.yaml`

Не помещать туда:

- пароли и API tokens;
- приватные SSH-ключи;
- содержимое файлов `/etc`, `/opt` и других путей гостевой ОС;
- Docker Compose и конфиги приложений;
- runtime-состояние;
- автоматически обнаруживаемые данные, которыми проект не управляет.

`guest.yaml` описывает желаемое состояние, а не является полным экспортом конфигурации Proxmox.

## Правило `rootfs/`

`rootfs/` является зеркалом корневой файловой системы гостя. Всё после `rootfs/` разворачивается относительно `/`.

```text
guests/101-network-gateway/rootfs/etc/nftables.conf
→ /etc/nftables.conf
```

Тот же принцип используется для приложений:

```text
guests/301-ai-control/rootfs/opt/ai-control/agents/hermes/
→ /opt/ai-control/agents/hermes/
```

Отдельного каталога `services/` нет. Код приложений, Dockerfile, Compose-файлы, MCP, агенты, скрипты и системные конфиги хранятся внутри `rootfs/` по реальному пути назначения.

Не создавать отдельные верхнеуровневые каталоги вроде `dns/`, `vpn/`, `nftables/` или `docker/`, если это файлы гостевой ОС. Они должны оказаться в соответствующем месте `rootfs/`.

Сам Git-репозиторий целиком на гостя копировать не требуется. Управляющий контур берёт каталог нужного гостя и синхронизирует только его файлы.

## Управление VM/LXC

Принято простое разделение:

```text
Proxmox MCP
→ создание и жизненный цикл VM/LXC

SSH
→ всё внутри гостевой ОС
```

Через Proxmox MCP выполняются создание, CPU/RAM/disk/network, start/stop/reboot, snapshots и backups. Через SSH выполняются установка пакетов, изменение файлов, управление `systemd`, Docker/Compose и приложениями.

Отдельный универсальный deploy-MCP не требуется. Ansible может использоваться поверх SSH для повторяемых процедур, но не является обязательным слоем.

## Текущий bootstrap AI Control

`320-ai-control` является временным bootstrap-узлом. Целевой управляющий гость — `301-ai-control`. До завершения миграции оба каталога могут существовать одновременно, но это не означает наличие двух равноправных целевых control plane.

## Общие правила

- `proxmox` является основным monorepo инфраструктуры;
- общие Ansible roles/playbooks не дублируются по гостям;
- общие шаблоны хранятся в `/templates`;
- не создавать фиктивные `compose.yaml`: файл появляется вместе с реальной конфигурацией;
- отдельный репозиторий создаётся только при реальной независимости компонента;
- секреты, токены, пароли и приватные ключи в Git не добавляются.
