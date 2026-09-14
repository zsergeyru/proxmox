# `guest.yaml` — манифест развёртывания VM/LXC

## Назначение

Каждый каталог `guests/<VMID>-<name>/` содержит `guest.yaml`, который является каноническим машинно-читаемым описанием объекта Proxmox.

Цель — чтобы один и тот же manifest мог использоваться человеком, PVE-deployer и AI:

```text
guest.yaml
→ validate
→ PLAN
→ apply
→ verify
```

Манифест описывает именно объект Proxmox и параметры его создания. Конфигурация ОС и приложений внутри гостя остаётся в `rootfs/`/Ansible и не смешивается с `guest.yaml`.

## Поиск по VMID

Универсальный deployer на PVE получает только номер:

```bash
deploy-guest 311
```

и ищет ровно один каталог:

```text
guests/311-*/guest.yaml
```

Если найдено 0 или больше 1 совпадения, выполнение прекращается. Имя каталога не является источником параметров VM — после обнаружения все значения берутся из YAML.

## Базовая schema

Используется существующая `schema_version: 1`.

Минимум:

```yaml
schema_version: 1
vmid: 321
name: app-services
type: vm
state: planned
node: pve
```

Допустимые `type`:

```text
vm
lxc
```

Допустимые `state` определены в `guests/README.md`.

## Ресурсы

Существующий раздел `resources` остаётся каноническим:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 24
    storage: local-lvm
```

Правила:

- `cores` — число vCPU;
- `memory_mb` — RAM в MiB;
- `disk.size_gb` — целевой размер основного диска/rootfs;
- `disk.storage` — существующий Proxmox storage;
- deployer не уменьшает существующий диск автоматически;
- отсутствующие поля не должны молча заменяться произвольными значениями deployer, если без них создание невозможно.

Не все существующие manifests пока полностью заполнены. Перед автоматическим deploy конкретного гостя его `guest.yaml` должен содержать все обязательные для данного типа параметры.

## Сеть

Используется существующая структура:

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

Deployer получает явный режим адресации, по умолчанию `current`. Он не должен одновременно конфигурировать два default gateway.

Планируемый CLI:

```bash
deploy-guest 321 --network current --apply
deploy-guest 321 --network target --apply
```

## Boot и protection

```yaml
boot:
  onboot: true

protection: false
```

Для обычных managed guests default проекта — `protection: false`.

Base template `9000` — отдельное исключение и остаётся защищённым. При Full Clone deployer явно задаёт protection нового гостя по manifest и никогда не снимает protection с source template.

## Management

Для обычных Debian VM:

```yaml
management:
  ssh:
    user: ops
    port: 22
```

Если deploy выполняется после появления `301-ai-control`, public infrastructure key может быть передан через Cloud-Init. Сам PVE-deployer не должен требовать private AI key и не должен хранить его на PVE.

## Источник создания VM

Для `type: vm` источник должен быть указан явно в секции `vm`.

Обычная Debian VM из проекта:

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

Допустимый основной сценарий:

```text
template_vmid: 9000
clone: full
```

Не следует зашивать правило `type: vm → всегда template 9000` в deployer. В проекте могут существовать HAOS, Windows, appliance/router VM и другие источники.

Если VM не создаётся клонированием template, для неё должен быть описан отдельный поддерживаемый `vm.source` до включения автоматического deploy.

## Источник создания LXC

Для `type: lxc` источник задаётся в секции `lxc`:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.x-amd64.tar.zst
  unprivileged: true
  nesting: true
```

`ostemplate` должен существовать на PVE или deployer должен иметь отдельный явно описанный механизм его получения. Он не должен случайно выбирать «последний Debian template» без manifest/policy.

## Пример VM manifest

```yaml
schema_version: 1
vmid: 321
name: app-services
type: vm
state: planned
node: pve

description: Application services

protection: false

resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 24
    storage: local-lvm

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

boot:
  onboot: true

management:
  ssh:
    user: ops
    port: 22

vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

## Пример LXC manifest

```yaml
schema_version: 1
vmid: 311
name: dev-services
type: lxc
state: planned
node: pve

protection: false

resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 16
    storage: local-lvm

network:
  bridge: vmbr0
  current:
    ipv4:
      address: 192.168.3.11/16
      gateway: 192.168.1.1

boot:
  onboot: true

lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.x-amd64.tar.zst
  unprivileged: true
  nesting: true
```

## Поведение `deploy-guest`

Планируемый PVE-side deployer работает непосредственно штатными инструментами Proxmox (`qm`, `pct`, `pvesh`, `pvesm`) и не зависит от `301`, Hermes или Proximo.

По умолчанию команда только строит PLAN:

```bash
deploy-guest 321
```

Применение требует явного флага:

```bash
deploy-guest 321 --apply
```

Алгоритм:

```text
обновить локальный read-only checkout zsergeyru/proxmox
→ найти guests/<VMID>-*/guest.yaml
→ проверить schema и обязательные поля
→ проверить конфликт VMID/name/type
→ проверить source template/ostemplate/storage/bridge
→ сравнить с существующим объектом, если он уже есть
→ вывести PLAN и drift
→ при --apply создать или безопасно привести поддерживаемые параметры
→ start, если policy/manifest это требует
→ проверить status/QEMU Agent/health, где применимо
```

Deployer не должен автоматически уничтожать существующий неизвестный объект, менять тип `vm ↔ lxc`, уменьшать диск, удалять production guest или выполнять destructive replacement без отдельного режима.

## Git как source of truth

PVE-deployer использует локальный read-only checkout приватного `zsergeyru/proxmox`. На PVE для него создаётся отдельный GitHub Deploy Key без write access.

```text
PVE Deploy Key
→ read-only zsergeyru/proxmox

301 Deploy Key
→ отдельная identity; может иметь write access для AI workflow
```

Эти ключи не переиспользуются между PVE и `301`.

## Главный принцип

> `guest.yaml` должен содержать достаточно данных, чтобы PVE мог однозначно построить PLAN создания VM/LXC без догадок, а сам deployer должен быть независим от любого AI-агента.
