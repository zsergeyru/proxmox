# Guests

Каждый каталог `guests/<VMID>-<name>/` соответствует одной VM/LXC и содержит её паспорт, локальные решения и управляемые файлы гостевой ОС.

Актуальная нумерация определяется только [`../docs/vmid-plan.md`](../docs/vmid-plan.md).

Подробная машинно-читаемая спецификация `guest.yaml` и правила универсального PVE-deployer описаны в [`../docs/guest-manifest.md`](../docs/guest-manifest.md).

## Структура

```text
<VMID>-<name>/
├── README.md
├── guest.yaml
├── decisions/        # при необходимости
├── STATUS.md         # временный observed drift/миграция
└── rootfs/           # управляемые файлы по реальным абсолютным путям
```

`README.md` описывает назначение и особенности гостя. `guest.yaml` описывает объект Proxmox. `rootfs/` содержит только файлы, которыми управляет проект, а не полный образ `/`.

## guest.yaml

Schema version: `1`.

Обязательные поля:

```yaml
schema_version: 1
vmid: 109
name: network-gateway
type: vm
state: planned
```

Допустимые `state`:

```text
planned   → запланирован, ещё не рабочий
active    → рабочий production/целевой объект
bootstrap → временный рабочий объект для развёртывания/миграции
legacy    → старый объект, сохраняемый до планового вывода
```

Нельзя переводить гостя в `active` только потому, что каталог создан в Git.

### Protection

Для обычных управляемых VM/LXC политика по умолчанию:

```yaml
protection: false
```

Поле можно не дублировать в каждом `guest.yaml`, если используется default `false`. `protection: true` фиксируется только для объектов, которые действительно должны быть защищены от случайного удаления.

Base template `9000` — отдельное исключение и всегда остаётся `protection=1`.

При Full Clone из защищённого template deploy-сценарий обязан **явно** выставить новому гостю `protection=0`, если в его паспорте не указано `protection: true`. Нельзя снимать protection с template ради создания обычных VM.

### Ресурсы

В `guest.yaml` фиксируются только параметры, которыми действительно управляет проект:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 2048
  disk:
    size_gb: 16
    storage: local-lvm
```

Не нужно копировать все значения Proxmox по умолчанию. Для автоматического deploy конкретный manifest должен содержать все параметры, без которых его тип нельзя создать однозначно.

### Сеть

Для planned-гостей фиксируются сразу два состояния: текущая сеть и целевая сеть после миграции.

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

Обе адресации выводятся из VMID по единому правилу:

```text
current: VMID XYZ → 192.168.X.YZ/16
target:  VMID XYZ → 10.0.X.YZ/16
```

Это описание этапов миграции, а не требование одновременно иметь два default gateway. В работающей ОС default gateway должен быть один. Подробности — в [`../docs/network.md`](../docs/network.md).

### Управление

Для обычных Debian VM из базового template единый административный SSH-пользователь:

```yaml
management:
  ssh:
    user: ops
    port: 22
```

`ops` используется человеком и Ansible/AI-управлением; разграничение выполняется отдельными SSH-ключами и sudo-политиками.

Целевой `301-ai-control` создаёт свою infrastructure identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Именно содержимое `.pub` может передаваться обычным новым Debian VM через Cloud-Init. Private key остаётся только в `301`.

GitHub Deploy Key `github_proxmox_ed25519` — отдельная identity и **не используется** для доступа к гостям.

Пароли, токены и private keys в `guest.yaml` не хранятся.

### VM/LXC-специфичные параметры и source

Для автоматического создания source должен быть явным.

Обычная VM из template:

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

Deployer не должен предполагать, что любая `type: vm` всегда создаётся из `9000`.

LXC:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard_13.x-amd64.tar.zst
  unprivileged: true
  nesting: true
```

Пустые секции не добавляются. Полные правила и примеры — в [`../docs/guest-manifest.md`](../docs/guest-manifest.md).

## rootfs

`rootfs/` повторяет абсолютные пути внутри гостя:

```text
guests/109-network-gateway/rootfs/etc/nftables.conf
→ /etc/nftables.conf

guests/301-ai-control/rootfs/opt/ai-control/...
→ /opt/ai-control/...
```

Отдельных универсальных каталогов `services/`, `docker/`, `dns/`, `vpn/` и подобных не создаём, если это обычные файлы гостевой ОС.

Сам monorepo целиком на каждый гость не копируется.

## Deploy

Есть два уровня deploy.

### PVE-side создание объекта

Универсальный deployer на самом PVE должен быть независим от AI:

```text
read-only checkout zsergeyru/proxmox
→ deploy-guest <VMID>
→ guest.yaml
→ qm/pct/pvesh/pvesm
→ VM/LXC
```

По умолчанию он строит PLAN; фактическое применение требует явного `--apply`. Подробности: [`../docs/guest-manifest.md`](../docs/guest-manifest.md) и [`../docs/pve-initialization.md`](../docs/pve-initialization.md).

### Управление после появления AI/DevOps

```text
Proximo в 301-ai-control
→ lifecycle/guest-level операции Proxmox

Ansible на 311-dev-services
→ повторяемая настройка ОС и применение rootfs по SSH

прямой SSH
→ bootstrap, диагностика, разовые и аварийные действия

Semaphore
→ необязательный web-интерфейс к тем же Ansible playbook
```

Для обычной Debian VM из `tpl-debian13` порядок уровня Proxmox:

```text
Full Clone from 9000
→ managed pool при необходимости
→ protection по manifest
→ CPU/RAM/disk/network
→ ciuser=ops
→ optional sshkeys=<301:/opt/ai-control/ssh/ai_control_ed25519.pub>
→ Cloud-Init update
→ start
→ QEMU Agent/SSH/health check
```

Base template `9000` не содержит ключей. `301` не передаёт private key новой VM — только соответствующий public key.

Для полностью принадлежащих проекту каталогов (`/opt/<service>/`) допустима синхронизация с удалением отсутствующих файлов. В общих системных каталогах (`/etc`, `/usr/local/bin`, `/etc/systemd/system`) файлы устанавливаются и удаляются только по конкретным путям.

Глобальный эквивалент `rsync --delete rootfs/ /` запрещён.

Persistent data, Docker volumes, базы данных, записи камер, пользовательские Git-репозитории, runtime state и секреты не относятся к `rootfs/`.

Подробное решение по repeatable deploy: [`311-dev-services/decisions/001-deployment-tooling.md`](311-dev-services/decisions/001-deployment-tooling.md).

## Текущие исключения

- `100-haos` — действующая production VM Home Assistant и сохраняет исторический VMID `100`;
- `201-ha-main` — только возможная будущая цель миграции и не создаётся ради унификации;
- `301-ai-control` — control plane и не должен автоматически попадать в собственную обычную write-зону;
- `320-ai-control` — временный bootstrap до проверки `301-ai-control`;
- `9000 tpl-debian13` — protected base template, разрешён как источник clone, но не как managed guest;
- `501-frigate` может не иметь полного manifest до выбора VM/LXC.

## Общие правила

- `proxmox` остаётся основным инфраструктурным monorepo;
- Ansible roles/playbooks находятся в `/ansible` и исполняются из `311-dev-services`;
- шаблоны VM/LXC находятся в `/templates`;
- фиктивные Compose-файлы и пустые конфиги заранее не создаются;
- отдельный репозиторий появляется только у действительно независимого компонента;
- секреты, токены, пароли и private keys в Git не добавляются.
