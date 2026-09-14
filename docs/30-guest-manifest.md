# `guest.yaml` — deploy source of truth для VM/LXC

## Назначение

Каждый каталог `guests/<VMID>-<name>/` обязан содержать `guest.yaml`.

`guest.yaml` — канонический машинно-читаемый desired state объекта Proxmox:

```text
guest.yaml
→ validate
→ PLAN
→ apply
→ verify
```

Манифест описывает сам объект Proxmox и параметры, необходимые для его однозначного создания/сверки. Конфигурация ОС и приложений внутри гостя остаётся в `rootfs/` и Ansible.

## Schema version

Текущая schema:

```yaml
schema_version: 2
```

Версия `2` вводит явный признак `deployable` и строгую deploy-ready схему. Это намеренно новая версия: обязательные поля не добавляются задним числом в `schema_version: 1`.

Формальная JSON Schema хранится в:

```text
schemas/guest.schema.yaml
```

CI сначала проверяет manifest по этой schema, затем выполняет дополнительные семантические проверки.

## `deployable`

Каждый manifest обязан явно содержать:

```yaml
deployable: true
```

или:

```yaml
deployable: false
```

Смысл:

```text
deployable: true
→ manifest является полным deploy source of truth
→ deploy-guest может строить PLAN/APPLY только из него
→ обязательны source, resources, network, boot, placement, protection и management

deployable: false
→ объект зарезервирован, наблюдается или временно существует
→ deploy-guest обязан отказаться от создания/пересоздания
→ manifest может быть неполным
```

`state: planned` не означает автоматически `deployable: true`. Например, будущий вариант миграции может быть `planned`, но оставаться `deployable: false` до принятия всех параметров.

## Базовые поля

```yaml
schema_version: 2
vmid: 321
name: app-services
type: lxc
state: planned
deployable: true
node: pve

description: General application services
```

Допустимые `type`:

```text
vm
lxc
undecided
```

`undecided` разрешён только при `deployable: false`. Это нужно для зарезервированных объектов вроде `501-frigate`, где VM/LXC ещё не выбран, но отсутствие manifest больше не допускается.

## Placement и protection

Для deployable-гостя значения задаются явно:

```yaml
protection: false

placement:
  pool: managed
```

Если объект не должен входить в обычную self-managed write-zone:

```yaml
placement:
  pool: null
```

В частности, `301-ai-control` не помещается в `managed`.

Deployer не выбирает pool и protection по скрытым defaults.

## Ресурсы

Для deployable VM/LXC:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 24
    storage: local-lvm
```

Для LXC дополнительно фиксируется swap:

```yaml
resources:
  swap_mb: 512
```

Правила:

- `cores` — число vCPU;
- `memory_mb` — RAM в MiB;
- `swap_mb` — swap LXC в MiB;
- `disk.size_gb` — целевой размер основного диска/rootfs;
- `disk.storage` — Proxmox storage;
- deployer не уменьшает существующий диск автоматически;
- обязательные значения не заменяются внутренними defaults deployer.

Параметры, отсутствующие в schema и manifest, считаются неуправляемыми этим слоем и могут наследоваться от source/template. Это должно быть осознанным наследованием, а не догадкой deployer.

## Сеть

Для deployable planned-гостя обязательны оба этапа:

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

Правила CI:

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Также проверяются:

- точный prefix `/16`;
- корректный IPv4;
- gateway находится в указанной сети;
- текущий gateway — `192.168.1.1`;
- целевой gateway — `10.0.0.1`;
- отсутствие дублирующихся статических IP.

Это описание двух стадий миграции. Одновременно два default gateway в госте не настраиваются.

CLI выбирает одну стадию:

```bash
deploy-guest 321 --network current --apply
deploy-guest 321 --network target --apply
```

## Boot

Deployable manifest явно определяет как автозапуск после reboot PVE, так и поведение после самого deploy:

```yaml
boot:
  onboot: true
  start_after_deploy: true
```

Deployer не должен сам решать, запускать ли только что созданный объект.

## Management

Для управляемых Debian VM/LXC проекта:

```yaml
management:
  ssh:
    user: ops
    port: 22
```

CI для deployable-гостей требует именно `ops:22`.

Создание OS user/authorized keys относится к bootstrap гостевой ОС, но желаемая management identity фиксируется в manifest.

Пароли, API token secrets и private keys в `guest.yaml` запрещены.

## Источник VM

Обычная Debian VM:

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

Для текущей schema автоматизированный clone-сценарий — только `full`.

Deployer обязан проверить наличие source VMID и `template: 1` на PVE до APPLY.

Нельзя зашивать правило «любая VM всегда из 9000» внутрь deployer. Если появится VM другого типа, schema/source расширяются явно.

## Источник LXC

Deployable LXC содержит точный pinned `ostemplate`:

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

Правила:

- `ostemplate` — конкретный Proxmox volume, а не `latest`, `13.x`, wildcard или TBD;
- если `download_if_missing: true`, deployer может получить **именно этот** template через `pveam`;
- переход на новую revision Debian template выполняется изменением Git manifest;
- `unprivileged`, `nesting` и `keyctl` задаются явно;
- Docker-heavy LXC проекта используют `nesting: true` и `keyctl: true`.

CI не проверяет фактическое наличие template/storage/bridge на конкретном PVE — это preflight самого deployer.

## Полный пример VM

```yaml
schema_version: 2
vmid: 301
name: ai-control
type: vm
state: planned
deployable: true
node: pve

description: Target central AI infrastructure control plane

protection: false

placement:
  pool: null

resources:
  cpu:
    cores: 2
  memory_mb: 4096
  disk:
    size_gb: 24
    storage: local-lvm

network:
  bridge: vmbr0
  current:
    ipv4:
      address: 192.168.3.1/16
      gateway: 192.168.1.1
  target:
    ipv4:
      address: 10.0.3.1/16
      gateway: 10.0.0.1

boot:
  onboot: true
  start_after_deploy: true

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

## Полный пример LXC

```yaml
schema_version: 2
vmid: 311
name: dev-services
type: lxc
state: planned
deployable: true
node: pve

description: Ansible, Semaphore, Git, CI and development services

protection: false

placement:
  pool: managed

resources:
  cpu:
    cores: 2
  memory_mb: 4096
  swap_mb: 1024
  disk:
    size_gb: 32
    storage: local-lvm

network:
  bridge: vmbr0
  current:
    ipv4:
      address: 192.168.3.11/16
      gateway: 192.168.1.1
  target:
    ipv4:
      address: 10.0.3.11/16
      gateway: 10.0.0.1

boot:
  onboot: true
  start_after_deploy: true

management:
  ssh:
    user: ops
    port: 22

lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst
    download_if_missing: true
  unprivileged: true
  features:
    nesting: true
    keyctl: true
```

Этот пример проходит тот же validator, что и реальные manifests.

## Reserved / undecided пример

```yaml
schema_version: 2
vmid: 501
name: frigate
type: undecided
state: planned
deployable: false
node: pve

description: Frigate; VM/LXC type and resources are pending hardware testing
```

Такой manifest сохраняет резерв VMID в Git, но deployer обязан завершиться без APPLY.

## Поведение `deploy-guest`

По умолчанию:

```bash
deploy-guest 311
```

строит PLAN.

Применение:

```bash
deploy-guest 311 --apply
```

Перед любым PLAN/APPLY:

```text
найти ровно один guests/<VMID>-*/guest.yaml
→ schema_version == 2
→ deployable == true
→ пройти JSON Schema
→ пройти semantic validation
→ проверить source/storage/bridge на реальном PVE
→ сравнить desired state с существующим объектом
→ PLAN
→ --apply
→ verify
```

Если `deployable: false`, команда должна явно сообщить причину и ничего не создавать.

Deployer не должен:

- уничтожать неизвестный существующий объект;
- менять `vm ↔ lxc`;
- уменьшать диск;
- выбирать source/template «по последней версии»;
- подставлять отсутствующие обязательные значения;
- автоматически добавлять protected/control-plane объекты в `managed`;
- выполнять destructive replacement без отдельного явного режима.

## Git как source of truth

PVE-deployer читает manifest из read-only checkout приватного `zsergeyru/proxmox`.

PLAN и APPLY выполняются по одному зафиксированному commit SHA.

Главный принцип:

> `deployable: true` означает, что `guest.yaml` сам содержит все проектные параметры, необходимые для однозначного создания объекта Proxmox; отсутствие обязательного параметра является ошибкой CI, а не поводом для deployer что-либо угадывать.
