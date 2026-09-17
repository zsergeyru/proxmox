# `deploy-guest` — спецификация развёртывания гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для поведения команды `deploy-guest`, PLAN/APPLY, безопасного повторного запуска и проверки результата.

Формат `guest.yaml` и итогового требуемого состояния определяет [`30-guest-manifest.md`](30-guest-manifest.md). Права API Proxmox определяет [`25-pve-access-control.md`](25-pve-access-control.md). Модель запуска `root` → `pvedeploy` описана в [`26-deploy-guest-and-agent-access.md`](26-deploy-guest-and-agent-access.md). Точный контракт management SSH keys задаёт [`28-management-ssh-keys.md`](28-management-ssh-keys.md). Guest Bootstrap и границу с Ansible определяет [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 1. Назначение

`deploy-guest` — средство на стороне PVE-хоста для детерминированного приведения одной VM/LXC к требуемому состоянию, описанному в Git.

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
проверка root SSH deployer
        ↓
management.ssh_identity, если явно запрошен
        ↓
management.project_repo_read, если явно запрошен
        ↓
Guest Bootstrap v1, если явно запрошен
        ↓
финальная проверка
        ↓
SUCCESS
```

`deploy-guest` не является универсальным средством управления Linux и не заменяет Ansible.

Массовое распространение management public keys выполняет отдельный `sync-management-keys`; `deploy-guest` регистрирует `.pub` гостя с `management.ssh_identity: true` и вызывает sync после фактического изменения registry.

Для Debian-гостя, effective state которого содержит `management.ssh`, `deploy-guest` устанавливает технический PVE tag `management-ssh`. Этот tag определяет область обнаружения для `sync-management-keys` и не заменяет ownership tag `proxmox-deployer`.

## 2. Ответственность v1

Первая версия отвечает за:

- поиск и проверку манифеста;
- запуск общего `scripts/validate_repo.py`;
- построение effective state через `scripts/guest_config.py`;
- чтение фактического состояния Proxmox;
- построение полного read-only PLAN;
- создание новой VM или LXC;
- безопасное изменение поддерживаемых параметров существующего управляемого объекта;
- передачу текущего `management-authorized-keys` при создании Debian VM/LXC;
- установку PVE tag `management-ssh` для Debian-гостя с `management.ssh`;
- запуск гостя согласно `boot.start_after_deploy`;
- проверку административного SSH `root` private key deployer;
- после реализации `management.ssh_identity` — создание/проверку guest-local management keypair и регистрацию только `.pub`;
- запуск `sync-management-keys` после нового/изменённого зарегистрированного public key;
- после реализации `management.project_repo_read` — материализацию фиксированного общего Git read-only credential;
- PLAN/APPLY явно запрошенных `bootstrap.capabilities`;
- проверку результата каждого управляемого шага;
- безопасный повторный запуск после частичной ошибки;
- финальную проверку PVE + SSH + management identity + Project Git access + Bootstrap;
- аудит запуска.

`management.ssh_identity` и `management.project_repo_read` уже приняты как целевые контракты, но ещё не реализованы в действующей schema v6. Реализация schemas/resolver/tests должна следовать этим документам, а не вводить другой интерфейс.

В v1 не входят:

- прикладные сервисы и Compose stacks;
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
- автоматический stop/reboot работающего существующего гостя ради изменения PVE-конфигурации;
- универсальный secret/credential API из manifest;
- выбор произвольного management key path/name/role из manifest;
- произвольный Git credential/repository/write-доступ через `guest.yaml`.

## 3. CLI

```bash
deploy-guest <VMID>
deploy-guest <VMID> --apply
```

Без `--apply` выполняются только проверки и PLAN. Любая изменяющая операция запрещена — как в PVE, так и внутри гостевой ОС, в public-key registry или через `sync-management-keys`.

С `--apply` сначала строится тот же полный план. Изменения начинаются только после успешного preflight и только если весь план применим безопасно.

## 4. Источники данных

### 4.1. Требуемое состояние

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

`deploy-guest.py` не содержит параллельных скрытых default-значений CPU, RAM, диска, сети, storage, pool, source, Bootstrap, management SSH identity или Project Git access.

`bootstrap`, `management.ssh_identity` и `management.project_repo_read` задаются только конкретным `guest.yaml` и не наследуются из defaults/profile. Базовый `management.ssh` может приходить из общих defaults.

### 4.2. Локальная конфигурация PVE

```text
/etc/proxmox-deployer/config.yaml
```

Она описывает сам deploy-контур и не подменяет manifest.

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

### 4.4. SSH identity deployer

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

`deploy-guest` не создаёт и не ротирует эту пару.

Историческое имя сохраняется, но это identity deployer для входа в гостевые ОС.

Постоянная база доверенных SSH host keys гостевых систем:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

### 4.5. Management public-key registry

Канонический публичный каталог:

```text
/etc/proxmox-deployer/public-keys/
```

Агрегированный текущий набор:

```text
/etc/proxmox-deployer/public-keys/management-authorized-keys
```

При создании новой управляемой Debian VM/LXC `deploy-guest` передаёт этот набор целиком через Cloud-Init `sshkeys` или LXC `ssh-public-keys` и устанавливает PVE tag `management-ssh`.

Если целевой interface ещё не реализован либо aggregate отсутствует в bootstrap-переходный период, минимально допустимый начальный ключ — `deployer.pub`; после реализации registry отсутствие валидного aggregate считается preflight error.

### 4.6. Общий Git read-only credential

Мастер-копия:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

Назначение фиксировано: только `zsergeyru/proxmox`, только read-only.

Если effective state требует:

```yaml
management:
  project_repo_read: true
```

root-wrapper открывает только этот фиксированный файл и передаёт содержимое дочернему deploy-процессу через выделенный FD. Private key запрещено передавать через argv, environment или постоянный файл, доступный `pvedeploy`.

## 5. Root-wrapper и Git revision

Root-wrapper `/usr/local/sbin/deploy-guest` до запуска Python-кода:

```text
берёт общую orchestration lock
→ проверяет доверенную root-owned копию Git
→ проверяет origin/branch/clean state
→ фиксирует точный SHA
→ строит/проверяет effective state настолько, насколько нужно root-side решению о Git credential
→ определяет management.project_repo_read
→ при необходимости открывает только фиксированный root-only Git READ key
→ передаёт DEPLOY_GUEST_SOURCE_REVISION
→ запускает scripts/pve/deploy-guest.py от pvedeploy
```

Python-код обязан проверить SHA и соответствие реально выполняемой trusted checkout.

`pvedeploy` не может передать root-wrapper произвольный путь и попросить прочитать другой secret.

Один запуск = одна Git revision.

Management public keys не требуют secret FD: они находятся в отдельном публичном registry и управляются по контракту [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

## 6. Общий валидатор

До любых изменений используется тот же:

```bash
python scripts/validate_repo.py
```

который запускается CI.

Схемы, зависимости Bootstrap, правила IP, profiles, `management.ssh_identity` и `management.project_repo_read` не дублируются отдельным набором правил в `deploy-guest.py`.

После реализации:

```text
management.ssh_identity
→ только boolean
→ без role/key_name/path/target_vmid/private material

management.project_repo_read
→ только boolean
→ без произвольных credentials/paths/secrets
```

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
11. deployer private/public key существуют и согласованы;
12. guest `known_hosts` доступен с ожидаемой trust-моделью;
13. management public-key registry валиден, если механизм уже реализован;
14. `deployer.pub` присутствует и соответствует `pve_guest_ed25519.pub`;
15. локальный PVE API доступен;
16. целевой node существует;
17. bridge существует;
18. storage существует и доступен;
19. pool существует, если задан;
20. source VM/LXC существует;
21. VMID не находится в неоднозначном или чужом состоянии;
22. Bootstrap declaration корректна;
23. `management.ssh_identity` корректен, если interface реализован;
24. `management.project_repo_read` корректен, если interface реализован;
25. если `management.project_repo_read=true`, root-wrapper передал ожидаемый FD;
26. если effective state содержит `management.ssh`, PLAN предусматривает tag `management-ssh`;
27. весь PVE + management identity + Project Git access + Bootstrap plan не содержит запрещённого действия.

Любая ошибка preflight => STOP до изменения PVE, гостевой ОС или public-key registry.

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

Каждая изменяющая API-операция, вернувшая UPID:

```text
получить UPID
→ дождаться завершения
→ прочитать exitstatus
→ продолжить только при success
```

Timeout конечный.

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

## 11. Маркеры PVE-объекта

Созданный `deploy-guest` объект получает ownership tag:

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

Объект считается управляемым `deploy-guest` по lifecycle только если одновременно:

```text
VMID существует
+ tag proxmox-deployer
+ service block указывает на этот manifest
```

Чужой/непомеченный VMID => STOP. Автоматического adoption нет.

Отдельно для management SSH-контура используется технический tag:

```text
management-ssh
```

`deploy-guest` ставит его Debian-гостю, если effective state содержит `management.ssh`. `sync-management-keys` рассматривает только объекты с этим tag, но дополнительно fail-closed проверяет объект и SSH trust.

`management-ssh` не означает ownership со стороны `deploy-guest`: AI-created Debian guest может иметь `management-ssh` без `proxmox-deployer`.

## 12. `deploy-incomplete`

Новый объект получает tag:

```text
deploy-incomplete
```

как можно раньше после создания.

Он снимается только после успешной полной приёмки:

```text
PVE state verified
+ root SSH deployer verified
+ management.ssh_identity verified + public key synced, если запрошен
+ management.project_repo_read verified, если запрошен
+ Bootstrap verified, если запрошен
+ final PVE verification
```

Если существующий полностью управляемый объект требует реального изменения внутри гостя — management SSH identity, Project Git access или Bootstrap — `deploy-incomplete` устанавливается перед первой изменяющей SSH-командой.

Read-only PLAN/NO CHANGE не создают тег.

При ошибке tag остаётся. Разрушительный rollback не выполняется.

## 13. Категории PLAN

Для PVE-параметров:

```text
CREATE
UPDATE
NO CHANGE
BLOCKED
```

Изменения параметров классифицируются дополнительно:

```text
ONLINE
REQUIRES-STOP
FORBIDDEN
```

Если объект работает и требуется изменение `REQUIRES-STOP`, v1 не останавливает его автоматически и показывает BLOCKED.

`FORBIDDEN` всегда блокирует APPLY.

Для post-SSH части PLAN показывает отдельно:

```text
Management SSH identity
  NOT REQUESTED | APPLY AFTER START | APPLY | NO CHANGE | BLOCKED

Project repo read
  NOT REQUESTED | APPLY AFTER START | APPLY | NO CHANGE | BLOCKED

Bootstrap
  base                APPLY AFTER START | APPLY | NO CHANGE | BLOCKED
  ...
```

Если management SSH identity требует новой регистрации `.pub`, PLAN также показывает, что после APPLY потребуется `sync-management-keys`, но сам sync в PLAN не запускается.

## 14. Создание VM

Для новой VM:

```text
проверить template source
→ выполнить Full Clone
→ установить ownership marker + deploy-incomplete
→ настроить CPU/RAM/disk/network
→ ciuser=root
→ inject current management-authorized-keys через Cloud-Init sshkeys
→ если management.ssh присутствует: установить PVE tag management-ssh
→ onboot/pool/protection
→ Cloud-Init update
→ preboot verify
→ start, если требуется
→ readiness
→ принять/проверить SSH host key по правилам trust
→ проверить root SSH private key deployer
→ management.ssh_identity handler, если запрошен
→ при новом public key зарегистрировать его и вызвать sync-management-keys
→ management.project_repo_read handler, если запрошен
→ Bootstrap handlers
→ final verify
→ снять deploy-incomplete
```

Шаблон `9000` сам management credentials не содержит.

## 15. Создание LXC

Для нового LXC:

```text
разрешить selector Debian template
→ детерминированно выбрать установленный подходящий archive
→ создать LXC
→ установить ownership marker + deploy-incomplete
→ CPU/RAM/swap/rootfs/network/features
→ ssh-public-keys=<current management-authorized-keys>
→ если management.ssh присутствует: установить PVE tag management-ssh
→ onboot/pool/protection
→ prestart verify
→ start, если требуется
→ SSH readiness/trust
→ проверить root SSH private key deployer
→ management.ssh_identity handler, если запрошен
→ при новом public key зарегистрировать его и вызвать sync-management-keys
→ management.project_repo_read handler, если запрошен
→ Bootstrap handlers
→ final verify
→ снять deploy-incomplete
```

`deploy-guest` не скачивает LXC template сам.

## 16. Management SSH identity handler

Целевой `management.ssh_identity: true` означает ровно контракт [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

Стандартная пара внутри гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Read-only check должен определить:

```text
ABSENT
VALID PAIR
BROKEN / AMBIGUOUS
```

PLAN:

```text
ABSENT + APPLY allowed
→ APPLY / APPLY AFTER START

VALID PAIR + matching registry
→ NO CHANGE

VALID PAIR + registry missing
→ APPLY registration

BROKEN / fingerprint conflict
→ BLOCKED / STOP
```

APPLY при полном отсутствии пары:

```text
создать каталог root:root 0700
→ сгенерировать ed25519 внутри гостя
→ private root:root 0600
→ public root:root 0644
→ вывести наружу только public line/fingerprint
→ зарегистрировать /etc/proxmox-deployer/public-keys/<VMID>-<name>.pub
→ проверить fingerprint
→ запустить sync-management-keys
```

Private key не переносится на PVE.

Если пара частично повреждена или registry содержит другой fingerprint, обычный deploy не ротирует identity автоматически.

## 17. `sync-management-keys`

Это отдельная PVE-команда и отдельный контракт. `deploy-guest` не реализует внутри себя массовый обход гостей.

После успешной регистрации нового/изменённого public key APPLY вызывает:

```bash
sync-management-keys
```

Команда:

```text
валидирует весь PVE public-key registry
→ пересобирает management-authorized-keys
→ перечисляет PVE VM/LXC с tag management-ssh
→ fail-closed проверяет объект и SSH trust
→ синхронизирует public catalog в участвующие Debian-гости
→ обновляет только управляемый блок /root/.ssh/authorized_keys
→ проверяет результат
```

Оператор может запустить её вручную в любой момент.

Ошибка sync после создания guest-local private key не вызывает удаления private key или VM/LXC. Deploy остаётся FAILED/incomplete до успешного повторного sync.

## 18. Project Git read handler

При `management.project_repo_read: true` после проверенного SSH:

```text
получить private bytes только из переданного root-wrapper FD
→ атомарно записать в госте:
  /etc/proxmox-guest/credentials/github-proxmox-read
→ root:root 0600
→ проверить fingerprint ожидаемой public части
→ не выводить private content
```

Если Git client и доверенный GitHub host key доступны, final verify может выполнить read-only `git ls-remote` к точному `zsergeyru/proxmox`.

Обычный deploy не удаляет уже выданный Git read credential автоматически только потому, что поле исчезло: отзыв/ротация — отдельная явная операция.

## 19. Guest Bootstrap

После management SSH identity и Project Git access выполняются только явно включённые capabilities в порядке:

```text
base → git → docker → ansible_controller
```

Каждый handler:

```text
read-only check
→ NO CHANGE или APPLY
→ final verify
```

Зависимости не добавляются автоматически.

Точные пакеты, запреты `apt upgrade/full-upgrade/dist-upgrade/autoremove`, конечные timeout и другие правила находятся в [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 20. SSH trust

Для нового гостя допускается первоначальное принятие SSH host key только для вычисленного ожидаемого адреса, после чего соединение немедленно повторяется со строгой проверкой.

Для существующего гостя неожиданная смена SSH host key:

```text
→ STOP
```

`deploy-guest` и `sync-management-keys` никогда не выполняют автоматический `ssh-keygen -R`/удаление known_hosts ради продолжения.

Потеря `/var/lib/pvedeploy/.ssh/known_hosts` требует отдельного явного сценария восстановления доверия.

## 21. Изменение ресурсов

Поддерживается безопасное увеличение диска. Уменьшение диска запрещено.

Storage move в v1 запрещён.

Для существующего остановленного объекта разрешённые изменения CPU/RAM/network применяются по контракту API.

Для работающего объекта изменения, требующие остановки, не выполняются автоматически:

```text
→ BLOCKED / REQUIRES-STOP
```

Автоматического stop/reboot нет.

## 22. Сеть

Обычный deploy поддерживает один административный IPv4 и один default gateway согласно effective state.

Массовая миграция, временный dual-IP и изменение нескольких гостей одной командой не входят в v1.

После сетевого изменения обязательна проверка ожидаемого адреса и SSH trust.

## 23. Повторный запуск

Каждый запуск заново читает:

```text
Git desired state
PVE actual state
SSH actual state
management SSH identity actual state/registry
Project Git access
Bootstrap state
```

Не используется слепое продолжение «с шага N» по журналу прошлого запуска.

Идемпотентный повторный APPLY должен выполнять только недостающие изменения.

Если VM/LXC уже полностью соответствует — `NO CHANGE` и никаких лишних SSH/package/sync операций.

## 24. Аудит

Каждый APPLY/PLAN пишет аудит без секретов.

Допустимо журналировать:

- время;
- VMID/name;
- Git SHA;
- планируемые/применённые действия;
- PVE UPID;
- fingerprints public keys;
- факт регистрации `.pub`;
- факт/результат `sync-management-keys`;
- Bootstrap result;
- Project Git verification result;
- итоговый exit code.

Запрещено журналировать:

- API token secret;
- private SSH keys;
- private Git read-key;
- полный environment;
- содержимое файлов credentials.

## 25. Exit codes

```text
0  SUCCESS / PLAN valid / NO CHANGE
1  validation or runtime failure
2  BLOCKED / unsafe state requiring operator action
3  foreign/ownership conflict or forbidden requested change
4  post-PVE acceptance failure: SSH, management key sync, Project Git access, Bootstrap or final verification
```

Точная реализация может детализировать сообщения, но смысл классов ошибок должен оставаться стабильным для автоматизации.

## 26. Что делает AI и чего не делает deploy

AI Control штатно не вызывает `deploy-guest` на PVE. Для обычных managed VM/LXC он использует Proximo и `ai-agent@pve!infra`.

При создании Debian VM/LXC AI обязан передать локально синхронизированный:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и установить PVE tag:

```text
management-ssh
```

Поэтому новая AI-created машина сразу содержит deployer и остальные зарегистрированные public keys и однозначно доступна последующему PVE-side key sync.

Если такой объект позже должен получить **собственную** management identity, в v1 это выполняется штатным `deploy-guest` для manifest с:

```yaml
management:
  ssh_identity: true
```

AI не получает remote write API к PVE public-key registry.

## 27. Состояние реализации

На момент фиксации документа:

```text
bootstrap.capabilities
→ действующий schema v6 contract

management.project_repo_read
→ принятый target contract, schema/resolver ещё не реализованы

management.ssh_identity
→ принятый target contract, schema/resolver/deployer/sync ещё не реализованы

management-ssh
→ принятый технический PVE tag для области sync, реализация ещё требуется
```

Поэтому документация определяет требуемую реализацию, но рабочие `guest.yaml` не должны получать неизвестные schema v6 поля раньше соответствующего изменения schemas/resolver/tests.

## 28. Главный принцип

> `deploy-guest` детерминированно управляет одной описанной в Git VM/LXC. Новая Debian-машина получает весь актуальный management public-key set и PVE tag `management-ssh`; собственный private management key создаётся только внутри гостя с `management.ssh_identity: true`, наружу регистрируется лишь `.pub`, а массовое распространение выполняет отдельный `sync-management-keys`. `management.project_repo_read` и Guest Bootstrap остаются независимыми последующими механизмами внутри единого manifest-раздела `management`.