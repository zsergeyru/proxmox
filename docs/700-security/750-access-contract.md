# Единый контракт доступа

**Тип:** спецификация  
**Статус:** действующий  
**Назначение:** определить `access.yaml` как единый машинный источник ответа на вопрос «кто → к какому ресурсу → с каким правом», а также показать, какие технические механизмы строятся из этих правил.

## Содержание

- [1. Роль access.yaml](#1-роль-accessyaml)
- [2. Структура правила](#2-структура-правила)
- [3. Текущие субъекты](#3-текущие-субъекты)
- [4. Как правила превращаются в механизмы](#4-как-правила-превращаются-в-механизмы)
- [5. Селекторы целей](#5-селекторы-целей)
- [6. Связь с другими машинными файлами](#6-связь-с-другими-машинными-файлами)
- [7. Что запрещено хранить](#7-что-запрещено-хранить)
- [8. Изменение и отзыв прав](#8-изменение-и-отзыв-прав)
- [9. Проверка контракта](#9-проверка-контракта)
- [10. Связанные документы](#10-связанные-документы)

## 1. Роль access.yaml

Файл:

```text
infrastructure/security/access.yaml
```

задаёт **требования к доступу**, а не сами secrets.

Он отвечает на вопросы:

```text
кто?
к какому сервису?
к какому ресурсу?
к каким целям?
что разрешено делать?
```

Пример логики:

```text
guest:410
 → semaphore
 → infrastructure-task
 → deploy-managed-guest
 → execute
```

Из этого правила следует, что 410 может вызвать разрешённую инфраструктурную операцию через Semaphore. Из него **не** следует прямое право 410 на PVE API.

## 2. Структура правила

Каждое правило содержит:

| Поле | Назначение |
|---|---|
| `id` | уникальное стабильное имя правила |
| `subject` | субъект, которому требуется право |
| `service` | логическая подсистема |
| `resource` | тип ресурса внутри подсистемы |
| `targets` | допустимые цели |
| `access` | разрешённые действия |

Пример:

```yaml
- id: ai-control-managed-guests
  subject: guest:410
  service: ssh
  resource: guest
  targets:
    - pool:managed
  access:
    - connect-root
```

Это правило не содержит SSH key, OTP, AppRole SecretID или путь к файлу. Оно только задаёт право.

## 3. Текущие субъекты

### 3.1. guest:910

`infra-manager` имеет права, необходимые доверенному исполнителю инфраструктуры:

```text
PVE API → управление гостями
PVE API → чтение нужных storage
SSH → root к host:pve
GitHub → чтение proxmox
SSH identity → собственная машинная идентичность
SSH guest → connect-root к project:linux-guests
```

Наличие SSH identity/guest правил не заменяет административный Ansible client CA. Это отдельная возможность межмашинного SSH, производная от того же контракта.

### 3.2. guest:410

`ai-control` имеет:

```text
GitHub → read proxmox
Semaphore → execute разрешённых infrastructure-task
SSH identity → собственная машинная идентичность
SSH guest → connect-root к pool:managed
```

При этом у 410 нет прямого:

```text
PVE API
root SSH к PVE
OpenBao administrative access
```

### 3.3. guest:311

`dev-services` сейчас имеет:

```text
GitHub → read proxmox
SSH identity → собственная машинная идентичность
```

Наличие `identity/issue` без `ssh/guest/connect-root` не даёт ему произвольного межмашинного SSH.

### 3.4. host:pve

PVE имеет специальные OpenBao-права, необходимые доверенной внешней границе:

```text
OpenBao instance   → unseal
OpenBao credential → materialize рабочие PVE/Git/Semaphore данные
OpenBao credential → update Semaphore runtime
OpenBao ssh-ca     → configure/sign managed-user и managed-host
```

Эти права принадлежат именно `host:pve`, а не любому процессу внутри 910.

## 4. Как правила превращаются в механизмы

`access.yaml` задаёт смысл. Конкретные службы реализуют его.

### 4.1. PVE API

```text
access.yaml: pve-api/*
        ↓
техническая PVE API identity
        ↓
PVE API
```

Текущий доверенный владелец такого доступа — `guest:910`.

### 4.2. Root SSH к PVE

```text
access.yaml: ssh/host/connect-root → host:pve
        ↓
отдельная root SSH identity
        ↓
PVE host-only operation
```

Это право не выводится из PVE API и не выдаётся обычным гостям.

### 4.3. Git read

```text
access.yaml: github/repository/read
        ↓
управляемый read-only credential
        ↓
проектный Git repository
```

Право записи не добавляется автоматически.

### 4.4. Semaphore infrastructure-task

```text
access.yaml: semaphore/infrastructure-task/execute
        ↓
допустимый набор шаблонов/операций
        ↓
Semaphore
        ↓
910 выполняет инфраструктурное действие
```

Этот механизм нужен, чтобы менее доверенный контур мог запросить конкретное действие без получения прямого PVE credential.

### 4.5. SSH identity + guest

Для целевой OTP-схемы используются **два правила одного субъекта**:

```text
ssh/identity/issue
        ↓
отдельный auth/machine AppRole

ssh/guest/connect-root
        ↓
точный список целей собственной OTP-роли
```

Вместе:

```text
access.yaml
   ↓
guest-<VMID> AppRole
   ↓
guest-<VMID> OTP role
   ↓
OTP только к разрешённым целям
```

`connect-root` без `identity/issue` запрещён.

Машинные SSH-сертификаты, `AuthorizedPrincipalsFile` со списками источников и периодическое перевыпускание клиентских машинных сертификатов не являются целевой производной `access.yaml`.

### 4.6. PVE-only OpenBao

Правила `host:pve → openbao/*` определяют функции, которые остаются за пределами обычного 910:

- unseal;
- материализация рабочих secrets;
- обновление секрета Semaphore;
- настройка и использование SSH CA.

Они не должны автоматически превращаться в общий OpenBao administrative credential для гостей.

## 5. Селекторы целей

Цель может быть конкретной или групповой.

Поддерживаемые логические селекторы:

```text
guest:<VMID>         → один конкретный гость
host:pve             → физический PVE
pool:managed         → поддерживаемые гости области managed
project:linux-guests → поддерживаемые Linux-гости проекта
self                 → сам субъект
```

Разворачивание группового селектора в фактические цели должно быть воспроизводимым и проверяемым.

Изменение состава `pool:managed` или `project:linux-guests` может изменить производный набор целей, поэтому синхронизация должна пересчитать соответствующие правила.

## 6. Связь с другими машинными файлами

Области ответственности:

| Файл | Что решает |
|---|---|
| `guest.yaml` | какой объект VM/LXC существует |
| `provision.yaml` | что установлено и работает внутри гостя |
| `status.yaml` | что и как проверять |
| `access.yaml` | кто с кем и каким способом имеет право взаимодействовать |

`guest.yaml` не является каталогом прав.

`provision.yaml` может описывать техническую реализацию механизма доступа, но не выдаёт право субъекту.

`status.yaml` проверяет фактическое состояние, но не создаёт новые полномочия.

## 7. Что запрещено хранить

`access.yaml` не содержит:

- пароли;
- API secrets;
- private keys;
- OpenBao tokens;
- RoleID/SecretID;
- OTP;
- пути к секретным файлам;
- KV paths;
- конфигурацию контейнера;
- порядок установки.

Вместо физического secret используется логическое имя ресурса.

Например:

```text
pve-api-infra-manager
github-proxmox-read
semaphore-runtime
```

Физическое хранение и доставка этих данных относятся к OpenBao и локальной реализации соответствующего гостя.

## 8. Изменение и отзыв прав

`access.yaml` должен быть не только источником выдачи, но и источником отзыва.

Примеры:

- удалено `github/repository/read` → субъект перестаёт получать управляемый Git credential;
- удалён `semaphore/infrastructure-task` → действие больше нельзя запрашивать через Semaphore;
- удалена одна цель `ssh/guest/connect-root` → адрес исчезает из OTP-роли источника;
- удалён `ssh/identity/issue` → машинный AppRole и возможность получать новые OTP отключаются;
- удалено OpenBao-право PVE → соответствующий PVE-only механизм больше не должен получать эту возможность.

Уже открытая SSH-сессия или уже завершённое действие не отменяются задним числом; отзыв прекращает новые выдачи и новые операции.

## 9. Проверка контракта

Репозиторий должен проверять:

- соответствие `access.schema.yaml`;
- уникальность `id`;
- существование субъектов и конкретных целей;
- допустимость сочетания `service/resource/access`;
- отсутствие secrets и путей их хранения;
- отсутствие прямого PVE API/root SSH у 410;
- наличие `identity/issue` у каждого OTP-источника с `connect-root`;
- отсутствие второго списка прав в `guest.yaml`;
- корректность текущих специальных PVE-only правил OpenBao.

После полной реализации OTP также необходимо автоматически подтверждать:

- один машинный AppRole на каждый `identity/issue`;
- отсутствие лишних AppRole;
- точное совпадение OTP targets с `connect-root` после раскрытия селекторов;
- отсутствие роли с произвольной целью;
- невозможность использовать чужую OTP-роль;
- удаление производных механизмов после удаления права.

Текущая проверка репозитория:

```bash
python scripts/validate_repo.py
```

Специализированный контрактный тест:

```bash
python scripts/tests/test-access-contract.py
```

## 10. Связанные документы

- [`700-overview.md`](700-overview.md) — общая модель взаимодействий.
- [`710-pve-access.md`](710-pve-access.md) — PVE-права.
- [`720-ssh-access.md`](720-ssh-access.md) — client CA, host CA и OTP.
- [`730-git-access.md`](730-git-access.md) — Git read/write.
- [`740-openbao.md`](740-openbao.md) — AppRole, KV и OTP.
- [`780-implementation-status.md`](780-implementation-status.md) — какие производные механизмы уже реализованы.
- [`../300-guests/320-guest-manifest.md`](../300-guests/320-guest-manifest.md) — роли машинных YAML.