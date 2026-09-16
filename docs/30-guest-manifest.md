# `guest.yaml` — deploy source of truth для VM/LXC

**Type:** Specification  
**Status:** Active  
**Source of truth:** Yes — для manifest/defaults/effective-state schema, merge semantics и deployable guest contract.

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

Переход с generic user `ops` на `root` не меняет структуру schema, поэтому номера schema не повышаются.

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
      user: root
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
        ostemplate: local:vztmpl/debian-13-standard
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

## Management SSH contract

Для всех deployable Debian VM/LXC effective state должен содержать:

```yaml
management:
  ssh:
    user: root
    port: 22
```

Root password не является частью manifest и не хранится в Git. Проектная policy требует locked root password и public-key-only SSH.

Разные управляющие контуры используют разные SSH keypairs, но один Linux-user `root`:

```text
PVE/deploy-guest key
AI Control key
Ansible/311 key
personal key при необходимости
```

Наличие конкретного public key в `/root/.ssh/authorized_keys` является отдельным access state и не выводится автоматически из pool membership.

## Source LXC template

Для LXC Git хранит **семейство** template, а не конкретную версию архива:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard
```

Это стабильный selector. В `defaults.yaml` запрещено указывать конкретный файл вида:

```text
debian-13-standard_13.6-1_amd64.tar.zst
```

Также запрещены `latest`, wildcard, `13.x`, `tbd` и подобные плавающие pseudo-values.

Stage 1 автономно подготавливает актуальный Debian 13 standard appliance на PVE:

```text
pveam update
→ выбрать актуальный debian-13-standard_*_amd64 archive
→ скачать в local:vztmpl, если его нет
```

`deploy-guest` при runtime resolution должен:

```text
прочитать effective lxc.source.ostemplate selector
→ найти на разрешённом storage установленные archives этого семейства
→ выбрать наиболее новую доступную версию
→ если подходящего archive нет — BLOCKED/STOP до изменений guest
```

`deploy-guest` не должен подменять семейство другим дистрибутивом и не должен сам скачивать base template в обход host bootstrap.

## Initial SSH keys

### VM

Текущий template `9000` использует `ciuser=root`, root password locked и root SSH key-only, но не содержит baked-in `authorized_keys`.

До первого start clone получает нужные public keys через Cloud-Init.

Точная версия и contract template определяются только в [`../templates/debian13/README.md`](../templates/debian13/README.md) и [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

### LXC

При создании LXC `deploy-guest` передаёт public keys через штатный `ssh-public-keys`. Они сразу устанавливаются для `root`.

Отдельное создание `ops`, `sudo` или перенос ключа после первого входа не требуется.

Capability `base` не участвует в initial SSH access.

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

Переезд сети означает изменение central desired state:

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

Из defaults/profile будут получены node, pool, storage, network, boot, SSH user/port, type и LXC source/features. Management IP будет вычислен из VMID.

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
Node               pve                         [defaults]
Pool               managed                     [defaults]
Storage            local-lvm                   [defaults]
Profile            docker-lxc                  [guest]
LXC selector       debian-13-standard          [profile]
Resolved archive   debian-13-standard_<...>    [runtime PVE]
SSH user           root                        [defaults]
Bridge             vmbr0                       [defaults]
Subnet             192.168.0.0/16              [defaults]
Gateway            192.168.1.1                 [defaults]
Management IP      192.168.3.11/16             [vmid]
Memory             4096 MiB                    [guest]
Disk size          32 GiB                      [guest]
```

При `network.ipv4.address` override источник IP — `[guest]`.

## Validator

Validator проверяет `guests/defaults.yaml` и существующие `guests/*/guest.yaml` плюс schema-файлы как описание правил.

Warnings не делают CI красным. IP override вне VMID-формулы — warning; IP вне central subnet, duplicate IP, gateway/network/broadcast collision — error.

Для deployable guests validator требует `management.ssh == root:22`.

`lxc.source.ostemplate` должен быть selector семейства без версии и имени архива. Отсутствие подходящего archive на конкретном PVE — runtime prerequisite/preflight deployer, а не задача schema validator.

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

Runtime-факты, которые зависят от конкретного PVE, например фактическое имя актуального LXC archive или набор установленных SSH public keys, не должны превращаться в жёстко зашитые версии внутри `defaults.yaml`.

`/etc/proxmox-deployer/config.yaml` остаётся host-side runtime/bootstrap config и не является источником guest defaults.
