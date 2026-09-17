# `guest.yaml` — основной источник требуемого состояния VM/LXC

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для схем `guest.yaml`/`defaults.yaml`, правил построения итогового состояния и требований к развёртываемой гостевой системе.

## Назначение

Требуемое состояние гостевой системы строится из одного Git-коммита:

```text
guests/defaults.yaml
        +
профиль из defaults.yaml
        +
guests/<VMID>-<name>/guest.yaml
        +
детерминированные правила проекта
        ↓
scripts/guest_config.py
        ↓
итоговое требуемое состояние
        ↓
проверка → ПЛАН → ПРИМЕНЕНИЕ → проверка результата
```

`guest.yaml` описывает PVE-состояние конкретной VM/LXC и, при необходимости, явно запрошенную ограниченную первичную настройку Guest Bootstrap. Повторяемая конфигурация ОС и приложений остаётся зоной Ansible; `rootfs/` не применяется скрыто средством `deploy-guest`.

## Версии схем

Исходный манифест гостевой системы и итоговое состояние:

```yaml
schema_version: 6
```

Версия 6 вводит действующий машинный интерфейс `bootstrap.capabilities`.

Центральные общие настройки:

```yaml
schema_version: 2
```

Используются три схемы:

```text
schemas/guest.schema.yaml
→ компактный исходный guest.yaml

schemas/guest-defaults.schema.yaml
→ guests/defaults.yaml

schemas/guest-effective.schema.yaml
→ полное итоговое состояние после объединения и вычисления IP
```

## `guests/defaults.yaml`

Основной пример:

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

Имена полей YAML не переводятся: это машинный интерфейс проекта.

## Правила объединения

Порядок строго определён:

```text
общие настройки
→ профиль
→ guest.yaml
→ вычисление административного IP
```

Словари объединяются рекурсивно; более позднее простое значение или `null` перекрывает более раннее.

Для развёртываемой гостевой системы поля `type`, `vm` и `lxc` принадлежат профилю. Индивидуальный манифест не должен копировать их только ради наглядности.

`bootstrap` является исключением из обычного наследования: в v1 он задаётся только явно в конкретном `guest.yaml`. `defaults.yaml` и профили не имеют поля `bootstrap`. Это исключает скрытую установку ПО во всех гостях при одном изменении общего профиля.

## Guest Bootstrap v1

Если гостю нужна первичная настройка после появления проверенного административного SSH, она задаётся явно:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Разрешённый набор v1:

```text
base
git
docker
ansible_controller
```

Отсутствие блока `bootstrap` означает: `deploy-guest` не выполняет внутри гостевой ОС никакую первичную настройку после SSH-проверки.

Если блок присутствует, должна быть включена хотя бы одна capability. Поля с неизвестными именами запрещены схемой. Значение каждой capability — только boolean.

Зависимости задаются явно, без неявного автоматического включения:

```text
base

git
└─ требует base

docker
└─ требует base

ansible_controller
├─ требует base
├─ требует git
└─ требует docker
```

Канонический порядок выполнения:

```text
base → git → docker → ansible_controller
```

Если зависимость нужна, но в манифесте не указана как `true`, проверка репозитория завершается ошибкой. Resolver не должен молча добавлять capability за пользователя.

Guest Bootstrap выполняется только после успешного `root SSH`, поэтому наличие включённого Bootstrap требует:

```yaml
boot:
  start_after_deploy: true
```

Bootstrap v1 не является универсальным механизмом выполнения команд. В `guest.yaml` запрещено вводить произвольные `packages`, shell-команды, URL установщиков, пароли, токены или закрытые ключи. Точная граница capabilities определена в [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## Требования к административному SSH

Для всех развёртываемых Debian VM/LXC итоговое состояние должно содержать:

```yaml
management:
  ssh:
    user: root
    port: 22
```

Пароль `root` не является частью манифеста и не хранится в Git. Политика проекта требует заблокированного пароля `root` и SSH-доступа только по открытым ключам.

Разные контуры управления используют разные пары SSH-ключей, но одного Linux-пользователя `root`:

```text
ключ PVE/deploy-guest
ключ AI Control
ключ Ansible/311
личный ключ при необходимости
```

Наличие конкретного открытого ключа в `/root/.ssh/authorized_keys` не определяется автоматически членством в пуле PVE.

Начальный SSH-доступ — часть готовности гостевой системы и существует независимо от `bootstrap.capabilities.base`.

## Источник VM

VM-профиль задаёт источник:

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
  guest_agent: true
```

Первая версия `deploy-guest` поддерживает только Full Clone. Шаблон и его версия не дублируются в каждом `guest.yaml`.

## Источник LXC

Для LXC Git хранит **семейство** шаблона, а не конкретную версию архива:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard
```

Не допускаются конкретные имена архивов, `latest`, `*`, `13.x`, `tbd` и другие плавающие обозначения.

PVE Configuration подготавливает подходящий Debian 13 template. `deploy-guest` во время PLAN:

```text
прочитать lxc.source.ostemplate
→ получить установленные архивы на разрешённом storage
→ детерминированно выбрать наиболее новый подходящий архив
→ зафиксировать его в плане текущего запуска
```

Если подходящего архива нет, запуск останавливается до изменений. `deploy-guest` не скачивает LXC-template сам.

## Начальные SSH-ключи

### VM

Шаблон `9000` использует `ciuser=root`, заблокированный пароль `root` и SSH только по ключам, но не содержит постоянный проектный `authorized_keys`.

До первого запуска полный клон получает открытый ключ PVE через Cloud-Init. Закрытый ключ внутрь VM не передаётся.

### LXC

При создании LXC `deploy-guest` передаёт открытый ключ PVE через штатный `ssh-public-keys`, после чего проверяет вход `root` по SSH.

## Сеть

### Один активный административный IP

Центральная сеть задаётся только в `guests/defaults.yaml`:

```yaml
network:
  bridge: vmbr0
  subnet: 192.168.0.0/16
  gateway: 192.168.1.1
  addressing: vmid
```

У обычной гостевой системы один административный IPv4 и один gateway по умолчанию.

### Адресация по VMID

Для `/16` действует правило:

```text
VMID XYZ + A.B.0.0/16
→ A.B.X.YZ
```

Примеры:

```text
311 + 192.168.0.0/16 → 192.168.3.11/16
311 + 10.0.0.0/16    → 10.0.3.11/16
```

В обычном `guest.yaml` адрес не записывается.

### Необязательное переопределение

Для исключения разрешено только явное указание bare IPv4:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

Префикс берётся только из `defaults.network.subnet`. Отдельный guest не может менять общий `subnet`, `gateway` или способ адресации.

Явный адрес вне формулы VMID вызывает предупреждение. Адрес вне центральной подсети, дублирующий адрес, gateway, network или broadcast — ошибка.

## Миграция сети

Переезд сети — изменение центрального требуемого состояния. Обычный `deploy-guest <VMID>` не реализует массовую миграцию и временный dual-IP.

Для массовой миграции нужен отдельный управляемый режим/инструмент, который фиксирует Git revision, переключает гостей по одному и проверяет доступность после каждого шага.

## Компактный манифест

Обычный deployable-гость без Bootstrap:

```yaml
schema_version: 6
vmid: 321
name: app-services
profile: docker-lxc
state: planned
deployable: true

description: General application services including Homarr

resources:
  cpu:
    cores: 2
  memory_mb: 2048
  swap_mb: 512
  disk:
    size_gb: 24
```

`311-dev-services` дополнительно явно включает Bootstrap:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Из общих настроек и профиля будут получены node, pool, storage, сеть, параметры запуска, SSH-пользователь/порт, тип и источник гостя. IP вычисляется из VMID.

## `state` и `deployable`

`state` — жизненная стадия и документация:

```yaml
state: planned | active | bootstrap | legacy
```

Она сама по себе не является командой. Право `deploy-guest` управлять объектом задаёт прежде всего:

```yaml
deployable: true
```

Поэтому `state: planned` вместе с `deployable: true` разрешает создание. `legacy` не означает удаление.

`deployable: false` не получает параметры развёртывания автоматически и может описывать наблюдаемый или зарезервированный объект. Такой объект не может содержать `bootstrap`.

## Зарезервированные и наблюдаемые манифесты

Пример:

```yaml
schema_version: 6
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

`type: undecided` всегда означает `deployable: false`.

## Происхождение значений в PLAN

PLAN по возможности показывает источник требуемого значения:

```text
Node               pve                         [defaults]
Pool               managed                     [defaults]
Storage            local-lvm                   [defaults]
Profile            docker-lxc                  [guest]
LXC selector       debian-13-standard          [profile]
Resolved archive   debian-13-standard_<...>    [runtime PVE]
SSH user           root                        [defaults]
Management IP      192.168.3.11/16             [vmid]
Memory             4096 MiB                    [guest]
Disk size          32 GiB                      [guest]
Bootstrap           base,git,docker,...          [guest]
```

Факты конкретного запуска, например конкретный LXC-архив, не записываются обратно в manifest.

## Проверка репозитория

Основной валидатор:

```bash
python scripts/validate_repo.py
```

Он проверяет исходные схемы, `defaults.yaml`, профили, структуру каталогов, конфликты VMID/IP, секретоподобные поля, ограничения VM/LXC и итоговое состояние, построенное через `scripts/guest_config.py`.

Семантика Bootstrap также проверяется общим resolver, которым пользуются validator и будущий `deploy-guest`. Второй набор правил Bootstrap в `deploy-guest.py` создавать нельзя.

## Git как основной источник

```text
guests/defaults.yaml
+
profile
+
guest.yaml
+
детерминированные правила guest_config.py
=
итоговое требуемое состояние
```

Факты, зависящие от конкретного PVE, не превращаются в скрытые значения по умолчанию или автоматически переписываемые поля Git.

Связанные основные документы:

- [`31-deploy-guest.md`](31-deploy-guest.md) — PLAN/APPLY и безопасное применение;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — Guest Bootstrap v1 и граница Ansible;
- [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md) — Docker внутри LXC;
- [`23-security.md`](23-security.md) — ключи и безопасность.
