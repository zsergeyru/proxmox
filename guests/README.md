# Guests

Каждый каталог `guests/<VMID>-<name>/` соответствует одной VM/LXC либо зарезервированному объекту и содержит его паспорт, локальные решения и управляемые файлы гостевой ОС.

Актуальная нумерация определяется [`../docs/11-vmid-plan.md`](../docs/11-vmid-plan.md).

Машинно-читаемая спецификация: [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

## Структура

```text
guests/
├── defaults.yaml      # общие deploy defaults, network stages и profiles
├── README.md
└── <VMID>-<name>/
    ├── README.md
    ├── guest.yaml     # индивидуальные параметры/overrides
    ├── decisions/     # при необходимости
    ├── STATUS.md      # observed drift/миграция
    └── rootfs/        # управляемые файлы по реальным абсолютным путям
```

`guest.yaml` обязателен для каждого каталога с VMID. Если параметры ещё не приняты, это выражается через `deployable: false`, а не отсутствием файла.

## guest schema v4

Effective deploy state строится так:

```text
defaults.yaml
→ profile
→ guest.yaml overrides
→ central network gateway
→ effective desired state
```

Все слои находятся в Git. Никаких скрытых deploy defaults внутри `deploy-guest` быть не должно.

### Central defaults

`defaults.yaml` централизует node, обычный pool/protection, disk storage, bridge, network mode/default-gateway selector, current/target subnet и gateway, boot policy, management SSH identity и VM/LXC source profiles.

Сейчас общая сеть остаётся:

```text
mode=current
default_gateway=current
current gateway=192.168.1.1
```

То есть текущий Keenetic не меняется.

### Profiles

Текущие profiles:

```text
debian-vm
→ full clone from VM template 9000
→ QEMU guest agent

docker-lxc
→ pinned Debian 13 LXC appliance
→ unprivileged + nesting + keyctl
```

Если меняется общий LXC archive/template contract, изменение делается в profile один раз.

### Индивидуальный guest

Типичный `311-dev-services/guest.yaml`:

```yaml
schema_version: 4
vmid: 311
name: dev-services
profile: docker-lxc
state: planned
deployable: true

description: Ansible, Semaphore, Git, CI and development services

resources:
  cpu:
    cores: 2
  memory_mb: 4096
  swap_mb: 1024
  disk:
    size_gb: 32

network:
  current:
    ipv4:
      address: 192.168.3.11/16
  target:
    ipv4:
      address: 10.0.3.11/16
```

В guest остаётся то, что действительно различается: identity, profile, описание, CPU/RAM/swap/disk size, IP и осознанные overrides.

`301-ai-control` переопределяет общий pool через `placement.pool: null`.

### Network

Gateway IP больше не хранится в каждом guest.

Центрально:

```yaml
network_stages:
  current:
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
  target:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1
```

VMID-правило:

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Режимы: `current`, `dual`, `target`. Default route всегда один.

### `deployable`

```text
true
→ defaults/profile разрешаются
→ строится и валидируется полный effective desired state

false
→ deployer ничего не создаёт/пересоздаёт
→ manifest может быть неполным
→ defaults не делают его deployable автоматически
```

## rootfs

`rootfs/` повторяет абсолютные пути внутри гостя. Monorepo целиком внутрь каждого гостя не копируется.

Persistent data, Docker volumes, БД, записи камер, пользовательские Git repositories, runtime state и secrets не относятся к `rootfs/`.

## Deploy

PVE-side создание объекта:

```text
read-only checkout zsergeyru/proxmox
→ defaults.yaml + guest.yaml
→ resolve effective desired state
→ deploy-guest <VMID>
→ PLAN
→ APPLY
→ verify
```

`deploy-guest` обязан показывать в PLAN не только значение, но и его provenance: `[defaults]`, `[profile]`, `[guest]` или `[network_stages.*]`.

После появления AI/DevOps:

```text
Proximo в 301-ai-control
→ разрешённый runtime lifecycle

Ansible на 311-dev-services
→ повторяемая настройка ОС/rootfs по SSH

Semaphore
→ необязательный web UI к Ansible
```

## Текущие исключения

- `100-haos` — production HAOS; `deployable: false`;
- `201-ha-main`, `202-ha-test`, `203-ha-flat2` — варианты/резервы;
- `301-ai-control` — deployable target control plane, но `placement.pool: null`;
- `320-ai-control` — временный bootstrap; `deployable: false`;
- `501-frigate` — `type: undecided`, `deployable: false`;
- `9000 tpl-debian13` — protected source template, не managed guest.

## Проверка CI

CI проверяет source guest schema v4, `guests/defaults.yaml` schema, effective deploy schema после inheritance, наличие `guest.yaml`, VMID/name, уникальность VMID/IP, VMID/IP формулу, central subnet/gateway contract, network mode/default gateway, profile, pinned source, `ops:22`, managed-pool boundary и отсутствие secrets.

Главный принцип:

> Дублирование выносится в versioned defaults/profile, но итоговый desired state остаётся полным, детерминированным и проверяемым.
