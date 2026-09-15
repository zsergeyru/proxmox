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

`guest.yaml` является машинно-читаемым паспортом конкретного объекта. Если параметры ещё не приняты, это выражается через `deployable: false`.

## guest schema v4

Effective deploy state строится так:

```text
defaults.yaml
→ profile
→ guest.yaml overrides
→ central network gateway
→ effective desired state
```

Все deploy-значения находятся в Git. Никаких скрытых defaults внутри `deploy-guest` быть не должно.

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

Несоответствие IP этому правилу считается предупреждением, а не ошибкой: нестандартный адрес допустим, если он назначен осознанно.

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

## Текущие исключения

- `100-haos` — production HAOS; `deployable: false`;
- `201-ha-main`, `202-ha-test`, `203-ha-flat2` — варианты/резервы;
- `301-ai-control` — deployable target control plane, но `placement.pool: null`;
- `320-ai-control` — временный bootstrap; `deployable: false`;
- `501-frigate` — `type: undecided`, `deployable: false`;
- `9000 tpl-debian13` — protected source template, не managed guest.

## Проверка guest-конфигурации

`scripts/validate_repo.py` намеренно имеет узкую область ответственности.

Из проектных данных он читает только:

```text
guests/defaults.yaml
guests/*/guest.yaml
```

Файлы `schemas/*.yaml` используются только как формальное описание правил валидации. Валидатор не анализирует `docs/*.md`, `README.md`, VMID-plan, `rootfs/` и остальные файлы репозитория и не строит по ним warnings/errors.

Проверяются source guest schema v4, `guests/defaults.yaml`, effective deploy state после inheritance, VMID/name относительно пути самого `guest.yaml`, уникальность VMID/IP, central subnet/gateway contract, network mode/default gateway, profiles, pinned source, `ops:22`, managed-pool boundary и отсутствие секретов непосредственно в `defaults.yaml`/`guest.yaml`.

Блокирующими ошибками считаются, в частности:

- IP вне соответствующей central subnet;
- IP, совпадающий со шлюзом;
- network/broadcast IP в качестве адреса гостя;
- одинаковый current и target IP;
- duplicate static IP;
- `ballooning_mb > memory_mb`;
- некорректная комбинация network mode/default gateway.

Предупреждения выводятся на русском языке и не делают CI красным. Валидатор предупреждает о следующих ситуациях:

- management IP не соответствует рекомендуемой VMID-формуле;
- current и target IP имеют разные идентификаторные части;
- `guest.yaml` повторяет значение, уже наследуемое из defaults/profile;
- guest переопределяет общий node, bridge, storage или management SSH;
- guest переопределяет общий `network.mode` или `network.default_gateway`;
- profile из `defaults.yaml` не используется ни одним deployable-гостем;
- deployable description содержит `TODO`/`TBD`/`pending`/`unknown` или аналогичный маркер незавершённости;
- `state: bootstrap` или `state: legacy` используется вместе с `deployable: true`;
- `state: active` имеет `boot.onboot: false`;
- `protection: true` используется у гостя в pool `managed`.

Главный принцип:

> Guest-validator проверяет только машинно-читаемый desired state гостей и не связывает его корректность с документацией репозитория.
