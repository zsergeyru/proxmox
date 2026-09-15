# `guest.yaml` — deploy source of truth для VM/LXC

## Назначение

Каждый каталог `guests/<VMID>-<name>/` обязан содержать `guest.yaml`.

Начиная со schema v4 deploy desired state строится из двух Git-источников:

```text
guests/defaults.yaml
        +
profile из defaults.yaml
        +
guests/<VMID>-<name>/guest.yaml
        ↓
effective desired state
        ↓
validate → PLAN → APPLY → verify
```

Это не скрытые defaults внутри deployer. Все наследуемые значения находятся в Git, версионируются тем же commit SHA и проходят CI вместе с `guest.yaml`.

Конфигурация ОС и приложений внутри гостя остаётся в `rootfs/` и Ansible.

## Schema version

Текущая версия guest manifest:

```yaml
schema_version: 4
```

Версия `4` вводит централизованные project defaults и именованные profiles.

Используются три schema:

```text
schemas/guest.schema.yaml
→ исходный компактный guest.yaml

schemas/guest-defaults.schema.yaml
→ guests/defaults.yaml

schemas/guest-effective.schema.yaml
→ итоговый deployable desired state после merge
```

CI сначала проверяет исходные файлы, затем строит effective state и проверяет его повторно.

## `guests/defaults.yaml`

Центральный файл хранит только действительно общие или профильные значения:

```yaml
schema_version: 1

defaults:
  node: pve
  protection: false
  placement:
    pool: managed
  resources:
    disk:
      storage: local-lvm
  network:
    bridge: vmbr0
    mode: current
    default_gateway: current
  boot:
    onboot: true
    start_after_deploy: true
  management:
    ssh:
      user: ops
      port: 22

network_stages:
  current:
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
  target:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1

profiles:
  debian-vm:
    type: vm
    vm:
      source:
        template_vmid: 9000
        clone: full
      guest_agent: true

  docker-lxc:
    type: lxc
    lxc:
      container_runtime: docker
      source:
        ostemplate: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
        download_if_missing: true
      unprivileged: true
      features:
        nesting: true
        keyctl: true
```

Таким образом смена LXC appliance, VM template, обычного storage, SSH identity или общего network default выполняется в одном месте.

## Порядок наследования

Merge выполняется строго в таком порядке:

```text
defaults
→ выбранный profile
→ guest.yaml overrides
→ central network-stage gateway injection
```

Более поздний слой перекрывает более ранний.

Например `placement.pool: managed` приходит из defaults, но `301-ai-control` явно задаёт:

```yaml
placement:
  pool: null
```

и остаётся вне обычной AI write-zone.

Для deployable guest поля `type`, `vm` и `lxc` принадлежат profile. Если нужен другой source/type contract, создаётся или выбирается другой profile, а не локально переписывается профиль в одном guest.

## `deployable`

Каждый manifest явно содержит `deployable: true` или `deployable: false`.

```text
deployable: true
→ defaults/profile применяются
→ строится полный effective desired state
→ deploy-guest может выполнять PLAN/APPLY

deployable: false
→ объект зарезервирован, наблюдается или временно существует
→ deploy-guest ничего не создаёт и не пересоздаёт
→ manifest может быть неполным
→ deploy defaults не превращают такой объект в deployable автоматически
```

## Компактный deployable manifest

Типичный LXC теперь содержит только индивидуальные параметры:

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

Из `defaults.yaml` сюда добавятся node, protection, pool, storage, bridge, network mode, active gateway selector, boot policy и management SSH. Из `docker-lxc` добавятся type, pinned appliance и LXC features.

## Ресурсы

Индивидуально в guest обычно остаются CPU, RAM, swap для LXC и disk size.

Обычный storage сейчас централизован:

```yaml
defaults:
  resources:
    disk:
      storage: local-lvm
```

Если конкретному guest потребуется другой storage, он может явно переопределить только это значение.

Deployer никогда не уменьшает существующий disk автоматически.

## Сеть

### Централизованные параметры

Subnet и gateway каждой стадии задаются только в `guests/defaults.yaml`:

```yaml
network_stages:
  current:
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
  target:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1
```

Gateway больше не дублируется в `guest.yaml`.

Общий текущий режим также имеет project default:

```yaml
defaults:
  network:
    bridge: vmbr0
    mode: current
    default_gateway: current
```

При необходимости конкретный guest может временно переопределить `mode`/`default_gateway`, что позволяет выполнять миграцию поэтапно.

### Индивидуальные адреса

В guest остаются сами management IP:

```yaml
network:
  current:
    ipv4:
      address: 192.168.3.21/16
  target:
    ipv4:
      address: 10.0.3.21/16
```

CI проверяет VMID-правило:

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Префиксы и ожидаемые gateway берутся из `defaults.yaml`, а не хардкодятся отдельно в каждом manifest.

### Режимы

Поддерживаются `current`, `dual`, `target`.

```text
mode=current → default_gateway=current
mode=dual    → default_gateway=current или target
mode=target  → default_gateway=target
```

Default route всегда один. Нормальная миграция: `current/current → dual/current → dual/target → target/target`.

Сейчас project default остаётся `current/current`, поэтому Keenetic и основной gateway не меняются.

## Profiles

### `debian-vm`

Определяет обычную Debian VM: `type=vm`, full clone from template 9000 и включённый QEMU guest agent.

### `docker-lxc`

Определяет Docker-heavy Debian LXC: pinned Debian 13 appliance, `unprivileged=true`, `nesting=true`, `keyctl=true`.

Обновление pinned appliance выполняется один раз в profile и после commit применяется ко всем новым гостям этого profile.

## Reserved / observed manifests

`deployable: false` не обязан использовать profile:

```yaml
schema_version: 4
vmid: 501
name: frigate
type: undecided
state: planned
deployable: false
node: pve

description: Frigate; VM/LXC type and resources are pending hardware testing
```

Такой manifest сохраняет VMID и известные параметры, но не получает deploy semantics только из-за наличия defaults.

## Поведение `deploy-guest`

По умолчанию `deploy-guest 311` строит PLAN, а `deploy-guest 311 --apply` применяет его.

Перед PLAN/APPLY:

```text
зафиксировать один Git commit SHA
→ прочитать guests/defaults.yaml
→ найти ровно один guests/<VMID>-*/guest.yaml
→ проверить source schemas
→ убедиться deployable=true
→ выбрать profile
→ выполнить deterministic deep merge
→ добавить gateway из central network_stages
→ проверить effective schema + semantics
→ проверить source/storage/bridge на PVE
→ сравнить effective desired state с PVE
→ PLAN
→ --apply
→ verify
```

PLAN должен показывать provenance значений, например:

```text
Node               pve                 [defaults]
Pool               managed             [defaults]
Storage            local-lvm           [defaults]
Profile            docker-lxc          [guest]
LXC appliance      debian-13-...       [profile]
Network mode       current             [defaults]
Default gateway    192.168.1.1         [network_stages.current]
Memory             4096 MiB            [guest]
Disk size          32 GiB              [guest]
```

Deployer не должен уничтожать неизвестный объект, менять `vm ↔ lxc`, уменьшать disk, выбирать source по `latest`, подставлять отсутствующие значения, менять сеть скрытым CLI-default, автоматически добавлять control-plane в `managed` или делать destructive replacement без отдельного режима.

## Git как source of truth

Source of truth теперь является детерминированным набором из одного commit:

```text
guests/defaults.yaml
+
guest.yaml
=
effective desired state
```

`/etc/proxmox-deployer/config.yaml` остаётся host-side runtime/bootstrap config и не используется как источник project defaults для VM/LXC.

Главный принцип:

> Наследование уменьшает дублирование, но не создаёт скрытых значений: всё, что влияет на deploy, находится в Git, проверяется CI и может быть показано в PLAN вместе с источником значения.
