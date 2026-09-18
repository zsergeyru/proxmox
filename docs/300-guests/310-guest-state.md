# Требуемое состояние гостевой системы

**Тип:** спецификация  
**Статус:** действующий  
**Назначение:** определить источники, параметры, правила построения и проверки требуемого состояния VM/LXC.

Этот документ отвечает на вопрос:

> Как в проекте описывается требуемое состояние конкретной гостевой системы и как из исходных файлов получается итоговое effective state?

Документ не описывает порядок применения изменений к Proxmox. Контракт PLAN/APPLY и жизненный цикл команды `deploy-guest` относятся к отдельной спецификации развёртывания.

## Содержание

- [1. Назначение и модель состояния](#1-назначение-и-модель-состояния)
- [2. Источники и построение состояния](#2-источники-и-построение-состояния)
- [3. Параметры гостевой системы](#3-параметры-гостевой-системы)
- [4. Правила и ограничения](#4-правила-и-ограничения)
- [5. Проверка состояния](#5-проверка-состояния)
- [6. Связанные документы](#6-связанные-документы)

## 1. Назначение и модель состояния

### 1.1. Назначение документа

Для управляемой VM/LXC проект хранит не готовую конфигурацию Proxmox, а исходное описание требуемого состояния.

Оно должно быть:

- компактным;
- проверяемым;
- воспроизводимым;
- однозначно преобразуемым в итоговое состояние;
- пригодным для сравнения с фактическим состоянием Proxmox.

Основной индивидуальный файл гостя:

```text
guests/<VMID>-<name>/guest.yaml
```

Общие значения и профили находятся в:

```text
guests/defaults.yaml
```

Машинные ограничения формата задаются схемами в `schemas/`.

### 1.2. Что считается требуемым состоянием

Требуемое состояние определяет, каким должен быть объект VM/LXC с точки зрения проекта.

К нему относятся, в частности:

- идентификатор и имя;
- тип гостевой системы;
- размещение;
- процессор, память и диск;
- сеть;
- правила запуска;
- административный management-контур;
- явно запрошенная первоначальная подготовка;
- параметры, специфичные для VM или LXC.

Работающая VM/LXC в Proxmox хранит фактическое состояние, но сама по себе не становится источником требований.

Изменение требуемого состояния выполняется через соответствующие файлы проекта.

### 1.3. Исходное и итоговое состояние

В проекте различаются два представления состояния.

**Исходное состояние** — компактные данные, которые человек редактирует в Git:

```text
guests/defaults.yaml
+
профиль
+
guests/<VMID>-<name>/guest.yaml
```

**Effective state** — полное итоговое состояние после наследования, объединения, нормализации и вычисления производных значений.

Общая схема:

```text
исходные данные
→ schema validation
→ resolver
→ effective state
→ проверка effective state
```

Средства проверки и развёртывания должны использовать одно и то же effective state и не реализовывать собственные параллельные правила его построения.

## 2. Источники и построение состояния

### 2.1. Источники состояния

Требуемое состояние строится из трёх основных источников.

`guests/defaults.yaml` содержит:

- общие значения проекта;
- параметры, одинаковые для большинства гостей;
- определения профилей VM/LXC.

Профиль определяет типовую основу конкретного класса гостевых систем.

Например:

```yaml
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

Индивидуальный `guest.yaml` содержит параметры конкретной VM/LXC и осознанные отклонения от общих значений.

Обычный deployable-гость не должен копировать в свой `guest.yaml` значения, которые уже однозначно приходят из `defaults.yaml` или выбранного профиля.

### 2.2. Порядок объединения и переопределения

Порядок построения состояния фиксирован:

```text
defaults
→ profile
→ guest.yaml
→ специальные правила
→ вычисляемые значения
→ effective state
```

Словари объединяются рекурсивно.

Более позднее простое значение заменяет более раннее.

Значение `null`, если оно разрешено схемой, является явным значением и также перекрывает унаследованное значение.

Пример:

```yaml
placement:
  pool: null
```

означает намеренное отсутствие обычного pool, а не отсутствие настройки.

Избыточное повторение унаследованного значения нежелательно: оно усложняет чтение манифеста и может скрывать последующие изменения общих настроек.

### 2.3. Специальные и вычисляемые значения

Не все поля следуют обычному наследованию.

Некоторые параметры разрешено задавать только конкретному гостю.

К таким полям относятся:

```text
management.ssh_identity
management.project_repo_read
bootstrap
```

Они не должны появляться в общих defaults или профилях.

Некоторые значения вообще не хранятся в обычном `guest.yaml`, а вычисляются из других параметров.

Главный пример — административный IPv4.

Если индивидуальное переопределение отсутствует, адрес вычисляется из VMID и центральной management-подсети.

### 2.4. Итоговое effective state

Общий resolver проекта:

```text
scripts/guest_config.py
```

строит deterministic effective state.

Для deployable-гостя итоговое состояние должно содержать все обязательные параметры, необходимые следующим слоям проекта.

В частности, resolver:

- объединяет defaults, профиль и индивидуальный манифест;
- нормализует individual-only management-поля;
- вычисляет административный IP;
- проверяет зависимости Bootstrap capabilities;
- возвращает включённые capabilities в каноническом порядке.

Машинный формат итогового состояния проверяется схемой:

```text
schemas/guest-effective.schema.yaml
```

Effective state является интерфейсом между описанием гостя и средствами, которые сравнивают или применяют это состояние.

## 3. Параметры гостевой системы

Этот раздел является справочником параметров исходного состояния.

Для каждого параметра указывается:

- его тип;
- где он задаётся или откуда наследуется;
- когда он обязателен;
- что он означает.

Точные взаимосвязи между полями и дополнительные ограничения собраны в разделе 4.

### 3.1. Основные параметры

Основные параметры определяют идентичность гостя, его место в проекте и возможность автоматического развёртывания.

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `schema_version` | integer, фиксированное значение `7` | `guest.yaml` | всегда | версия машинного контракта исходного манифеста |
| `vmid` | integer, `100–999` | `guest.yaml` | всегда | VMID/CTID гостевой системы |
| `name` | string | `guest.yaml` | всегда | имя гостя; должно совпадать с именем каталога после VMID |
| `description` | непустая string | `guest.yaml` | всегда | краткое назначение гостевой системы |
| `profile` | string | `guest.yaml` | для `deployable: true` | имя профиля из `guests/defaults.yaml` |
| `type` | enum: `vm`, `lxc`, `undecided` | профиль либо `guest.yaml` | зависит от `deployable` | тип гостевой системы |
| `state` | enum: `planned`, `active`, `bootstrap`, `legacy` | `guest.yaml` | всегда | логическое состояние объекта в проекте |
| `deployable` | boolean | `guest.yaml` | всегда | разрешено ли универсальному контуру развёртывать гостя |
| `node` | string | обычно `defaults`, допускается override | в effective state | PVE-узел |
| `protection` | boolean | обычно `defaults`, допускается override | в effective state | защита VM/LXC средствами Proxmox |
| `placement.pool` | string или `null` | обычно `defaults`, допускается override | в effective state | pool Proxmox или явное отсутствие pool |

Имена `name` и `profile` используют простой машинный формат: строчные латинские буквы, цифры и дефис. Имя начинается с буквы или цифры.

Каталог гостя имеет форму:

```text
<VMID>-<name>
```

Например:

```text
311-dev-services/
└── guest.yaml
```

Начало соответствующего манифеста:

```yaml
schema_version: 7
vmid: 311
name: dev-services
profile: docker-lxc
state: planned
deployable: true

description: Ansible, Semaphore, Git, CI and development services
```

Для deployable-гостя `type` в обычном `guest.yaml` не повторяется. Он приходит из выбранного профиля:

```yaml
profiles:
  docker-lxc:
    type: lxc
```

Для объекта, который пока только описан и не должен развёртываться, допустима другая модель:

```yaml
schema_version: 7
vmid: 550
name: future-service
type: undecided
state: planned
deployable: false

description: Reserved guest for a future service
```

Явное исключение из обычного pool задаётся через `null`:

```yaml
placement:
  pool: null
```

Это именно значение, а не отсутствие параметра.

### 3.2. Ресурсы

Раздел `resources` описывает вычислительные ресурсы и основной диск гостя.

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `resources.cpu.cores` | integer, минимум `1` | обычно `guest.yaml` | для deployable-гостя | число виртуальных CPU |
| `resources.cpu.type` | непустая string | defaults/profile/guest | необязательно | тип CPU Proxmox, если нужен явный выбор |
| `resources.memory_mb` | integer, минимум `256` | обычно `guest.yaml` | для deployable-гостя | оперативная память в MiB |
| `resources.ballooning_mb` | integer, минимум `0` | defaults/profile/guest | необязательно | нижняя граница ballooning для VM |
| `resources.swap_mb` | integer, минимум `0` | defaults/profile/guest | обязателен в effective LXC | swap LXC в MiB |
| `resources.disk.size_gb` | integer, минимум `1` | обычно `guest.yaml` | для deployable-гостя | требуемый размер основного диска |
| `resources.disk.storage` | непустая string | обычно `defaults`, допускается override | в effective state | PVE storage для основного диска |

Типичный индивидуальный блок ресурсов LXC:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 4096
  swap_mb: 1024
  disk:
    size_gb: 32
```

При этом общее хранилище не требуется повторять в каждом госте:

```yaml
# guests/defaults.yaml
defaults:
  resources:
    disk:
      storage: local-lvm
```

После объединения effective state будет содержать и размер, и storage:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 4096
  swap_mb: 1024
  disk:
    size_gb: 32
    storage: local-lvm
```

Пример ресурсов VM:

```yaml
resources:
  cpu:
    cores: 2
  memory_mb: 4096
  disk:
    size_gb: 24
```

`ballooning_mb`, если используется, не может превышать `memory_mb`.

Например:

```yaml
resources:
  memory_mb: 4096
  ballooning_mb: 2048
```

допустимо, а `ballooning_mb: 8192` при `memory_mb: 4096` — нет.

### 3.3. Сеть и запуск

Сетевые параметры разделены на центральные настройки сети и индивидуальное состояние гостя.

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `network.bridge` | непустая string | `defaults`; guest override допускается | в effective state | PVE bridge |
| `network.subnet` | IPv4 network string | только центральные defaults | в defaults | management-подсеть проекта |
| `network.gateway` | IPv4 address string | только центральные defaults | в defaults | общий gateway |
| `network.addressing` | фиксированное значение `vmid` | только центральные defaults | в defaults | правило автоматического вычисления IP |
| `network.ipv4.address` в source | bare IPv4 string без prefix | только конкретный `guest.yaml` | необязательно | явное исключение из VMID-адресации |
| `network.ipv4.address` в effective state | IPv4 interface string с prefix | вычисляется resolver | всегда для deployable-гостя | итоговый management IPv4 |

Центральная сеть выглядит, например, так:

```yaml
# guests/defaults.yaml
network:
  bridge: vmbr0
  subnet: 192.168.0.0/16
  gateway: 192.168.1.1
  addressing: vmid
```

Для VMID `311` обычный `guest.yaml` вообще не содержит IP. Resolver получает:

```yaml
network:
  bridge: vmbr0
  subnet: 192.168.0.0/16
  gateway: 192.168.1.1
  addressing: vmid
  ipv4:
    address: 192.168.3.11/16
```

Если гостю нужен осознанный нестандартный адрес, в source-манифесте задаётся только bare IPv4:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

Записывать prefix в индивидуальном поле нельзя:

```yaml
# неверно
network:
  ipv4:
    address: 192.168.8.50/16
```

Параметры запуска:

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `boot.onboot` | boolean | обычно `defaults`, допускается override | в effective state | запускать ли гостя при старте PVE |
| `boot.start_after_deploy` | boolean | обычно `defaults`, допускается override | в effective state | запускать ли гостя после инфраструктурного развёртывания |

Базовая политика проекта сейчас задаётся централизованно:

```yaml
boot:
  onboot: true
  start_after_deploy: true
```

Индивидуальное изменение возможно только когда оно действительно требуется:

```yaml
boot:
  onboot: false
```

Связанные ограничения для management и Bootstrap описаны в разделе 4.

### 3.4. Управление и первоначальная подготовка

Раздел `management` описывает административный доступ и дополнительные управляемые возможности гостя.

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `management.ssh.user` | string; для управляемых Debian — `root` | обычно `defaults` | в effective state | пользователь административного SSH |
| `management.ssh.port` | integer, `1–65535`; проектный контракт — `22` | обычно `defaults` | в effective state | порт административного SSH |
| `management.ssh_identity` | boolean | только конкретный `guest.yaml` | необязательно в source; boolean в effective | собственная исходящая management SSH identity |
| `management.project_repo_read` | boolean | только конкретный `guest.yaml` | необязательно в source; boolean в effective | управляемый read-only доступ к репозиторию проекта |

Общий административный контракт задаётся один раз:

```yaml
# guests/defaults.yaml
management:
  ssh:
    user: root
    port: 22
```

Обычному гостю не требуется повторять этот блок.

Гость, которому нужна собственная management identity:

```yaml
management:
  ssh_identity: true
```

Гость, которому дополнительно нужен read-only доступ к проектному Git:

```yaml
management:
  ssh_identity: true
  project_repo_read: true
```

Если individual-only поле отсутствует, effective state нормализует его в `false`. Например, source:

```yaml
management:
  ssh_identity: true
```

даёт в effective state:

```yaml
management:
  ssh:
    user: root
    port: 22
  ssh_identity: true
  project_repo_read: false
```

Первоначальная подготовка задаётся отдельным блоком `bootstrap`.

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `bootstrap.capabilities` | object/mapping | только конкретный `guest.yaml` | только если нужен Bootstrap | набор явно включённых возможностей |
| `bootstrap.capabilities.base` | boolean | `guest.yaml` | по необходимости | базовая подготовка |
| `bootstrap.capabilities.git` | boolean | `guest.yaml` | по необходимости | подготовка Git |
| `bootstrap.capabilities.docker` | boolean | `guest.yaml` | по необходимости | подготовка Docker |
| `bootstrap.capabilities.ansible_controller` | boolean | `guest.yaml` | по необходимости | подготовка Ansible controller |

Минимальный Bootstrap:

```yaml
bootstrap:
  capabilities:
    base: true
```

Bootstrap для Docker-гостя:

```yaml
bootstrap:
  capabilities:
    base: true
    docker: true
```

Полный набор для управляющего DevOps-гостя:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Если блок `bootstrap` присутствует, хотя бы одна capability должна иметь значение `true`.

Неизвестные capability names не разрешены.

Зависимости capabilities и требования к запуску гостя описаны в разделе 4.

### 3.5. Параметры VM и LXC

Параметры типа гостя обычно определяются профилем, а не индивидуальным `guest.yaml`.

Для VM используется раздел `vm`:

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `vm.source.template_vmid` | integer, `100–9999` | VM-профиль | для deployable VM | VMID исходного шаблона |
| `vm.source.clone` | enum, сейчас только `full` | VM-профиль | для deployable VM | способ клонирования |
| `vm.guest_agent` | boolean | VM-профиль | для deployable VM | использование QEMU Guest Agent |
| `vm.bios` | enum: `ovmf`, `seabios` | для deployable VM — defaults/profile | необязательно | тип BIOS VM |

Действующий базовый профиль:

```yaml
profiles:
  debian-vm:
    type: vm
    vm:
      source:
        template_vmid: 9000
        clone: full
      guest_agent: true
```

Индивидуальный deployable VM-манифест поэтому остаётся компактным:

```yaml
schema_version: 7
vmid: 301
name: ai-control
profile: debian-vm
state: planned
deployable: true

description: Target central AI infrastructure control plane

placement:
  pool: null

resources:
  cpu:
    cores: 2
  memory_mb: 4096
  disk:
    size_gb: 24
```

В нём нет `type: vm`, `vm.source` и `vm.guest_agent`: эти значения приходят из профиля.

Для LXC используется раздел `lxc`:

| Поле | Тип | Где задаётся | Обязательность | Назначение |
|---|---|---|---|---|
| `lxc.source.ostemplate` | string | LXC-профиль | для deployable LXC | селектор семейства Proxmox LXC template |
| `lxc.unprivileged` | boolean | LXC-профиль | для deployable LXC | непривилегированный контейнер |
| `lxc.container_runtime` | enum, сейчас только `docker` | LXC-профиль | если профиль предназначен для Docker | контейнерная среда внутри LXC |
| `lxc.features.nesting` | boolean | LXC-профиль | для deployable LXC-профиля | разрешение nesting |
| `lxc.features.keyctl` | boolean | LXC-профиль | для deployable LXC-профиля | разрешение keyctl |

Действующий Docker-LXC профиль:

```yaml
profiles:
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

Индивидуальный LXC-манифест содержит только индивидуальные значения:

```yaml
schema_version: 7
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

Если этому же гостю понадобится Bootstrap Docker, он добавляется отдельно:

```yaml
bootstrap:
  capabilities:
    base: true
    docker: true
```

Наличие `container_runtime: docker` в профиле и наличие Bootstrap capability `docker` решают разные задачи: первое описывает тип гостя, второе — явно запрошенную первоначальную подготовку внутри него.

## 4. Правила и ограничения

### 4.1. VMID и адресация

Для управляемых VM/LXC используются VMID/CTID в диапазоне:

```text
100–999
```

Функциональная нумерация применяется к новым объектам и помогает группировать роли.

Принятые диапазоны:

| Диапазон | Назначение |
|---|---|
| `100–199` | сеть и базовая инфраструктура |
| `200–299` | Home Assistant и автоматика |
| `300–399` | прикладные, DevOps- и AI-сервисы |
| `400–499` | мониторинг и обслуживание |
| `500–599` | видео и камеры |
| `600–699` | временные и экспериментальные VM/LXC |
| `900–999` | устаревшие и служебные объекты |

Функциональная нумерация не является причиной перенумеровывать уже работающую систему.

Для центральной management-сети `/16` адрес по умолчанию вычисляется так:

```text
VMID XYZ + A.B.0.0/16
→ A.B.X.YZ
```

Например:

```text
311 + 192.168.0.0/16 → 192.168.3.11
311 + 10.0.0.0/16    → 10.0.3.11
```

Если нужен осознанный индивидуальный адрес, в `guest.yaml` задаётся только bare IPv4:

```yaml
network:
  ipv4:
    address: 192.168.8.50
```

Префикс остаётся центральным и берётся из `defaults.network.subnet`.

Индивидуальный адрес не может совпадать с gateway, network, broadcast или адресом другого гостя. Адрес, не совпадающий с формулой VMID, является осознанным исключением и отмечается проверкой предупреждением.

### 4.2. Состояние и возможность развёртывания

Поле `state` описывает логическое состояние объекта в проекте.

Допустимые значения:

```text
planned
active
bootstrap
legacy
```

Поле `deployable` отдельно отвечает на вопрос, разрешено ли универсальному контуру развёртывания работать с этим гостем.

Для `deployable: true`:

- должен быть указан профиль;
- должны быть определены обязательные ресурсы;
- `type`, `vm` и `lxc` не задаются непосредственно в индивидуальном манифесте, а приходят из профиля.

Для `deployable: false` тип задаётся явно.

`type: undecided` допустим только для неразвёртываемого объекта.

Наличие `bootstrap`, `management.ssh_identity` или `management.project_repo_read` требует `deployable: true`.

Обычный deployable-гость размещается в pool `managed`.

Для специальных self-managed объектов проект допускает отсутствие обычного managed pool; такие исключения проверяются отдельно и не должны распространяться на остальные VM/LXC.

### 4.3. Management и Bootstrap

`management.ssh` является общим административным контрактом и может наследоваться из `defaults.yaml`.

Два поля имеют другой жизненный цикл:

```text
management.ssh_identity
management.project_repo_read
```

Они:

- задаются только в конкретном `guest.yaml`;
- не разрешены в defaults/profile;
- в effective state всегда присутствуют как boolean;
- при отсутствии в исходном манифесте получают `false`.

Если хотя бы одно из этих полей равно `true`, гость должен быть запущен после развёртывания:

```yaml
boot:
  start_after_deploy: true
```

Причина — соответствующие действия выполняются только после появления проверенного административного SSH.

То же требование действует при наличии `bootstrap`.

Bootstrap capabilities имеют явные зависимости:

```text
base

git
└─ base

docker
└─ base

ansible_controller
├─ base
├─ git
└─ docker
```

Зависимости не включаются автоматически.

Если capability требуется, она должна быть явно установлена в `true`.

Канонический порядок:

```text
base → git → docker → ansible_controller
```

Манифест не является интерфейсом для произвольных shell-команд, package lists, URL установщиков или секретов.

### 4.4. Правила VM и LXC

Deployable VM должна использовать профиль типа `vm`.

Проектный VM-профиль определяет источник из шаблона и включает QEMU Guest Agent.

Первая версия универсального развёртывания поддерживает только:

```text
clone: full
```

VM не может клонироваться сама из себя.

Для базовой Debian VM используется проектный шаблон, но точная спецификация его сборки относится к каталогу `templates/`.

Deployable LXC должен использовать профиль типа `lxc`.

В Git хранится селектор семейства LXC template, а не конкретное имя versioned-архива.

Например:

```yaml
lxc:
  source:
    ostemplate: local:vztmpl/debian-13-standard
```

Плавающие обозначения вроде `latest`, `13.x`, `tbd`, wildcard или конкретное имя архивного файла не являются допустимым источником.

Если LXC предназначен для Docker, обязательна комбинация:

```yaml
lxc:
  container_runtime: docker
  unprivileged: true
  features:
    nesting: true
    keyctl: true
```

Docker внутри LXC допускается для доверенных сервисов в этих ограничениях. Более сильная изоляция при необходимости достигается выбором VM, а не расширением прав LXC без отдельного решения.

## 5. Проверка состояния

### 5.1. Схемы и общий resolver

Используются три машинные схемы:

```text
schemas/guest.schema.yaml
→ исходный guest.yaml

schemas/guest-defaults.schema.yaml
→ guests/defaults.yaml и profiles

schemas/guest-effective.schema.yaml
→ итоговое effective state
```

Схемы проверяют форму данных, допустимые значения и обязательные зависимости, которые можно выразить декларативно.

Общий resolver:

```text
scripts/guest_config.py
```

отвечает за правила, требующие вычисления или объединения данных.

Validator и средство развёртывания должны использовать этот общий модуль, а не создавать собственную реализацию merge, адресации или Bootstrap dependencies.

### 5.2. Проверка исходного и итогового состояния

Проверка выполняется в двух стадиях.

Сначала проверяются исходные файлы:

- формат `guests/defaults.yaml`;
- формат конкретного `guest.yaml`;
- допустимые поля и значения;
- отсутствие запрещённых неизвестных полей.

После этого resolver строит effective state.

Затем проверяются:

- полнота итогового состояния;
- соответствие типу VM/LXC;
- обязательные ресурсы;
- сеть;
- management-контракт;
- параметры VM/LXC;
- зависимости Bootstrap.

Некоторые проверки нельзя выразить только YAML-схемой и выполняются программно.

### 5.3. Проверка репозитория

Основная команда проверки:

```bash
python scripts/validate_repo.py
```

Она проверяет не только отдельный YAML-файл, но и взаимосвязи между объектами проекта.

В частности, проверяются:

- соответствие `vmid` и `name` имени каталога;
- уникальность VMID;
- корректность профилей;
- корректность management-сети;
- вычисляемые и явно заданные IP;
- отсутствие конфликтующих адресов;
- допустимость переопределений;
- требования к deployable-гостям;
- VM- и LXC-ограничения;
- Bootstrap dependencies;
- отсутствие секретоподобных полей и private key material.

Ошибка проверки означает, что состояние нельзя считать корректным входом для автоматического развёртывания.

Предупреждение означает состояние, которое формально допустимо, но требует внимания, например осознанное отклонение IP от формулы VMID или избыточное переопределение общего значения.

## 6. Связанные документы

- [`300-overview.md`](300-overview.md) — общая роль гостевых систем и их жизненный цикл.
- [`../100-architecture/120-state-model.md`](../100-architecture/120-state-model.md) — общая модель требуемого и фактического состояния проекта.
- [`../200-pve/200-overview.md`](../200-pve/200-overview.md) — роль PVE-хоста и граница между хостом и гостевым контуром.
- [`../../guests/README.md`](../../guests/README.md) — структура каталога конкретных VM/LXC.
- [`../../schemas/guest.schema.yaml`](../../schemas/guest.schema.yaml) — машинная схема исходного `guest.yaml`.
- [`../../schemas/guest-defaults.schema.yaml`](../../schemas/guest-defaults.schema.yaml) — машинная схема общих настроек и профилей.
- [`../../schemas/guest-effective.schema.yaml`](../../schemas/guest-effective.schema.yaml) — машинная схема итогового effective state.
- [`../../scripts/guest_config.py`](../../scripts/guest_config.py) — общий resolver требуемого состояния.
- [`../../scripts/validate_repo.py`](../../scripts/validate_repo.py) — проверка исходного и итогового состояния репозитория.
- [`../../templates/README.md`](../../templates/README.md) — шаблоны и источники для создания VM/LXC.
