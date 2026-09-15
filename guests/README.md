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

Все значения находятся в Git. Deployer не должен иметь скрытых project defaults.

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

Validator выводит русское предупреждение, если override не соответствует VMID-формуле:

```text
management IP 192.168.8.50 не соответствует правилу VMID 311;
ожидается 192.168.3.11
```

Если override совпадает с вычисляемым адресом, validator предупреждает, что он избыточен.

`subnet`, `gateway` и `addressing` на уровне отдельного guest не переопределяются.

## Миграция сети

Два IP, `current/target`, `dual` и два набора gateway больше не используются.

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

меняются только два значения в `guests/defaults.yaml`:

```yaml
defaults:
  network:
    subnet: 10.0.0.0/16
    gateway: 10.0.0.1
```

После этого effective IP автоматически станет:

```text
109 → 10.0.1.9
211 → 10.0.2.11
301 → 10.0.3.1
311 → 10.0.3.11
...
```

Будущий migration/deploy script должен применять изменение к гостям последовательно: изменить штатную Proxmox network-конфигурацию, перезапустить при необходимости, проверить новый IP и только после успешной проверки переходить к следующему гостю.

Для гостя с явным `network.ipv4.address` override адрес не пересчитывается. При смене subnet такой override нужно изменить осознанно; если он окажется вне новой subnet, validator выдаст ошибку.

## Profiles

Текущие profiles:

```text
debian-vm
→ full clone from VM template 9000
→ QEMU guest agent

docker-lxc
→ pinned Debian 13 LXC appliance
→ unprivileged + nesting + keyctl
```

Profiles не должны переопределять общую сеть.

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

`python scripts/validate_repo.py` анализирует проектные данные только из:

```text
guests/defaults.yaml
guests/*/guest.yaml
```

Schema-файлы используются только как правила проверки.

Validator не читает `docs/*.md`, README, `rootfs/` и другие проектные файлы.

Блокирующие ошибки включают, в частности:

- schema/YAML ошибки;
- duplicate VMID/IP;
- management IP вне central subnet;
- IP, совпадающий с gateway;
- network/broadcast IP;
- неправильный central subnet/gateway;
- `ballooning_mb > memory_mb`;
- нарушение profile/source contracts;
- secrets/private keys в `defaults.yaml` или `guest.yaml`.

Неблокирующие предупреждения на русском языке включают:

- явный IP override не соответствует VMID-формуле;
- избыточный IP override;
- избыточное значение, уже наследуемое из defaults/profile;
- override общего node/bridge/storage/SSH;
- неиспользуемый profile;
- подозрительные state/boot/protection combinations.

## Deploy

`deploy-guest` должен показывать provenance каждого effective значения:

```text
Node               pve                 [defaults]
Pool               managed             [defaults]
Storage            local-lvm           [defaults]
Profile            docker-lxc          [guest]
Network subnet     192.168.0.0/16      [defaults]
Default gateway    192.168.1.1         [defaults]
Management IP      192.168.3.11/16     [vmid]
Memory             4096 MiB            [guest]
```

Если задан IP override, provenance адреса меняется на `[guest]`.

Главный принцип:

> В `guest.yaml` хранится только действительно индивидуальное. Сеть является общей, а обычный management IP детерминированно выводится из VMID.
