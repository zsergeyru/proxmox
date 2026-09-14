# Guests

Каждый каталог `guests/<VMID>-<name>/` соответствует одной VM/LXC либо зарезервированному объекту и содержит его паспорт, локальные решения и управляемые файлы гостевой ОС.

Актуальная нумерация определяется [`../docs/11-vmid-plan.md`](../docs/11-vmid-plan.md).

Машинно-читаемая спецификация: [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

## Структура

```text
<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/        # при необходимости
├── STATUS.md         # observed drift/миграция
└── rootfs/           # управляемые файлы по реальным абсолютным путям
```

`guest.yaml` обязателен для каждого каталога с VMID. Отсутствие manifest теперь является ошибкой CI.

Если параметры ещё не приняты, это выражается самим manifest:

```yaml
type: undecided
deployable: false
```

а не отсутствием файла.

## guest.yaml schema v2

Базовые обязательные поля:

```yaml
schema_version: 2
vmid: 109
name: network-gateway
type: vm
state: planned
deployable: true
node: pve

description: Network gateway, DNS, VPN and policy routing
```

Допустимые `state`:

```text
planned   → запланирован, ещё не рабочий
active    → рабочий production/целевой объект
bootstrap → временный рабочий объект для развёртывания/миграции
legacy    → старый объект, сохраняемый до планового вывода
```

Допустимые `type`:

```text
vm
lxc
undecided
```

`undecided` разрешён только при `deployable: false`.

### `deployable`

```text
true
→ manifest полностью достаточен для PVE PLAN/APPLY
→ CI требует source/resources/network/boot/management/placement/protection

false
→ объект нельзя создавать через универсальный deployer
→ manifest хранит резерв, observed state или временную миграционную запись
```

`state` и `deployable` решают разные задачи. `planned` может быть как deployable, так и пока недоопределённым.

### Protection и pool

Deployable guest задаёт значения явно:

```yaml
protection: false

placement:
  pool: managed
```

Control-plane/защищённые объекты используют:

```yaml
placement:
  pool: null
```

`301-ai-control` не входит в обычную self-managed write-zone.

### Ресурсы

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 16
    storage: local-lvm
```

LXC дополнительно фиксирует `swap_mb`.

Deployer не подставляет отсутствующие обязательные параметры.

### Сеть

Для deployable planned-гостя:

```yaml
network:
  bridge: vmbr0
  current:
    ipv4:
      address: 192.168.3.21/16
      gateway: 192.168.1.1
  target:
    ipv4:
      address: 10.0.3.21/16
      gateway: 10.0.0.1
```

VMID-правило:

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Это две стадии миграции, а не два одновременных default gateway.

### Boot

```yaml
boot:
  onboot: true
  start_after_deploy: true
```

Оба значения обязательны у deployable guest.

### Management

Единая management identity Debian-гостей:

```yaml
management:
  ssh:
    user: ops
    port: 22
```

Private keys, passwords и token secrets в manifest не хранятся.

### VM source

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

### LXC source

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
    download_if_missing: true
  unprivileged: true
  features:
    nesting: true
    keyctl: true
```

Source pin должен быть конкретным. `latest`, `13.x`, wildcard и TBD в deployable manifest запрещены CI.

## rootfs

`rootfs/` повторяет абсолютные пути внутри гостя:

```text
guests/109-network-gateway/rootfs/etc/nftables.conf
→ /etc/nftables.conf

guests/301-ai-control/rootfs/opt/ai-control/...
→ /opt/ai-control/...
```

Monorepo целиком внутрь каждого гостя не копируется.

Для каталогов, полностью принадлежащих проекту (`/opt/<service>/`), допустима синхронизация с удалением файлов, исчезнувших из Git. В общих системных каталогах файлы устанавливаются/удаляются только по конкретным путям.

Persistent data, Docker volumes, БД, записи камер, пользовательские Git repositories, runtime state и secrets не относятся к `rootfs/`.

## Deploy

PVE-side создание объекта:

```text
read-only checkout zsergeyru/proxmox
→ deploy-guest <VMID>
→ guest.yaml
→ validate
→ PLAN
→ qm/pct/pvesh/pvesm
→ verify
```

`deploy-guest` обязан отказать при `deployable: false`.

После появления AI/DevOps:

```text
Proximo в 301-ai-control
→ разрешённый runtime lifecycle

Ansible на 311-dev-services
→ повторяемая настройка ОС/rootfs по SSH

Semaphore
→ необязательный web UI к Ansible
```

Для обычной Debian VM из `tpl-debian13`:

```text
Full Clone from 9000
→ placement/pool
→ protection
→ CPU/RAM/disk/network
→ ciuser=ops
→ Cloud-Init
→ start согласно boot.start_after_deploy
→ QEMU Agent/SSH verification
```

Base template `9000` остаётся protected.

## Текущие исключения

- `100-haos` — production HAOS; `deployable: false`, исторический VMID сохраняется;
- `201-ha-main`, `202-ha-test`, `203-ha-flat2` — варианты/резервы; до принятия полных параметров `deployable: false`;
- `301-ai-control` — deployable target control plane, но `placement.pool: null`;
- `320-ai-control` — временный bootstrap; `deployable: false`;
- `501-frigate` — manifest существует, но `type: undecided` и `deployable: false` до теста iGPU/OpenVINO;
- `9000 tpl-debian13` — protected template, source для clone, но не managed guest.

## Проверка CI

CI проверяет:

- JSON Schema v2;
- наличие `guest.yaml` в каждом VMID-каталоге;
- совпадение VMID/name с именем каталога;
- уникальность VMID и статических IP;
- наличие VMID в плане;
- VMID/IP формулу и `/16`;
- gateway и принадлежность subnet;
- deploy-ready обязательные поля;
- pinned VM/LXC source;
- `ops:22`;
- managed-pool boundary;
- отсутствие secret-like keys.

Главный принцип:

> Неполный объект не маскируется defaults. Он явно `deployable: false` до момента, когда manifest станет полным.
