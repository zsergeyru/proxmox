# Guests

Каждый каталог `guests/<VMID>-<name>/` соответствует одной VM/LXC либо зарезервированному объекту и содержит его паспорт, локальные решения и управляемые файлы гостевой ОС.

Машинно-читаемая спецификация: [`../docs/30-guest-manifest.md`](../docs/30-guest-manifest.md).

## Структура

```text
guests/
├── defaults.yaml      # общие deploy defaults, сеть и profiles
├── README.md
└── <VMID>-<name>/
    ├── README.md
    ├── guest.yaml     # индивидуальные параметры/overrides
    ├── decisions/
    ├── STATUS.md
    └── rootfs/
```

`guest.yaml` обязателен только для каталогов, которые уже заведены в `guests/`. Если объект ещё не готов к универсальному deploy, используется `deployable: false`.

## Schema

Текущий source manifest:

```yaml
schema_version: 5
```

Текущий `guests/defaults.yaml`:

```yaml
schema_version: 2
```

Effective deploy state строится так:

```text
defaults.yaml
→ выбранный profile
→ guest.yaml overrides
→ вычисление management IP из VMID
→ effective desired state
```

Все project defaults находятся в Git. Deployer не должен иметь скрытых project defaults.

## Централизованные defaults

Каноническая сеть задаётся один раз:

```yaml
defaults:
  network:
    bridge: vmbr0
    subnet: 192.168.0.0/16
    gateway: 192.168.1.1
    addressing: vmid
```

Management SSH для deployable Debian guests также централизован:

```yaml
defaults:
  management:
    ssh:
      user: root
      port: 22
```

Root password authentication не используется; доступ выдаётся public SSH keys.

## VMID-addressing

В каждый момент времени у гостя один management IPv4 и один default gateway.

Обычный management IP вычисляется из VMID:

```text
VMID XYZ + subnet A.B.0.0/16
→ A.B.X.YZ
```

Примеры для текущей сети:

```text
109 → 192.168.1.9
211 → 192.168.2.11
301 → 192.168.3.1
311 → 192.168.3.11
321 → 192.168.3.21
331 → 192.168.3.31
401 → 192.168.4.1
```

Префикс `/16` берётся из `defaults.network.subnet` и в `guest.yaml` не дублируется.

## Обычный deployable guest

Типичный `311-dev-services/guest.yaml`:

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

Секция `network` отсутствует полностью, потому что bridge, subnet, gateway и management IP однозначно выводятся из `defaults.yaml` и VMID.

## Необязательный IP override

Если конкретному гостю действительно нужен адрес, не соответствующий VMID-формуле, допускается только явный override самого host-IP:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

Указывать `/16` нельзя. Префикс всё равно приходит из `defaults.network.subnet`.

Validator выводит предупреждение, если override не соответствует VMID-формуле. Если override совпадает с вычисляемым адресом, validator предупреждает, что он избыточен.

`subnet`, `gateway` и `addressing` на уровне отдельного guest не переопределяются.

## Миграция сети

Для перехода, например, с:

```text
192.168.0.0/16
gateway 192.168.1.1
```

на:

```text
10.0.0.0/16
gateway 10.0.0.1
```

меняются central values в `guests/defaults.yaml`:

```yaml
defaults:
  network:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1
```

После этого effective IP автоматически пересчитывается по VMID.

Для guest с явным `network.ipv4.address` override адрес не пересчитывается; при смене subnet такой override нужно проверить осознанно.

## Profiles

Текущие profiles:

```text
debian-vm
→ Full Clone from VM template 9000 Template-Version 6
→ QEMU guest agent
→ root key-only SSH через Cloud-Init

docker-lxc
→ Debian 13 LXC family selector local:vztmpl/debian-13-standard
→ runtime resolution наиболее нового установленного archive этого семейства
→ unprivileged + nesting + keyctl
→ root key-only SSH через ssh-public-keys
```

Конкретная версия LXC archive **не хранится** в defaults.

Profiles не должны переопределять общую сеть.

## SSH identities

Один Linux-user `root` может иметь несколько независимых public keys:

```text
PVE/deployer key
AI Control key
Ansible/311 key
personal key при необходимости
```

Pool `managed` и набор SSH keys являются независимыми механизмами. Изменение pool membership не должно автоматически редактировать `authorized_keys`.

## `deployable`

```text
deployable: true
→ defaults/profile применяются
→ вычисляется management IP
→ строится полный effective desired state
→ deploy-guest может PLAN/APPLY

deployable: false
→ universal deployer объект не создаёт и не пересоздаёт
→ manifest может содержать только известные/наблюдаемые параметры
```

## Validator

`python scripts/validate_repo.py` анализирует проектные данные из:

```text
guests/defaults.yaml
guests/*/guest.yaml
```

Schema-файлы задают машинные правила.

Блокирующие ошибки включают, в частности:

- schema/YAML ошибки;
- duplicate VMID/IP;
- management IP вне central subnet;
- IP, совпадающий с gateway/network/broadcast;
- неправильный central subnet/gateway;
- `ballooning_mb > memory_mb`;
- нарушение profile/source contracts;
- `management.ssh`, отличный от `root:22` для deployable guest;
- versioned LXC archive вместо family selector;
- secrets/private keys в `defaults.yaml` или `guest.yaml`.

Warnings не делают CI красным и используются для подозрительных, но не обязательно неправильных overrides/state combinations.

## Deploy

`deploy-guest` должен показывать provenance effective values, например:

```text
Node               pve                         [defaults]
Pool               managed                     [defaults]
Storage            local-lvm                   [defaults]
Profile            docker-lxc                  [guest]
LXC selector       debian-13-standard          [profile]
Resolved archive   debian-13-standard_<...>    [runtime PVE]
SSH user           root                        [defaults]
Network subnet     192.168.0.0/16              [defaults]
Default gateway    192.168.1.1                 [defaults]
Management IP      192.168.3.11/16             [vmid]
Memory             4096 MiB                    [guest]
```

Initial SSH access является частью deploy readiness и не зависит от optional `base` capability.

Главный принцип:

> В `guest.yaml` хранится только действительно индивидуальное. Общая сеть и management user централизованы; IP выводится из VMID; runtime-версия LXC appliance и фактические SSH authorized keys не хардкодятся в guest defaults.
