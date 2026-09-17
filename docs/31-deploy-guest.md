# `deploy-guest` — спецификация развёртывания гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для поведения команды `deploy-guest`, PLAN/APPLY, Guest Bootstrap v1, безопасного повторного запуска и проверки результата.

Формат `guest.yaml` и итогового требуемого состояния определяет [`30-guest-manifest.md`](30-guest-manifest.md). Права API Proxmox определяет [`25-pve-access-control.md`](25-pve-access-control.md). Модель запуска `root` → `pvedeploy` описана в [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md). Точный контракт Guest Bootstrap и границу с Ansible определяет [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 1. Назначение

`deploy-guest` — средство на стороне PVE-хоста для детерминированного приведения одной VM/LXC к требуемому состоянию, описанному в Git, включая явно запрошенную ограниченную первичную настройку Guest Bootstrap v1 и принятый контракт read-only доступа к проектному репозиторию.

```text
guests/defaults.yaml
+
profile
+
guests/<VMID>-<name>/guest.yaml
+
детерминированные правила проекта
        ↓
scripts/guest_config.py
        ↓
итоговое требуемое состояние
        ↓
фактическое состояние Proxmox
        ↓
PLAN
        ↓
--apply ?
        ↓
APPLY PVE
        ↓
проверка root SSH
        ↓
project_repo_read, если явно запрошен
        ↓
Guest Bootstrap v1, если явно запрошен
        ↓
финальная проверка
        ↓
SUCCESS
```

`deploy-guest` не является универсальным средством управления конфигурацией Linux и не заменяет Ansible.

## 2. Ответственность v1

Первая версия отвечает за:

- поиск и проверку манифеста;
- запуск общего `scripts/validate_repo.py`;
- построение effective state через `scripts/guest_config.py`;
- чтение фактического состояния Proxmox;
- построение полного read-only PLAN;
- создание новой VM или LXC;
- безопасное изменение поддерживаемых параметров существующего управляемого объекта;
- первоначальную установку открытого SSH-ключа PVE при создании;
- запуск гостя согласно `boot.start_after_deploy`;
- проверку административного SSH `root`;
- материализацию фиксированного общего Git read-only credential при `access.project_repo_read: true` после реализации этого поля в schema/resolver;
- PLAN/APPLY явно запрошенных `bootstrap.capabilities`;
- проверку результата каждой capability;
- безопасный повторный запуск после частичной ошибки;
- финальную проверку PVE + SSH + Project Git access + Bootstrap;
- аудит запуска.

`access.project_repo_read` уже принят как контракт, но на момент фиксации этого документа ещё не реализован в действующей schema v6. Реализация schemas/resolver/tests должна следовать этому документу, а не вводить другой интерфейс.

В v1 не входят:

- прикладные сервисы и их Compose stacks;
- применение `rootfs/`;
- обычные Ansible roles/playbooks;
- будущий `provisioning`;
- удаление VM/LXC;
- автоматическое пересоздание;
- `--force`, `--delete`, `--recreate`, `--adopt`;
- уменьшение диска;
- перенос диска между storage;
- автоматическая cross-node migration;
- массовая миграция сети и временный dual-IP;
- автоматический stop/reboot уже работающего существующего гостя ради изменения PVE-конфигурации;
- универсальное управление `authorized_keys` существующих гостей;
- универсальный secret/credential API из manifest;
- выбор произвольного Git credential, repository или write-доступа через `guest.yaml`.

## 3. CLI

```bash
deploy-guest <VMID>
deploy-guest <VMID> --apply
```

Пример:

```bash
deploy-guest 311
deploy-guest 311 --apply
```

Без `--apply` команда выполняет только проверки, read-only обращения к PVE/SSH и строит PLAN. Любая изменяющая операция запрещена — как в PVE, так и внутри гостевой ОС.

С `--apply` сначала строится тот же полный план. Изменения начинаются только после успешного preflight и только если весь план применим безопасно.

## 4. Источники данных

### 4.1. Требуемое состояние гостя

Единственный источник:

```text
guests/defaults.yaml
+
profile
+
guests/<VMID>-<name>/guest.yaml
+
scripts/guest_config.py
```

`deploy-guest.py` не содержит параллельных скрытых default-значений CPU, RAM, диска, сети, storage, pool, source или Bootstrap.

`bootstrap` в v1 задаётся только явно в конкретном `guest.yaml`; он не наследуется из `defaults.yaml` или profile.

После реализации `access.project_repo_read` он также задаётся только конкретным `guest.yaml` и не наследуется из defaults/profile.

### 4.2. Локальная конфигурация PVE

```text
/etc/proxmox-deployer/config.yaml
```

Она описывает сам контур deploy и не подменяет manifest.

### 4.3. PVE API token

```text
/etc/proxmox-deployer/secrets/host-deploy.token
```

Формат:

```text
token_id=deployer@pve!host-deploy
token_secret=<secret>
```

Секрет не выводится и не попадает в audit.

### 4.4. SSH identity гостя

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

`deploy-guest` не создаёт и не ротирует эту пару.

Постоянная база доверенных SSH host keys гостевых систем:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

### 4.5. Общий Git read-only credential

Мастер-копия:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

Назначение фиксировано:

```text
только GitHub Deploy Key READ ONLY
только zsergeyru/proxmox
```

Файл остаётся `root:root 0600`. `pvedeploy` не получает постоянного доступа к нему.

Если текущий effective state требует:

```yaml
access:
  project_repo_read: true
```

root-wrapper открывает **только этот фиксированный файл** и передаёт его содержимое дочернему `deploy-guest.py` через отдельный file descriptor. Запрещено передавать private key через argv, environment или временный файл, читаемый `pvedeploy` после завершения запуска.

`guest.yaml` не задаёт source path, credential id или destination path. Никакого интерфейса `secret: <name>` не существует.

## 5. Фиксация Git revision и root-wrapper

Root-wrapper `/usr/local/sbin/deploy-guest` до запуска Python-кода:

```text
берёт общую orchestration lock
→ проверяет доверенную root-owned копию Git
→ проверяет origin/branch/clean state
→ фиксирует точный SHA
→ строит/проверяет effective state настолько, насколько нужно для root-side решения о credential handoff
→ определяет project_repo_read
→ если false/отсутствует: не открывает Git private key
→ если true: открывает фиксированный root-only Git READ key и передаёт его через выделенный FD
→ передаёт DEPLOY_GUEST_SOURCE_REVISION
→ запускает scripts/pve/deploy-guest.py от pvedeploy
```

Python-код обязан проверить SHA и соответствие реально выполняемой trusted checkout.

`pvedeploy` не может передать root-wrapper произвольный путь и попросить прочитать другой secret. Связка:

```text
project_repo_read
→ /etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

фиксирована реализацией root-wrapper.

Один запуск = одна Git revision.

## 6. Общий валидатор

`deploy-guest` не реализует второй валидатор manifests.

До любых изменений он обязан использовать тот же:

```bash
python scripts/validate_repo.py
```

который используется CI.

Схемы, зависимости Bootstrap, правила IP, profiles и прочие repo-level invariants не дублируются в deployer.

После реализации `access.project_repo_read` общий schema/resolver/validator должен принимать только boolean-поле `project_repo_read` и запрещать произвольные credentials/paths/secrets.

Сам `deploy-guest` дополнительно проверяет только runtime-факты конкретного PVE и SSH-состояние, которые нельзя проверить по Git.

## 7. Обязательный preflight

До первой изменяющей операции должны успешно пройти как минимум:

1. VMID имеет допустимый формат;
2. найден ровно один каталог `guests/<VMID>-<name>/`;
3. `guest.yaml` существует и согласован с именем каталога;
4. `deployable: true`;
5. весь repository проходит `scripts/validate_repo.py`;
6. effective state успешно строится через `scripts/guest_config.py`;
7. effective state проходит schema;
8. `DEPLOY_GUEST_SOURCE_REVISION` корректен и соответствует checkout;
9. `config.yaml` читается;
10. API token читается и имеет ожидаемый token id;
11. SSH private/public key существуют и согласованы;
12. guest `known_hosts` доступен с ожидаемой trust-моделью;
13. локальный PVE API доступен;
14. целевой node существует;
15. bridge существует;
16. storage существует и доступен;
17. pool существует, если задан;
18. source VM/LXC существует;
19. VMID не находится в неоднозначном или чужом состоянии;
20. Bootstrap declaration корректна;
21. `access.project_repo_read` корректен, если соответствующий interface уже реализован;
22. если `project_repo_read=true`, root-wrapper передал ожидаемый FD, а не произвольный secret path;
23. весь PVE + Project Git access + Bootstrap plan не содержит запрещённого действия.

Любая ошибка preflight => STOP до изменения PVE или гостевой ОС.

## 8. PVE API

Основной `scripts/pve/deploy-guest.py` выполняется как `pvedeploy` и работает через HTTPS REST API от имени:

```text
deployer@pve!host-deploy
```

Основная логика не использует `qm`, `pct`, `pvesh` или локальный root как обход RBAC.

Для локального single-node v1:

```text
https://<node>:8006/api2/json
```

TLS verification обязательна с доверенным CA:

```text
/etc/pve/pve-root-ca.pem
```

`verify=false`, `CERT_NONE` и аналоги запрещены.

API token не требует CSRF token.

## 9. Асинхронные задачи PVE

Каждая mutating API-операция, вернувшая UPID:

```text
получить UPID
→ дождаться завершения
→ прочитать exitstatus
→ продолжить только при success
```

Timeout конечный. Для clone/create и коротких операций могут использоваться разные значения.

При timeout/ошибке:

- STOP;
- записать UPID в audit;
- не выполнять следующие зависимые шаги;
- не делать разрушительный rollback;
- следующий запуск заново читает фактическое состояние.

## 10. Поиск фактического объекта

По VMID определяется:

```text
absent | VM | LXC
node
running | stopped
pool
config
tags
description
```

Если тип существующего объекта отличается от требуемого:

```text
VM ↔ LXC mismatch
→ FORBIDDEN / STOP
```

Никакого автоматического пересоздания нет.

## 11. Маркер управляемого объекта

Созданный `deploy-guest` объект получает tag:

```text
proxmox-deployer
```

и детерминированный service block в начале description:

```text
managed-by=proxmox-deployer
source-repo=zsergeyru/proxmox
manifest=guests/<VMID>-<name>/guest.yaml

<description из guest.yaml>
```

Объект считается управляемым только если одновременно:

```text
VMID существует
+ tag proxmox-deployer
+ service block указывает на этот manifest
```

Чужой/непомеченный VMID => STOP. Автоматического adoption в v1 нет.

`deploy-guest` изменяет только свои зарезервированные tags и управляемую часть description; сторонние tags не удаляются.

## 12. `deploy-incomplete`

Незавершённый объект отмечается:

```text
deploy-incomplete
```

Для нового VMID marker `proxmox-deployer`, service block и `deploy-incomplete` записываются как можно раньше после создания.

Для уже управляемого объекта без `deploy-incomplete`, если APPLY должен выполнить реальное изменение Guest Bootstrap **или materialize/update `project_repo_read` credential**, `deploy-incomplete` должен быть установлен перед первой изменяющей SSH-командой.

Tag снимается только после полного успеха:

```text
PVE verified
+ SSH root verified, если guest работает
+ project_repo_read verified, если запрошен
+ все requested Bootstrap capabilities verified
+ final PVE compare successful
```

Если ошибка произошла до установки project marker после allocation VMID, следующий запуск видит unmarked VMID и останавливается для ручной проверки.

Повторный запуск никогда не удаляет частичный объект автоматически.

## 13. PLAN

Итоговый action объекта:

```text
CREATE
UPDATE
NO CHANGE
BLOCKED
```

Каждый PVE diff классифицируется:

```text
ONLINE
REQUIRES-STOP
FORBIDDEN
```

Правила:

- любой `FORBIDDEN` => весь PLAN `BLOCKED`;
- работающий существующий guest + любой PVE `REQUIRES-STOP` => весь PLAN `BLOCKED`;
- если PLAN `BLOCKED`, `--apply` не выполняет никаких изменений, даже если в плане есть отдельные ONLINE-операции;
- v1 не останавливает и не перезагружает существующий running guest автоматически.

Project Git access и Bootstrap отображаются отдельными разделами PLAN.

## 14. PLAN Guest Bootstrap

Канонический порядок:

```text
base → git → docker → ansible_controller
```

Для существующего running + SSH-accessible гостя PLAN может выполнять только read-only checks capability и показывать, например:

```text
Bootstrap
  base                NO CHANGE
  git                 APPLY
  docker              NO CHANGE
  ansible_controller  APPLY
```

Для нового отсутствующего объекта или гостя, который PLAN не может безопасно проверить без изменения состояния:

```text
Bootstrap
  base                APPLY AFTER START
  git                 APPLY AFTER START
  docker              APPLY AFTER START
  ansible_controller  APPLY AFTER START
```

Без `--apply` запрещены package install/update, изменение файлов, service enable/start и любые другие изменяющие SSH-команды.

Bootstrap capability не должна влиять на классификацию PVE diff как ONLINE/REQUIRES-STOP/FORBIDDEN, но ошибка/неоднозначность read-only Bootstrap check может сделать полный PLAN `BLOCKED`.

### 14.1. PLAN `project_repo_read`

Если effective state запрашивает:

```yaml
access:
  project_repo_read: true
```

PLAN показывает отдельное действие, например:

```text
Project repository access
  project_repo_read   NO CHANGE | APPLY | APPLY AFTER START
```

PLAN не копирует private key и не изменяет файлы гостя. Для существующего доступного гостя read-only check может проверить:

- наличие стандартного файла credential;
- `root:root 0600`;
- совпадение fingerprint с ожидаемым общим Git read-key без вывода private key;
- при наличии Git и корректного SSH host trust — read-only `git ls-remote` основного репозитория.

Для нового гостя PLAN показывает `APPLY AFTER START` и не запускает его.

## 15. Создание VM

Поддерживается только:

```yaml
vm:
  source:
    template_vmid: 9000
    clone: full
```

Последовательность:

```text
verify template
→ Full Clone target VMID/name
→ project marker + deploy-incomplete
→ CPU/RAM/disk
→ bridge + Cloud-Init IP/gateway
→ ciuser=root
→ inject pve_guest_ed25519.pub
→ onboot/pool/protection
→ Cloud-Init update
→ verify pre-first-boot config
→ start if start_after_deploy=true
→ readiness
→ root SSH
→ materialize project_repo_read credential if requested
→ Bootstrap if requested
→ final PVE compare
→ remove deploy-incomplete
```

Private административный SSH key внутрь VM не передаётся. Исключение по типу секрета — общий Git read-only credential: его локальная копия специально материализуется только при `project_repo_read=true` и не используется для входа в VM.

## 16. Создание LXC

`lxc.source.ostemplate` — selector семейства, а не pin конкретного archive.

PLAN:

```text
query installed templates on allowed storage
→ выбрать newest matching archive детерминированно
→ pin выбранный archive только для текущего run
```

Если ничего не найдено => STOP до create. `deploy-guest` не скачивает template; это ответственность PVE Configuration.

Последовательность:

```text
resolve installed archive
→ create LXC
→ pass pve_guest_ed25519.pub via ssh-public-keys
→ project marker + deploy-incomplete
→ resources/features/storage/network
→ onboot/pool/protection
→ verify pre-start
→ start if requested
→ root SSH
→ materialize project_repo_read credential if requested
→ Bootstrap if requested
→ final PVE compare
→ remove deploy-incomplete
```

## 17. Начальные SSH-ключи

На create v1 гарантированно устанавливается административный открытый ключ:

```text
pve_guest_ed25519.pub
```

Другие административные public keys могут быть введены позже отдельным явным контрактом.

`project_repo_read` не относится к `authorized_keys`: это исходящий Git client credential, который устанавливается отдельным шагом после проверенного root SSH.

Для существующего управляемого объекта `deploy-guest` не переписывает автоматически `authorized_keys`.

Если существующий running guest должен быть доступен, но PVE deploy key не проходит — STOP и отдельное восстановление доступа.

### 17.1. Материализация общего Git read-key

Стандартный destination:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

Требования:

```text
parent directory root:root, не шире 0700
credential root:root 0600
атомарная запись
никакого вывода содержимого в stdout/stderr/audit
```

Handler не принимает произвольный destination или credential name из manifest.

Если файл уже существует и соответствует ожидаемому fingerprint/правам, `NO CHANGE`.

Если существует другой файл по тому же managed path, его содержимое не выводится; APPLY заменяет его только если объект однозначно находится под управлением этого контракта. Неизвестное/неоднозначное состояние => STOP.

Если `project_repo_read` перестал быть запрошен, v1 **не удаляет** существующую локальную копию автоматически. Отзыв/удаление credential требует отдельного явно спроектированного действия, чтобы обычное изменение manifest не отрезало доступ неожиданно.

## 18. SSH host key trust

Глобальный `StrictHostKeyChecking=no` запрещён.

Для SSH к гостям используется:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Новый объект:

```text
после create/start
→ получить host key только ожидаемого management address
→ принять initial key контролируемым механизмом
→ записать permanent known_hosts
→ повторить реальное соединение со strict verification
```

Существующий managed объект:

```text
known host key совпадает → continue
unexpected host-key change → STOP
```

Нельзя автоматически удалить старый entry и принять новый.

Потеря `known_hosts` не должна приводить к молчаливому повторному доверию всем существующим гостям; требуется осознанная recovery-процедура.

Для исходящего Git SSH из гостя проверка host key GitHub также обязательна. PVE root-only `known_hosts` не копируется гостю как скрытый общий файл; способ установки доверенного GitHub host key должен быть детерминированным и проверяемым.

## 19. Readiness и power semantics

`boot.start_after_deploy` означает только поведение deployer после успешного APPLY, а не постоянное desired power-state:

```text
true  + stopped  → start
true  + running  → не restart только ради этого
false + stopped  → leave stopped
false + running  → не stop
```

Если guest после APPLY работает, success требует SSH `root`.

Для VM перед SSH может ожидаться QGA и Cloud-Init в пределах конечного timeout.

Для LXC ожидаются running state и SSH.

Если guest намеренно остаётся stopped и Bootstrap и `project_repo_read` не запрошены, SSH check пропускается с явным сообщением.

Manifest с Bootstrap обязан иметь `start_after_deploy: true`. Для применения `project_repo_read` guest также должен быть доступен по SSH; если он намеренно остаётся stopped, PLAN показывает невозможность materialize credential без запуска, а APPLY не должен скрыто менять power policy.

## 20. CPU и RAM

Новый guest: параметры задаются до первого boot.

Existing stopped guest: supported CPU/RAM можно reconcile.

Existing running guest: изменение CPU/RAM в v1 всегда `REQUIRES-STOP`, даже если конкретный hotplug теоретически доступен.

## 21. Диск

```text
desired > current  → grow allowed
desired == current → NO CHANGE
desired < current  → FORBIDDEN
storage differs     → FORBIDDEN
```

Disk shrink и storage move запрещены.

`deploy-guest` гарантирует размер виртуального PVE disk/volume. Скрытое расширение filesystem внутри обычной VM не является его универсальной обязанностью.

## 22. Сеть

Новый guest: network задаётся до первого boot.

Existing stopped guest: разрешено поддерживаемое и однозначное изменение bridge/IP/prefix/gateway.

Existing running guest: network change = `REQUIRES-STOP`.

Dual-IP и массовая subnet migration в обычном v1 отсутствуют.

## 23. Pool / protection / onboot

Если PVE API позволяет, эти изменения могут применяться ONLINE.

Pool не является политикой SSH или Git access.

`protection` не используется как механизм защиты от удаления в `deploy-guest`, потому что delete в v1 вообще отсутствует.

## 24. Identity

`vmid`, `name` и type участвуют в identity объекта.

Type mismatch => FORBIDDEN.

Service marker, указывающий на другой manifest => STOP.

Автоматический rename существующего объекта в v1 не выполняется.

## 25. `state` и `deployable`

```yaml
state: planned | active | bootstrap | legacy
```

`state` — lifecycle/documentation, не команда.

Управляемость определяет:

```yaml
deployable: true
```

Поэтому `state: planned` + `deployable: true` допускает create. `legacy` не означает delete/recreate.

## 26. Guest Bootstrap v1

Действующий manifest interface:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Разрешены только:

```text
base
git
docker
ansible_controller
```

Зависимости должны быть явно включены:

```text
git                → base
docker             → base
ansible_controller → base + git + docker
```

Resolver не добавляет зависимости автоматически.

Порядок APPLY всегда:

```text
base → git → docker → ansible_controller
```

Каждая capability реализуется как:

```text
read-only check
→ NO CHANGE или apply
→ final verify
```

Capability обязана быть идемпотентной.

Все bootstrap operations выполняются через проверенный административный SSH, а не через `pct exec`, `qm guest exec` или скрытый root-local канал.

Точный смысл capabilities описан в [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

`bootstrap.git` и `access.project_repo_read` — разные вещи:

```text
bootstrap.git
→ гарантирует наличие Git client

access.project_repo_read
→ разрешает локальную копию фиксированного read-only credential к zsergeyru/proxmox
```

Одно не должно скрыто означать другое.

## 27. Ошибка Bootstrap или Git access

Если capability не применилась, `project_repo_read` не материализовался/не прошёл verify или другая обязательная проверка не прошла:

```text
FAILED
→ следующие зависимые шаги не выполняются
→ deploy-incomplete остаётся
→ объект сохраняется
→ destructive rollback отсутствует
```

Следующий run заново выполняет read-only checks requested состояния и доводит только недостающее.

Номер последнего шага не является источником истины.

## 28. Общий APPLY order

После полного preflight и неблокированного PLAN:

```text
1  create, если absent
2  project marker + deploy-incomplete
3  placement/pool/support params
4  CPU/RAM
5  disk grow
6  network
7  Cloud-Init / initial SSH public key при create
8  onboot/protection
9  verify PVE config pre-start
10 start if needed
11 readiness
12 root SSH, если guest работает
13 установить deploy-incomplete перед первой guest mutation у existing clean object
14 materialize + verify project_repo_read, если запрошен
15 Bootstrap capabilities в canonical order
16 verify Bootstrap
17 final PVE re-read/compare
18 final SSH acceptance, если guest работает
19 remove deploy-incomplete
20 audit SUCCESS
```

Если `project_repo_read` или Bootstrap отсутствуют, соответствующие шаги пропускаются.

## 29. Failure model

`deploy-guest` не обещает полный transaction rollback.

Модель:

```text
step
→ wait/verify
→ next step
```

При любой ошибке:

- STOP;
- не выполнять последующие операции;
- не удалять созданный объект автоматически;
- не уменьшать/откатывать диск;
- не пытаться восстановить «старую» сеть разрушительным guessing;
- не печатать/сохранять переданный Git private key;
- сохранить `deploy-incomplete`, если run уже начал изменять управляемый объект;
- следующий run заново читает actual state PVE, Project Git access и Bootstrap.

## 30. Финальная проверка

HTTP 200 или успешный UPID сами по себе не означают success.

Нужно заново прочитать PVE и сравнить как минимум:

- VMID/type;
- node;
- name;
- project marker;
- pool;
- CPU/RAM;
- PVE disk size;
- bridge/network;
- onboot;
- protection;
- source там, где его можно подтвердить для нового guest;
- отсутствие `deploy-incomplete` после завершения.

Для running Debian VM/LXC обязательно подтвердить `root SSH` PVE deploy key.

Если requested `project_repo_read=true`, обязательно подтвердить стандартный managed credential, владельца/права, ожидаемый fingerprint и, когда Git доступен, read-only доступ к `zsergeyru/proxmox`.

Для каждой requested Bootstrap capability обязательно выполнить её final verify.

## 31. `NO CHANGE`

Если PVE, Project Git access и Bootstrap уже соответствуют desired state:

```text
Action: NO CHANGE
```

В `--apply` режиме mutating API/SSH-команды не выполняются.

Допустимы обязательные read-only availability/acceptance checks.

`deploy-incomplete` не создаётся для чистого `NO CHANGE`.

## 32. Удаление

`deploy-guest` v1 никогда не удаляет VM/LXC.

Ни `state`, ни отсутствие manifest, ни source/type conflict не дают права на destroy.

Аналогично удаление ранее материализованного общего Git read-key только потому, что поле исчезло из manifest, в v1 не выполняется автоматически. Отзыв credential — отдельная явная операция.

Delete/recreate/adopt требуют отдельных будущих операций и спецификаций.

## 33. Audit

```text
/var/log/proxmox-deployer/audit/
```

Каждый run имеет отдельный log и фиксирует:

- date/time;
- VMID/name;
- PLAN или APPLY;
- Git SHA;
- manifest path;
- non-secret desired state;
- минимально необходимый actual state;
- полный plan;
- executed mutations;
- UPID;
- Project Git access check/action/result без содержимого credential;
- Bootstrap checks/actions/results;
- final verification;
- `SUCCESS`, `FAILED` или `BLOCKED`.

Нельзя журналировать:

- `token_secret`;
- private SSH/Git key;
- содержимое credential FD;
- полный environment;
- application secrets.

## 34. Exit codes

```text
0  success / PLAN успешно построен
1  config/schema/repository/preflight error
2  unsafe/ambiguous/BLOCKED
3  PVE API или async task error
4  readiness/QGA/Cloud-Init/SSH/Project-Git/Bootstrap verification error
```

Unexpected internal error => nonzero.

## 35. Зависимости реализации

`deploy-guest.py` обязан переиспользовать:

```text
scripts/guest_config.py
scripts/validate_repo.py
schemas/
```

PVE Configuration обязана подготовить как минимум:

```text
python3
python3-yaml
python3-jsonschema
OpenSSH client
/etc/pve/pve-root-ca.pem
pvedeploy runtime
PVE API token
pve_guest_ed25519
root-only github_proxmox_repo_ed25519
/var/lib/pvedeploy/.ssh/known_hosts
```

Bootstrap handlers не должны хранить secrets в Git и не должны дублировать manifest validation.

Root-wrapper должен владеть логикой безопасной передачи фиксированного Git read-key через FD; основной deployer не должен иметь API получения произвольных root-only секретов.

## 36. Обязательные тесты

Минимальный набор unit/contract tests:

### Manifest / resolver

- lookup manifest по VMID;
- `deployable: false`;
- schema v6;
- отсутствие Bootstrap;
- canonical capability order;
- missing capability dependency;
- Bootstrap при `start_after_deploy=false`;
- unknown capability;
- Bootstrap у non-deployable объекта;
- отсутствие secrets в manifest/audit;
- после реализации: `access.project_repo_read` boolean only;
- после реализации: запрет произвольных credential/path/secret полей.

### PLAN / PVE

- новый VM;
- новый LXC;
- LXC archive selection/missing;
- missing VM template;
- existing correctly managed object;
- foreign/unmarked VMID;
- bad manifest marker;
- VM/LXC type mismatch;
- `NO CHANGE`;
- disk grow/shrink/storage change;
- running guest network/CPU/RAM change => BLOCKED;
- stopped guest update;
- `deploy-incomplete` rerun;
- API failure;
- failed UPID;
- task timeout.

### SSH / Project Git / Bootstrap

- SSH failure;
- SSH host-key mismatch;
- new guest host-key enrolment then strict reconnect;
- `project_repo_read` отсутствует → root не передаёт FD и guest не меняется;
- `project_repo_read=true` → передаётся только фиксированный Git read-key;
- невозможность запросить другой root-only secret;
- PLAN не materialize credential;
- APPLY записывает credential `root:root 0600` атомарно;
- повторный APPLY с совпадающим fingerprint => `NO CHANGE`;
- credential никогда не попадает в audit/stdout/stderr/environment;
- Bootstrap PLAN makes no guest mutations;
- capability `NO CHANGE`;
- capability APPLY + verify;
- capability failure preserves `deploy-incomplete`;
- idempotent rerun after partial Bootstrap;
- `ansible_controller` dependencies/order.

Критический invariant-test: без `--apply` любая попытка mutating HTTP method или изменяющей SSH-команды должна немедленно проваливать тест.

## 37. Явно вне v1

```text
delete
recreate
--force
--adopt
disk shrink
disk storage move
cross-node migration
auto stop/reboot existing running guest
mass network migration
temporary dual IP
universal authorized_keys management
universal secret broker
arbitrary credential selection from guest.yaml
GitHub write access through project_repo_read
rootfs sync
arbitrary bootstrap packages
arbitrary bootstrap shell commands
application provisioning
provisioning.capabilities
AI management through deploy-guest
```

## 38. Главный принцип

`deploy-guest` v1 должен быть скучным и предсказуемым:

```text
Git desired state
→ один общий validator/resolver
→ полный read-only PLAN
→ безопасный APPLY PVE
→ проверенный root SSH
→ только явно запрошенный фиксированный Project Git READ access
→ только явно запрошенный идемпотентный Bootstrap
→ повторная проверка фактического состояния
→ SUCCESS
```

Если состояние неоднозначно или безопасный переход не определён, правильное действие — `BLOCKED/STOP`, а не догадка и не скрытое исправление.