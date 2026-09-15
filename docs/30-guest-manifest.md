# `guest.yaml` — deploy source of truth для VM/LXC

## Назначение

Desired state гостя строится из одного Git commit:

```text
guests/defaults.yaml
        +
profile из defaults.yaml
        +
guests/<VMID>-<name>/guest.yaml
        +
VMID addressing rule
        ↓
effective desired state
        ↓
validate → PLAN → APPLY → verify
```

Конфигурация ОС и приложений внутри гостя остаётся в `rootfs/` и Ansible.

## Версии schema

Source guest manifest:

```yaml
schema_version: 5
```

Central defaults:

```yaml
schema_version: 2
```

Используются три schema:

```text
schemas/guest.schema.yaml
→ компактный source guest.yaml

schemas/guest-defaults.schema.yaml
→ guests/defaults.yaml

schemas/guest-effective.schema.yaml
→ полный deployable desired state после merge и вычисления IP
```

## `guests/defaults.yaml`

Канонический пример:

```yaml
schema_version: 2

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
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
    addressing: vmid
  boot:
    onboot: true
    start_after_deploy: true
  management:
    ssh:
      user: ops
      port: 22

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
      unprivileged: true
      features:
        nesting: true
        keyctl: true
```

## Merge

Порядок строго детерминирован:

```text
defaults
→ profile
→ guest.yaml
→ management IP resolution
```

Maps объединяются deep-merge; более поздний scalar/null перекрывает более ранний.

Например `301-ai-control` использует:

```yaml
placement:
  pool: null
```

и остаётся вне обычного `managed`.

Для deployable guest `type`, `vm` и `lxc` принадлежат profile.

## Source LXC template

Для LXC Git является source of truth для конкретного `ostemplate`:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
```

Имя template должно быть pinned и не содержит `latest`, wildcard или плавающую версию.

`deploy-guest` **не скачивает LXC template и не выбирает другой template автоматически**. Его обязанность:

```text
прочитать effective lxc.source.ostemplate
→ проверить, что указанный volume уже доступен на PVE
→ если отсутствует — BLOCKED/STOP до изменений гостя
```

Наличие нужного LXC template — host-side prerequisite, но guest deployment не определяет, как именно он был подготовлен. PVE Stage 1 не читает `guest.yaml`, `guests/defaults.yaml`, profiles или другие guest-данные ради выбора template: Stage 1 является автономным bootstrap самого хоста и исполняет только собственную встроенную логику. Если требуемого profile template на PVE нет, `deploy-guest` останавливается без попытки скачать или подменить его.

## Сеть

### Один активный IP

У каждого гостя в desired state один management IPv4 и один default gateway.

Central network:

```yaml
defaults:
  network:
    bridge: vmbr0
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
    addressing: vmid
```

`current/target`, `mode`, `dual` и `default_gateway selector` удалены из manifest contract.

### VMID-addressing

Для `/16` применяется правило:

```text
VMID XYZ + A.B.0.0/16
→ A.B.X.YZ
```

Пример:

```text
VMID 311 + 192.168.0.0/16
→ 192.168.3.11/16
```

После изменения central subnet:

```text
VMID 311 + 10.0.0.0/16
→ 10.0.3.11/16
```

В `guest.yaml` обычный IP вообще не записывается.

### Optional override

Для исключения разрешён только host-address:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

Префикс запрещено указывать локально. Он всегда берётся из `defaults.network.subnet`.

Override имеет приоритет над VMID-формулой, но validator выводит warning, если адрес отличается от ожидаемого. Override, равный вычисляемому адресу, также даёт warning как избыточный.

`subnet`, `gateway` и addressing policy отдельный guest менять не может.

## Миграция сети

Переезд сети теперь означает изменение central desired state:

```yaml
# было
subnet: 192.168.0.0/16
gateway: 192.168.1.1

# стало
subnet: 10.0.0.0/16
gateway: 10.0.0.1
```

Обычные manifests не меняются.

Migration tool должен:

```text
зафиксировать Git commit
→ проверить доступность нового gateway/L2
→ получить список управляемых гостей
→ для каждого гостя по очереди:
     вычислить новый effective IP
     записать штатную Proxmox network-конфигурацию
     применить/reboot при необходимости
     проверить новый IP
     STOP при ошибке
→ завершить после проверки всех гостей
```

Массовое одновременное переключение не требуется.

## Compact manifest

```yaml
schema_version: 5
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
```

Из defaults/profile будут получены node, pool, storage, network, boot, SSH, type и LXC source/features. Management IP будет вычислен из VMID.

## Reserved / observed manifests

`deployable: false` не получает deploy defaults автоматически. Такой manifest может хранить известные факты, например:

```yaml
schema_version: 5
vmid: 501
name: frigate
type: undecided
state: planned
deployable: false
node: pve

description: Frigate video surveillance; VM/LXC type and resources are pending iGPU/OpenVINO testing

network:
  bridge: vmbr0

boot:
  onboot: false
```

Если у non-deployable объекта нужен явный management IP, он также может использовать `network.ipv4.address` без prefix.

## PLAN provenance

Ожидаемый вывод:

```text
Node               pve                 [defaults]
Pool               managed             [defaults]
Storage            local-lvm           [defaults]
Profile            docker-lxc          [guest]
LXC appliance      debian-13-...       [profile]
Bridge             vmbr0               [defaults]
Subnet             192.168.0.0/16      [defaults]
Gateway            192.168.1.1         [defaults]
Management IP      192.168.3.11/16     [vmid]
Memory             4096 MiB            [guest]
Disk size          32 GiB              [guest]
```

При `network.ipv4.address` override источник IP — `[guest]`.

## Validator

Validator проверяет только `guests/defaults.yaml` и существующие `guests/*/guest.yaml` плюс schema-файлы как описание правил.

Warnings не делают CI красным. IP override вне VMID-формулы — warning; IP вне central subnet, duplicate IP, gateway/network/broadcast collision — error.

Pinned `lxc.source.ostemplate` является частью Git desired state. Отсутствие template на конкретном PVE — runtime prerequisite/preflight deployer, а не задача schema validator.

## Git как source of truth

Источник desired state:

```text
guests/defaults.yaml
+
guest.yaml
+
детерминированная VMID-формула
=
effective desired state
```

Для LXC это также означает: конкретный `ostemplate` берётся из Git без автоматической подмены deployer'ом.

`/etc/proxmox-deployer/config.yaml` остаётся host-side runtime/bootstrap config и не является источником guest defaults.
