# Zero-day bootstrap `301-ai-control` — спецификация двух скриптов

## Статус

**Архитектурное решение принято, скрипты ещё не реализованы.**

Этот документ является спецификацией для двух будущих публичных executable-скриптов в репозитории `zsergeyru/proxmox-bootstrap`:

```text
create-ai-control-vm.sh
install-ai-control.sh
```

Их задача — восстановить центральный AI control plane на новом Proxmox-хосте, где изначально нет Git checkout приватного репозитория, AI-агентов, SSH-ключей AI и GitHub credentials.

Канонический executable-код хранится только в публичном `zsergeyru/proxmox-bootstrap`. В приватном `zsergeyru/proxmox` хранится эта спецификация, desired state и дальнейшая конфигурация инфраструктуры.

## Целевой сценарий

Исходное состояние:

```text
новый компьютер
└── Proxmox VE
    └── 9000 tpl-debian13
```

После zero-day bootstrap должно получиться:

```text
301 ai-control
├── /opt/ai-control/agents/hermes/
│   └── Hermes + встроенный WebUI
├── /opt/ai-control/mcp/proximo/
│   └── Proximo MCP
├── /opt/ai-control/repos/
│   └── proxmox/                 # появляется после регистрации Deploy Key
├── infrastructure SSH identity
└── GitHub Deploy Key identity
```

Полная цепочка:

```text
PVE + template 9000
→ create-ai-control-vm.sh
→ готовая базовая VM 301
→ install-ai-control.sh внутри 301
→ Hermes + WebUI + Proximo + SSH identities
→ показать GitHub Deploy Key public key
→ оператор регистрирует ключ в zsergeyru/proxmox
→ повторная проверка install-ai-control.sh
→ clone приватного репозитория в /opt/ai-control/repos/proxmox
→ Hermes получает source of truth
→ дальше Hermes/Proximo и затем 311/Ansible разворачивают остальные гости
```

Приватный Git намеренно появляется **после** развёртывания AI. GitHub PAT для zero-day bootstrap не требуется.

---

# 1. `create-ai-control-vm.sh`

## Назначение

Первый скрипт выполняется **на Proxmox VE host от `root`**.

Он отвечает за уровень виртуализации и за те bootstrap-credentials, которые может безопасно создать только администратор PVE. Он **не устанавливает Hermes и не является application installer**.

Главный результат первого этапа:

```text
9000 tpl-debian13
        ↓ Full Clone
301 ai-control
        ↓
готова к запуску install-ai-control.sh
```

## Предварительные условия

Скрипт должен проверить до внесения изменений:

- запуск от `root` на PVE;
- наличие команд `qm`, `pvesh`, `pveum`, `pvesm` и необходимых shell-утилит;
- существование VMID `9000`;
- `9000` действительно является template;
- имя template соответствует `tpl-debian13` либо явно разрешённому override;
- QEMU Guest Agent включён в template;
- доступность выбранного storage и bridge;
- VMID `301` свободен либо уже принадлежит ожидаемой `ai-control` VM;
- отсутствует ситуация, в которой скрипт мог бы перезаписать неизвестный объект.

При несовпадении VMID/имени или сомнительном состоянии скрипт прекращает работу без удаления существующих объектов.

## Параметры по умолчанию

Целевые defaults:

```text
VMID:       301
Name:       ai-control
Template:   9000 tpl-debian13
Node:       pve
Clone:      Full Clone
CPU:        2 cores
RAM:        4096 MiB
Disk:       24 GiB
Bridge:     vmbr0
IPv4:       192.168.3.1/16
Gateway:    192.168.1.1
On boot:    yes
Protection: no
CI user:    ops
```

Параметры должны допускать явное переопределение environment variables/CLI options, но default values обязаны соответствовать паспорту `301` и документации проекта.

Перед реализацией скрипта значения CPU/RAM/disk должны быть также внесены в `guests/301-ai-control/guest.yaml`, чтобы не было второго источника истины.

## Создание VM

Порядок первого этапа:

```text
validate PVE/template/storage/network
→ Full Clone 9000 → 301
→ явно protection=0
→ CPU/RAM
→ увеличить disk при необходимости
→ network/Cloud-Init
→ ciuser=ops
→ onboot=1
→ qm cloudinit update
→ first start
→ дождаться QEMU Guest Agent
→ проверить cloud-init completion и базовый health
```

Linked Clone для production `301` не используется.

Скрипт не должен снимать `protection=1` с template `9000` и не должен изменять сам template.

## Почему на первом этапе нужен Proximo credential

Исключение из правила «первый скрипт только создаёт VM» — создание management identity для Proximo.

Причина: API user/token и ACL являются объектами самого PVE. Создавать их из AI-гостя через административный host credential неправильно: это дало бы `301` возможность расширять собственные полномочия.

Поэтому `create-ai-control-vm.sh`, выполняемый человеком как `root` на PVE, один раз создаёт утверждённую management identity с минимальными правами.

Целевое имя:

```text
user:  proximo@pve
token: proximo@pve!ai-control
privilege separation: enabled
```

Также подготавливается resource pool:

```text
managed
```

`301` не должен автоматически помещаться в свою обычную self-managed write-зону.

Template `9000` должен быть доступен management identity для read/clone, но не для изменения или удаления.

Точные ACL/privileges берутся из `guests/320-ai-control/decisions/002-proxmox-permissions.md` и проверяются `proximo doctor`. Скрипт не должен выдавать `PVEAdmin` на `/` и не должен использовать `root@pam` token.

## Передача Proximo token secret

Token secret показывается Proxmox только при создании. Он не должен записываться в Git или оставаться постоянным plaintext-файлом на PVE.

После запуска `301` первый скрипт через QEMU Guest Agent создаёт внутри VM закрытый bootstrap-каталог и передаёт туда credential для последующей установки Proximo.

Целевое место:

```text
/etc/ai-control/secrets/proximo/pve-token
```

Требования:

```text
owner: ops
mode:  0600
```

Формат должен соответствовать Proximo:

```text
proximo@pve!ai-control=SECRET
```

После успешной передачи временная копия секрета на PVE уничтожается. Скрипт не выводит secret в итоговый отчёт.

Для TLS первый этап также должен передать в `301` доверенный сертификат/CA PVE, если это необходимо для проверки self-signed certificate. Целевой режим Proximo — TLS verification enabled; `PROXIMO_VERIFY_TLS=false` не является штатным default.

## Повторный запуск первого скрипта

Скрипт должен быть безопасно повторно запускаемым.

Если `301` уже существует и соответствует ожидаемому объекту:

- не удалять VM;
- не делать новый clone;
- не уменьшать disk;
- не сбрасывать Cloud-Init без причины;
- не менять уже существующие ключи/данные внутри гостя;
- проверить состояние и вывести, какие шаги уже завершены.

Если Proximo token уже существует и рабочий secret присутствует внутри `301`, ничего не менять.

Если token существует, но secret внутри `301` утрачен, скрипт **не должен молча ротировать token**. Требуется явный флаг/режим восстановления, например `--rotate-proximo-token`.

Если VMID `301` занят другим объектом, выполнение прекращается.

## Что первый скрипт НЕ делает

`create-ai-control-vm.sh` не должен:

- устанавливать Hermes;
- устанавливать Proximo package/runtime;
- настраивать model provider/API keys;
- создавать GitHub Deploy Key;
- клонировать приватный Git;
- устанавливать Ansible/Semaphore;
- разворачивать другие VM/LXC;
- выдавать AI права на IAM/ACL/network/storage administration PVE.

По завершении он выводит статус VM и точную следующую команду для второго этапа.

---

# 2. `install-ai-control.sh`

## Назначение

Второй скрипт выполняется **внутри `301-ai-control`** от `root` либо через `sudo` пользователя `ops`.

Он отвечает только за программную часть AI control plane:

```text
base packages
→ каталоги /opt/ai-control
→ Hermes в agents/hermes
→ Hermes WebUI
→ Proximo в mcp/proximo
→ SSH identities
→ GitHub Deploy Key onboarding
→ health checks
```

Он не создаёт PVE users/tokens/ACL и не должен иметь механизма расширения собственных прав на гипервизоре.

## Каталоги

Обязательная структура:

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   ├── proximo/
│   └── <future-mcp>/
├── repos/
│   └── proxmox/
└── state/
```

Ключевое правило:

> Конкретный AI-агент всегда устанавливается в `/opt/ai-control/agents/<agent>/`.

Поэтому Hermes нельзя устанавливать прямо в `/opt/ai-control`, `/root`, `/home/ops` или каталог MCP.

Общие MCP не размещаются внутри `agents/hermes`; для них используется `/opt/ai-control/mcp/`.

Рабочий checkout инфраструктурного репозитория не смешивается с кодом агента и находится в `/opt/ai-control/repos/proxmox`.

Владельцем рабочих каталогов агента и checkout является `ops`, если конкретный runtime не требует более строгой отдельной service identity.

## Базовые пакеты

Скрипт устанавливает только необходимые runtime/admin dependencies, которых нет в base template, например:

- Python runtime/venv tooling для Proximo;
- Git;
- OpenSSH client;
- curl/ca-certificates и необходимые TLS-инструменты;
- зависимости Hermes согласно официальному способу установки;
- systemd unit dependencies.

Docker устанавливается только если выбранный и проверенный способ установки Hermes действительно его требует. Сам факт наличия AI control не является основанием добавлять Docker без необходимости.

## Hermes

Hermes — основной агент `301`.

Целевое размещение:

```text
/opt/ai-control/agents/hermes/
```

Требования к installer:

- использовать официальный/проверенный источник Hermes;
- фиксировать проверенную версию, а не бесконтрольно устанавливать случайный `latest` при каждом запуске;
- рабочий каталог сервиса должен быть `/opt/ai-control/agents/hermes`;
- постоянные данные и configuration должны переживать повторный запуск installer;
- model/provider credentials не должны попадать в Git;
- обновление Hermes должно быть отдельным осознанным режимом, а не побочным эффектом повторного bootstrap.

### WebUI

Используется штатный WebUI/Dashboard Hermes.

Целевой порт проекта:

```text
tcp/9119
```

WebUI не должен штатно публиковаться без авторизации. Точный поддерживаемый Hermes механизм auth и конкретные config/env names необходимо проверить при реализации скрипта; документация не должна выдумывать несовместимую схему авторизации.

После установки скрипт выводит URL Dashboard. Model/provider credentials оператор задаёт после первого входа либо поддерживаемым secret-механизмом Hermes.

Это допустимый ручной шаг: новый сервер не может самостоятельно получить credential провайдера модели.

## Proximo

Целевой Proxmox MCP:

```text
Proximo / proximo-proxmox
```

Размещение:

```text
/opt/ai-control/mcp/proximo/
```

Рекомендуемая реализация — отдельное Python virtual environment внутри этого каталога, чтобы runtime Proximo был изолирован и версия могла быть зафиксирована.

Configuration/secret не смешиваются с executable files:

```text
/etc/ai-control/proximo/
/etc/ai-control/secrets/proximo/pve-token
```

Минимальная конфигурация должна содержать:

```text
PROXIMO_API_BASE_URL=https://<PVE>:8006/api2/json
PROXIMO_NODE=pve
PROXIMO_TOKEN_PATH=/etc/ai-control/secrets/proximo/pve-token
PROXIMO_VERIFY_TLS=true
PROXIMO_TOOLSETS=pve.guests
```

При self-signed PVE certificate используется pin/CA bundle, а не отключение TLS verification как постоянное решение.

Proximo запускается Hermes как локальный stdio MCP. Для одного локального Hermes отдельный сетевой MCP daemon не является обязательным элементом zero-day архитектуры.

После установки обязательно выполняется:

```text
proximo doctor
```

Результат сохраняется для диагностики, например:

```text
/opt/ai-control/state/proximo-doctor.json
```

Установка считается корректной только если ожидаемые guest-level capabilities доступны, а запрещённые host/IAM capabilities не появились из-за слишком широкого token/ACL.

## Infrastructure SSH identity

Второй скрипт создаёт отдельную пару для прямого SSH из AI control в будущие managed Debian VM:

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
```

Правила:

- генерировать только если ключ ещё отсутствует;
- private key никогда не выводить;
- private key никогда не копировать в Git/template/PVE;
- permissions `0600` для private key;
- public key разрешено читать Hermes;
- public key передаётся новым VM через Cloud-Init до первого запуска.

Ожидаемый дальнейший workflow:

```text
Hermes/Proximo
→ Full Clone 9000
→ ciuser=ops
→ sshkeys=<ai_control_ed25519.pub>
→ first start
→ SSH ops@guest с ai_control_ed25519
```

Этот ключ предназначен для инфраструктурных VM и не используется как GitHub credential.

## GitHub Deploy Key identity

Отдельно создаётся:

```text
/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
```

Назначение — только приватный репозиторий:

```text
zsergeyru/proxmox
```

Private key остаётся в `301`. Скрипт выводит оператору только `.pub` вместе с инструкцией:

```text
GitHub
→ zsergeyru/proxmox
→ Settings
→ Deploy keys
→ Add deploy key
```

Для сценария, в котором Hermes должен делать commit/push, Deploy Key регистрируется с `Allow write access`.

GitHub PAT zero-day bootstrap не использует.

## Поведение до регистрации Deploy Key

Отсутствие доступа к приватному Git после первой установки **не является ошибкой bootstrap**.

Скрипт должен завершить локальную установку и явно показать состояние, например:

```text
Hermes ............... OK
Hermes WebUI ......... OK
Proximo .............. OK
Infrastructure SSH ... OK
GitHub SSH key ....... GENERATED
GitHub repository .... WAITING FOR DEPLOY KEY
```

То есть AI уже развёрнут до появления Git.

## Поведение после регистрации Deploy Key

При повторном запуске installer либо отдельной проверке выполняются:

```text
SSH auth to GitHub
→ git ls-remote private repo
→ clone, если checkout отсутствует
→ verify working tree/remote
```

Целевой checkout:

```text
/opt/ai-control/repos/proxmox
```

Если каталог уже существует и является ожидаемым checkout, он не удаляется и не клонируется заново.

После успешного clone приватный репозиторий становится source of truth для дальнейших действий Hermes.

## systemd и автозапуск

Hermes/WebUI должны переживать reboot `301`.

Installer создаёт/активирует только необходимые systemd units. Сервисы запускаются от минимально необходимого пользователя; если Hermes работает от `ops`, unit должен явно задавать `User=ops` и рабочий каталог `/opt/ai-control/agents/hermes`.

Proximo при stdio-модели может запускаться непосредственно Hermes как MCP subprocess и не обязан иметь отдельный постоянно работающий systemd daemon.

## Повторный запуск второго скрипта

`install-ai-control.sh` должен быть идемпотентным в практическом смысле:

- не перегенерировать существующие SSH keys;
- не сбрасывать model/provider credentials;
- не удалять Git checkout;
- не менять GitHub Deploy Key;
- не переустанавливать/обновлять Hermes на другую версию без явного upgrade-режима;
- не ротировать Proximo token;
- чинить отсутствующие каталоги/permissions/systemd units, если это безопасно;
- повторно выполнять health checks.

Повторный запуск должен использоваться как безопасная команда «доделать/проверить bootstrap».

## Что второй скрипт НЕ делает

`install-ai-control.sh` не должен:

- создавать/удалять VM 301;
- менять PVE host network/storage;
- создавать себе дополнительные PVE roles/ACL/tokens;
- использовать `root@pam` API token;
- добавлять секреты в Git;
- автоматически разворачивать всю остальную инфраструктуру до того, как Hermes получил приватный source of truth;
- устанавливать Ansible/Semaphore внутрь 301.

---

# 3. Граница между двумя скриптами

Разделение ответственности принципиальное:

```text
create-ai-control-vm.sh
└── PVE / VM provisioning / Proximo bootstrap credential

install-ai-control.sh
└── software inside 301 / agents / MCP / SSH identities / Git onboarding
```

Первый скрипт можно завершить и проверить до установки AI. Второй можно многократно запускать внутри уже существующей VM без пересоздания VM.

`create-ai-control-vm.sh` не должен содержать большой embedded application installer. `install-ai-control.sh` не должен требовать административного доступа к PVE host.

---

# 4. Секреты и backup

Секретами являются как минимум:

```text
Proximo API token secret
ai_control_ed25519
github_proxmox_ed25519
Hermes/model provider credentials
WebUI auth/signing secrets, если они используются
```

Они:

- не хранятся в `zsergeyru/proxmox-bootstrap`;
- не хранятся в `zsergeyru/proxmox`;
- не находятся в base template `9000`;
- создаются/передаются только во время bootstrap;
- входят в чувствительные данные backup `301`.

Полный backup `301` фактически является backup control-plane credentials и должен защищаться соответствующим образом.

---

# 5. Критерии готовности

## Этап A — VM создана

После `create-ai-control-vm.sh`:

- существует именно VM `301 ai-control`;
- это Full Clone `9000`;
- `protection=0` у `301`, template `9000` остаётся защищённым;
- CPU/RAM/disk/network соответствуют desired state;
- `onboot=1`;
- QEMU Guest Agent отвечает;
- cloud-init завершён;
- Proximo bootstrap credential безопасно присутствует внутри `301`;
- host-side временные secrets удалены.

## Этап B — AI установлен

После первого `install-ai-control.sh`:

- Hermes установлен в `/opt/ai-control/agents/hermes`;
- WebUI доступен и не оставлен намеренно без auth;
- Proximo установлен в `/opt/ai-control/mcp/proximo`;
- `proximo doctor` выполняется;
- созданы две разные SSH identity;
- GitHub public Deploy Key показан оператору;
- отсутствие Git-доступа корректно отображается как ожидание регистрации ключа.

## Этап C — Git подключён

После регистрации Deploy Key и повторной проверки:

- GitHub SSH authentication работает;
- `git ls-remote` приватного `zsergeyru/proxmox` проходит;
- repo клонирован в `/opt/ai-control/repos/proxmox`;
- Hermes может читать source of truth;
- при разрешённом write Deploy Key Hermes может commit/push согласно принятому workflow.

## Этап D — control plane доказал работоспособность

До замены `320-ai-control` необходимо выполнить live-test:

```text
Hermes + Proximo
→ создать тестовый Full Clone из 9000
→ поместить его в managed pool
→ protection=0
→ передать ai_control_ed25519.pub через Cloud-Init
→ first start
→ QEMU Agent health
→ SSH ops из 301
```

После этого можно переходить к развёртыванию `311-dev-services` и штатной Ansible-модели.

---

# 6. Порядок реализации

Рекомендуемый порядок работы над кодом:

1. привести `guests/301-ai-control/guest.yaml` к полному desired state CPU/RAM/disk/network;
2. реализовать и протестировать `create-ai-control-vm.sh` отдельно;
3. подтвердить чистое создание `301` из `9000`;
4. реализовать `install-ai-control.sh` без Git-зависимости;
5. подтвердить каталоги `/opt/ai-control/agents/hermes` и `/opt/ai-control/mcp/proximo`;
6. проверить `proximo doctor`;
7. проверить генерацию/сохранность обеих SSH identity;
8. зарегистрировать Deploy Key вручную;
9. проверить clone в `/opt/ai-control/repos/proxmox`;
10. провести end-to-end test создания тестовой VM и SSH из `301`;
11. только после этого считать zero-day chain live-validated.

## Главный принцип

> Сначала создаётся независимый от приватного Git AI control plane; затем человек один раз регистрирует его GitHub public key; после этого AI получает source of truth и способен продолжить развёртывание инфраструктуры самостоятельно.
