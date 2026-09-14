# Инициализация нового Proxmox VE host

## Цель

Нужен один публичный bootstrap-скрипт:

```text
zsergeyru/proxmox-bootstrap/init-pve.sh
```

Он подготавливает новый Proxmox VE host до состояния, в котором PVE способен самостоятельно читать desired state проекта из приватного `zsergeyru/proxmox`, создавать базовый template и разворачивать VM/LXC по `guest.yaml` без зависимости от `301-ai-control`, Hermes или другого AI-агента.

После появления read-only доступа к приватному Git все остальные executable-скрипты должны храниться уже в приватном `zsergeyru/proxmox`. Публичный bootstrap-репозиторий нужен только для zero-day входа на совершенно новый PVE.

## Исходное состояние

```text
новый компьютер
└── установлен Proxmox VE
```

Не предполагается наличие:

- Git checkout приватного репозитория;
- AI/301;
- Hermes/другого агента;
- Proximo;
- GitHub credentials;
- template `9000`;
- проектных PVE users/roles/tokens/pools.

## Целевое состояние

После инициализации:

```text
PVE
├── корректные бесплатные PVE repositories
├── минимальный bootstrap toolset
├── технический Linux-user pvedeploy
├── отдельный read-only GitHub Deploy Key
├── shallow read-only checkout zsergeyru/proxmox
├── проектные PVE users/roles/tokens
├── resource pool managed (если окончательно сохраняем эту модель)
├── template 9000 tpl-debian13
├── private deploy tooling из zsergeyru/proxmox
├── bootstrap state/version
└── health report
```

После этого:

```bash
deploy-guest 301 --apply
deploy-guest 311 --apply
```

должны работать независимо от AI control plane.

---

# 1. Preflight host — принято

Перед изменениями `init-pve.sh` должен проверить:

- запуск от `root`;
- наличие и работоспособность `pveversion`, `qm`, `pct`, `pvesh`, `pveum`, `pvesm`;
- node name;
- доступность аппаратной виртуализации/KVM;
- существование и состояние storage `local` и `local-lvm`;
- наличие `vmbr0`;
- свободное место на root и основных storage;
- DNS resolution;
- выход в интернет к `download.proxmox.com`, GitHub и публичному bootstrap repo;
- текущее состояние времени/синхронизации;
- отсутствие очевидных конфликтов VMID `9000` и других bootstrap-объектов.

Критические ошибки должны останавливать скрипт **до** внесения существенных изменений.

---

# 2. Бесплатный Proxmox repository — принято

Для PVE без subscription `init-pve.sh` должен привести APT configuration к штатной бесплатной схеме Proxmox VE.

Для Debian 13 / Trixie целевой PVE repository:

```text
Types: deb
URIs: http://download.proxmox.com/debian/pve
Suites: trixie
Components: pve-no-subscription
Signed-By: /usr/share/keyrings/proxmox-archive-keyring.gpg
```

Также должны оставаться корректные Debian base/security repositories.

Правила:

- не использовать `pve-test` как default;
- отключать недоступный enterprise repo, если subscription отсутствует;
- не добавлять сторонние repositories без отдельного решения;
- перед изменением APT sources сохранить их копию;
- обычный повторный запуск не должен бесконтрольно выполнять большой upgrade.

Обновление пакетов лучше вынести в явный режим, например:

```bash
init-pve.sh --update-system
```

Обычный init может выполнять `apt update`, но серьёзное обновление хоста должно быть осознанным действием.

---

# 3. Минимальный пакетный набор — принято

Принцип: устанавливать только то, что реально требуется bootstrap/deployer/диагностике PVE.

## Обязательный bootstrap runtime

```text
git
openssh-client
python3
python3-yaml
curl
jq
ca-certificates
```

Это минимальный набор для:

- read-only Git checkout;
- SSH Deploy Key;
- разбора `guest.yaml`;
- работы bootstrap/deployer;
- проверки HTTPS/API responses.

## Полезные небольшие host-admin инструменты

Допускаются как базовый набор:

```text
mc
htop
tmux
smartmontools
lm-sensors
```

Они полезны именно для обслуживания гипервизора и уже соответствуют реальным задачам проекта.

## Условные/по наличию

Не нужно бездумно устанавливать всё подряд. Такие утилиты как `rsync`, `tar`, `unzip`, `lsof`, `ethtool`, `pciutils`, `usbutils` следует устанавливать только если они реально отсутствуют и используются bootstrap/deployer/diagnostics.

Не устанавливать на PVE:

- Docker;
- AI runtimes;
- Hermes;
- application services;
- произвольные development toolchains.

---

# 4. Время и DNS — принято

`init-pve.sh` должен проверить:

- timezone;
- активную синхронизацию времени;
- фактическую дату/время;
- DNS resolution;
- доступность GitHub/Proxmox repositories по DNS.

Скрипт не должен самовольно менять timezone на жёстко заданное значение без параметра/policy.

Неверное время критично для TLS, API tokens, журналов, GitHub и диагностики.

---

# 5. Storage preparation — принято

Проверить существование:

```text
local
local-lvm
```

Для `local` проверить необходимые content types. Для проекта нужны как минимум те типы, которые реально используются bootstrap/template/deploy, включая `snippets`, а также при необходимости `iso`, `vztmpl`, `backup`.

При изменении `content` нельзя затереть уже существующие допустимые типы.

`init-pve.sh` должен:

1. прочитать текущий storage config;
2. определить недостающие capabilities;
3. показать PLAN;
4. добавить только недостающее;
5. повторно проверить результат.

Проверка storage входит в preflight до создания template `9000`.

---

# 6. `managed` pool и Proxmox identities — требует окончательного решения

## Нужен ли `managed` pool

`managed` не является технически обязательным для самого `deploy-guest`, но полезен как **граница полномочий и логическая группа** управляемых объектов.

Плюсы:

- можно ограничить AI/автоматизацию только выделенным набором VM/LXC;
- удобно видеть управляемые объекты в Proxmox UI;
- проще отличать обычные managed guests от защищённых объектов;
- можно не включать туда `100`, `301`, `320`, `9000` и другие исключения.

Минусы:

- ACL становятся немного сложнее;
- при создании guest нужно сразу правильно включать его в pool;
- PVE-side deployer всё равно должен иметь право создать новый объект до/при помещении в pool.

Предварительная рекомендация: **`managed` сохранить**, но считать его прежде всего security/organization boundary для runtime automation, а не необходимым условием существования deployer.

## Почему обсуждаются две Proxmox identity

Предлагаемые назначения:

```text
deployer@pve
→ используется PVE-side deploy-guest
→ deterministic deployment по guest.yaml
→ create/configure/start managed VM/LXC
→ работает без AI

proximo@pve
→ используется Proximo из 301-ai-control
→ runtime AI operations
→ lifecycle/snapshots/backups/diagnostics managed guests
```

Зачем разделять:

- разные места хранения token secret;
- разный audit trail;
- можно отозвать AI token, не ломая host-side recovery/deploy;
- можно дать deployer и Proximo разные наборы прав;
- компрометация AI credential не затрагивает host bootstrap credential.

Альтернатива для упрощения:

```text
automation@pve
├── token deployer
└── token proximo
```

с privilege separation и разными token ACL. Это уменьшает число backing users, но user ACL должен покрывать объединённую область прав обоих token.

Окончательно выбрать между:

```text
A. два отдельных PVE users
B. один automation@pve + два privilege-separated token
```

нужно до реализации `init-pve.sh`.

Ни один вариант не должен использовать `root@pam` token или `PVEAdmin` на `/` просто ради удобства.

---

# 7. Linux-user `pvedeploy` — принято

`root` нужен для первоначального `init-pve.sh`, но штатная работа Git/deployer не должна выполняться из `/root` без необходимости.

Создаётся отдельный системный пользователь:

```text
pvedeploy
```

Его задачи:

- хранить read-only GitHub SSH identity;
- владеть read-only checkout private repo;
- запускать manifest parser/deploy client;
- вести собственные runtime/log/state files.

Целевые каталоги:

```text
/etc/proxmox-deployer/
/var/lib/proxmox-deployer/
/var/log/proxmox-deployer/
```

Git private key должен принадлежать `pvedeploy` и иметь `0600`.

Не давать:

```text
NOPASSWD: ALL
```

Если deployer работает через Proxmox API token, обычный full sudo ему вообще не требуется.

---

# 8. Read-only Git checkout — уточнено

## Зачем локальный checkout

PVE нужен локальный источник истины для:

```text
guests/*/guest.yaml
private PVE scripts
templates metadata
project policies
```

При этом PVE **никогда не делает commit/push**.

## GitHub identity

Отдельная PVE-only пара:

```text
/etc/proxmox-deployer/ssh/github_proxmox_ed25519
/etc/proxmox-deployer/ssh/github_proxmox_ed25519.pub
```

Public key регистрируется как Deploy Key для `zsergeyru/proxmox`:

```text
Allow write access = OFF
```

Ключ `301-ai-control` здесь не используется.

## Shallow checkout

Рекомендуемый первый вариант:

```text
full working tree
+ только короткая Git history
```

То есть **shallow clone**, например depth 1.

Это означает:

- все нужные каталоги доступны локально;
- история старых commit почти не скачивается;
- обновление быстрое;
- sparse checkout пока не нужен.

Целевой каталог:

```text
/var/lib/proxmox-deployer/repo
```

владелец:

```text
pvedeploy:pvedeploy
```

## Политика обновления

Checkout считается read-only cache, поэтому локальных изменений там быть не должно.

Перед PLAN/apply:

```text
fetch origin/main
→ определить commit SHA
→ привести checkout к этому origin/main
→ использовать один и тот же SHA для PLAN и APPLY
```

Это важнее, чем просто `git pull`: manifest не должен поменяться между построением плана и применением.

Deployer должен выводить используемый commit, например:

```text
Repository revision: abcdef123456
```

Для воспроизводимости позже полезно поддержать:

```bash
deploy-guest 311 --ref <commit> --apply
```

Sparse checkout пока не использовать: репозиторий небольшой, а deployer может потребовать файлы из разных разделов.

---

# 9. Снимок конфигурации PVE перед init — уточнено

Это **не backup VM и не полноценный disaster-recovery backup**.

Назначение — быстро вернуть/сравнить настройки, которые `init-pve.sh` собирается менять.

Перед первым изменяющим этапом создать каталог:

```text
/var/backups/proxmox-bootstrap/YYYYMMDD-HHMMSS/
```

и сохранить туда только важную host configuration/state information, например:

```text
/etc/network/interfaces
/etc/hosts
/etc/hostname
/etc/resolv.conf
APT source files
/etc/pve/storage.cfg
/etc/pve/user.cfg
/etc/pve/datacenter.cfg   # если существует/релевантен
```

Дополнительно полезно сохранить диагностические snapshots в текст/JSON:

```text
pveversion -v
storage list/config
users
roles
ACL
pools
network state
```

Почему не копировать бездумно весь `/etc/pve`:

- `/etc/pve` — специальная Proxmox cluster filesystem (`pmxcfs`), а не обычный каталог;
- init меняет только ограниченную часть конфигурации;
- точечный snapshot понятнее и безопаснее для rollback/audit.

Этот локальный snapshot защищает от ошибки bootstrap, но **не заменяет внешний backup PVE configuration** на случай физической потери системного SSD.

Повторный запуск init не обязан создавать новый snapshot, если с момента предыдущего init изменения не планируются; можно создавать его перед каждой реально изменяющей миграцией bootstrap version.

---

# 10. Bootstrap state — принято, назначение уточнено

Целевой файл:

```text
/var/lib/proxmox-bootstrap/state.json
```

Он нужен для **resume/idempotency**, а не как source of truth.

Пример задач state:

- помнить, что GitHub key уже создан, и не регенерировать его;
- понимать, что script остановился в `WAITING_FOR_GITHUB_KEY`;
- хранить установленную bootstrap schema/version;
- помнить последний успешно использованный repo commit;
- хранить ожидаемую template version;
- отображать понятный progress/status человеку.

Пример логического состояния:

```json
{
  "schema": 1,
  "bootstrap_version": 1,
  "github_key_created": true,
  "github_access": false,
  "repo_revision": null,
  "roles_initialized": false,
  "template_9000_ready": false
}
```

Но правило принципиальное:

> `state.json` не имеет права утверждать, что объект существует только потому, что так записано в файле.

При каждом повторном запуске фактическое состояние проверяется заново:

```text
state says key exists
→ проверить файл и fingerprint

state says repo cloned
→ проверить .git/remote/revision

state says role exists
→ проверить через PVE API

state says template ready
→ проверить VMID 9000/template/version
```

То есть state ускоряет и объясняет процесс, но реальный PVE/Git остаётся источником факта.

---

# 11. Bootstrap version — принято

У `init-pve.sh` должна быть собственная версия/schema, например:

```text
PVE-Bootstrap-Version: 1
```

или машинно-читаемое поле в state.

Назначение:

- понимать, какая версия bootstrap уже применялась;
- выполнять миграции `v1 → v2`, а не полную переинициализацию;
- не повторять старые destructive/one-time этапы;
- показывать operator, если требуется migration.

Пример:

```text
Installed bootstrap: 1
Available bootstrap:  2
Action: migration required
```

---

# 12. Итоговый health report — принято

После запуска выводить полный статус, например:

```text
PVE INIT STATUS

[OK] Proxmox detected
[OK] KVM
[OK] pve-no-subscription repository
[OK] DNS
[OK] time sync
[OK] local
[OK] local-lvm
[OK] snippets
[OK] pvedeploy Linux user
[OK] GitHub Deploy Key
[OK] GitHub read-only access
[OK] private repo checkout
[OK] project roles/tokens
[OK] managed pool
[OK] template 9000
[OK] deploy-guest

Repository revision: abcdef123456
Bootstrap version: 1

READY
```

Если требуется ручной этап:

```text
WAITING FOR GITHUB AUTHORIZATION
```

вместо ложного `READY`.

---

# Создание template 9000

После получения private repo `init-pve.sh` не должен содержать полноценную копию builder logic.

Целевая модель после упрощения публичного repo:

```text
PUBLIC proxmox-bootstrap
└── init-pve.sh

PRIVATE proxmox
├── scripts/pve/create-template.sh
├── scripts/pve/deploy-guest...
├── guests/*/guest.yaml
└── остальные infrastructure sources
```

То есть `init-pve.sh`:

```text
получил read-only Git
→ запускает канонический private scripts/pve/create-template.sh
→ проверяет 9000
```

Публичному repo больше не требуется постоянно хранить все downstream scripts.

---

# Порядок работы `init-pve.sh`

Целевой flow:

```text
root запускает public init-pve.sh
→ preflight
→ backup изменяемой host config
→ настроить pve-no-subscription repo
→ установить минимальные packages
→ проверить DNS/time/storage
→ создать pvedeploy
→ создать PVE read-only GitHub key
→ показать public key
→ WAITING FOR GITHUB, если key ещё не зарегистрирован

повторный init-pve.sh
→ проверить GitHub auth
→ shallow clone private zsergeyru/proxmox
→ зафиксировать commit SHA
→ создать утверждённые PVE roles/users/tokens/pool
→ запустить private create-template.sh
→ установить private deploy-guest tooling
→ фактически проверить все этапы
→ health report
→ READY
```

Все этапы должны быть идемпотентными.

---

# Что НЕ должно входить в init-pve

Не устанавливать и не настраивать здесь:

- Docker;
- Hermes/другой AI;
- SmartDNS/VPN/PBR;
- Ansible/Semaphore;
- monitoring stack;
- приложения конкретных VM;
- guest `rootfs`;
- model/provider credentials.

Это уже private desired state и deploy гостевых систем.

---

# Открытые решения перед реализацией

До написания финального `init-pve.sh` нужно окончательно решить только два связанных вопроса:

1. сохраняем ли `managed` pool как обязательную security boundary;
2. использовать две PVE user identities (`deployer@pve` и `proximo@pve`) или одного backing user `automation@pve` с двумя privilege-separated tokens.

Остальные перечисленные в этом документе пункты считаются принятыми для первой версии bootstrap.