# `deploy-guest` — спецификация развёртывания гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для поведения команды `deploy-guest`, PLAN/APPLY, безопасного повторного запуска и проверки результата.

Формат `guest.yaml` задаёт [`30-guest-manifest.md`](30-guest-manifest.md), management SSH keys — [`28-management-ssh-keys.md`](28-management-ssh-keys.md), PVE ACL — [`25-pve-access-control.md`](25-pve-access-control.md), Guest Bootstrap — [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md). Текущая степень реализации отдельно фиксируется в [`29-implementation-status.md`](29-implementation-status.md).

## 1. Назначение

`deploy-guest` детерминированно приводит одну VM/LXC к desired state из Git:

```text
guests/defaults.yaml
+ profile
+ guests/<VMID>-<name>/guest.yaml
        ↓
scripts/guest_config.py
        ↓
effective state
        ↓
PVE actual state
        ↓
PLAN
        ↓
--apply ?
        ↓
APPLY PVE
        ↓
verified root SSH deployer
        ↓
management.ssh_identity
        ↓
management.project_repo_read
        ↓
Guest Bootstrap
        ↓
final verify
        ↓
SUCCESS
```

Команда не заменяет Ansible и не является универсальным Linux configuration manager.

## 2. Контракт v1

`deploy-guest` отвечает за:

- поиск и проверку manifest;
- общий `scripts/validate_repo.py`;
- effective state через `scripts/guest_config.py`;
- полный read-only PLAN;
- создание VM/LXC и безопасное изменение поддерживаемых PVE-параметров;
- передачу текущего `management-authorized-keys` новой Debian VM/LXC;
- PVE tag `management-ssh` при наличии `management.ssh`;
- запуск согласно `boot.start_after_deploy`;
- первое сохранение SSH host key новой машины и строгую дальнейшую сверку;
- проверку root SSH deployer;
- `management.ssh_identity` handler;
- регистрацию `<VMID>.pub` и запуск `sync-management-keys` после изменения registry;
- `management.project_repo_read` APPLY/REMOVE/VERIFY;
- явно запрошенные `bootstrap.capabilities`;
- финальную приёмку и audit.

Schema v7 уже определяет `management.ssh_identity` и `management.project_repo_read` как boolean desired state. `deploy-guest` обязан использовать именно effective state общего resolver и не вводить параллельный интерфейс.

В v1 не входят:

```text
application services / Compose stacks
rootfs sync
обычные Ansible roles/playbooks
универсальный provisioning API
удаление VM/LXC
recreate/adopt/force
уменьшение диска
storage move
cross-node migration
массовая network migration / dual-IP
auto stop/reboot существующего running guest
универсальный secret API
произвольный Git credential/repository/write access
```

## 3. CLI и режимы

```bash
deploy-guest <VMID>
deploy-guest <VMID> --apply
```

Без `--apply` любые изменения запрещены: PVE, guest OS, registry и `sync-management-keys` остаются read-only.

С `--apply` сначала строится тот же полный PLAN. Изменения начинаются только после успешного preflight.

## 4. Источники данных и границы доверия

### Desired state

Единственный источник:

```text
guests/defaults.yaml
+ profile
+ guests/<VMID>-<name>/guest.yaml
+ scripts/guest_config.py
```

`bootstrap`, `management.ssh_identity` и `management.project_repo_read` задаются только конкретному guest и не наследуются из defaults/profile. Базовый `management.ssh` может наследоваться.

### PVE local config

```text
/etc/proxmox-deployer/config.yaml
```

### API token

```text
/etc/proxmox-deployer/secrets/host-deploy.token
```

Ожидаемый token id:

```text
deployer@pve!host-deploy
```

### Deployer SSH identity

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

`deploy-guest` эту пару не создаёт и не ротирует.

### Guest SSH trust database

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

### Management public-key registry

```text
/var/lib/proxmox-deployer/public-keys/
├── deployer.pub
├── <VMID>.pub
└── management-authorized-keys
```

Владелец — `pvedeploy:pvedeploy`. `deploy-guest.py` может изменять этот **публичный** registry напрямую после полной валидации и только атомарно.

### Project Git READ master

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

Это root-only master read key только для `zsergeyru/proxmox`.

## 5. Root-wrapper

`/usr/local/sbin/deploy-guest` запускается от root и до передачи управления `pvedeploy`:

```text
берёт orchestration lock
→ проверяет root-trusted checkout
→ проверяет origin/branch/clean state
→ фиксирует точный Git SHA
→ строит и проверяет effective state
→ если project_repo_read=true: открывает только фиксированный Git READ key
→ передаёт secret только через выделенный FD
→ передаёт DEPLOY_GUEST_SOURCE_REVISION
→ запускает scripts/pve/deploy-guest.py от pvedeploy
```

Private key запрещено передавать через argv/environment или постоянный файл, читаемый `pvedeploy`.

`pvedeploy` не может попросить wrapper прочитать произвольный secret path.

Один запуск = одна Git revision.

## 6. Общий resolver и schema v7

До изменений выполняется:

```bash
python scripts/validate_repo.py
```

`deploy-guest.py` не дублирует правила schemas/resolver.

Действующий contract:

```text
management.ssh_identity
→ boolean
→ только конкретный guest.yaml
→ absent = false
→ без role/key_name/path/target_vmid/private material

management.project_repo_read
→ boolean
→ только конкретный guest.yaml
→ absent = false
→ без credentials/path/secret/repository selector
```

Если любой из этих flags = `true`, effective `boot.start_after_deploy` должен быть `true`, поскольку handler работает только после verified SSH root.

## 7. Обязательный preflight

До первой mutation проверяются как минимум:

1. корректный VMID и ровно один `guests/<VMID>-<name>/`;
2. `deployable: true`;
3. весь repository проходит validator;
4. effective state успешно строится и проходит schema v7;
5. source revision совпадает с trusted checkout;
6. local config и ожидаемый API token доступны;
7. deployer private/public key согласованы;
8. guest known_hosts доступен с ожидаемыми владельцем/правами;
9. management registry валиден и содержит корректный `deployer.pub`;
10. PVE API, node, bridge, storage, pool и source доступны;
11. существующий VMID не находится в foreign/ambiguous state;
12. для новой management identity нет неподтверждённого stale `<VMID>.pub`;
13. Bootstrap declaration корректна;
14. management flags корректны по schema v7;
15. при `project_repo_read=true` wrapper передал ожидаемый FD;
16. полный PLAN не содержит запрещённых действий.

Ошибка preflight => `STOP` до первой mutation.

## 8. PVE API и async tasks

Основной процесс работает как `pvedeploy` через HTTPS REST API:

```text
https://<node>:8006/api2/json
```

TLS verification обязательна с:

```text
/etc/proxmox-deployer/pve-root-ca.pem
```

`verify=false`, `CERT_NONE`, локальные `qm/pct/pvesh` как обход RBAC запрещены.

Любая API mutation с UPID:

```text
получить UPID
→ дождаться завершения с конечным timeout
→ проверить exitstatus
→ продолжить только при success
```

Ошибка/timeout => STOP без разрушительного rollback; следующий запуск заново читает actual state.

## 9. Ownership и PVE tags

Lifecycle ownership объекта `deploy-guest` требует одновременно:

```text
tag proxmox-deployer
+
service block в description:
managed-by=proxmox-deployer
source-repo=zsergeyru/proxmox
manifest=guests/<VMID>-<name>/guest.yaml
```

Foreign/unmarked VMID => STOP; automatic adoption отсутствует.

Отдельные runtime tags:

```text
management-ssh
→ участие в management SSH sync

deploy-incomplete
→ объект создан/изменяется, но полная приёмка ещё не завершена
```

`management-ssh` не является ownership marker. AI-created guest может иметь `management-ssh` без `proxmox-deployer`.

`deploy-incomplete` снимается только после успешной проверки PVE + SSH + requested management state + Bootstrap + final verify.

## 10. Категории PLAN

PVE actions:

```text
CREATE
UPDATE
NO CHANGE
BLOCKED
```

Дополнительная классификация:

```text
ONLINE
REQUIRES-STOP
FORBIDDEN
```

Running guest автоматически не останавливается ради `REQUIRES-STOP`.

Post-SSH PLAN:

```text
Management SSH identity
  NOT REQUESTED | APPLY AFTER START | APPLY | NO CHANGE | BLOCKED

Project repo read
  NOT REQUESTED | APPLY AFTER START | APPLY | REMOVE | NO CHANGE | BLOCKED

Bootstrap
  <capability>  APPLY AFTER START | APPLY | NO CHANGE | BLOCKED
```

PLAN никогда не генерирует management key, не пишет credential и не запускает sync.

## 11. Создание VM

```text
verify template source
→ Full Clone
→ ownership + deploy-incomplete
→ CPU/RAM/disk/network
→ ciuser=root
→ Cloud-Init sshkeys=<management-authorized-keys>
→ management-ssh tag
→ onboot/pool/protection
→ Cloud-Init update
→ start if requested
→ readiness
→ первое сохранение SSH host key нового ожидаемого адреса
→ повторное SSH уже со strict check
→ verify root SSH deployer
→ management.ssh_identity handler
→ registry change ? sync-management-keys
→ project_repo_read desired-state handler
→ Bootstrap handlers
→ final verify
→ remove deploy-incomplete
```

Template 9000 не содержит постоянных project management credentials.

## 12. Создание LXC

```text
resolve allowed Debian template selector
→ create LXC
→ ownership + deploy-incomplete
→ CPU/RAM/swap/rootfs/network/features
→ ssh-public-keys=<management-authorized-keys>
→ management-ssh tag
→ onboot/pool/protection
→ start if requested
→ readiness
→ first host-key trust for new expected address
→ verify root SSH deployer
→ management.ssh_identity handler
→ registry change ? sync-management-keys
→ project_repo_read desired-state handler
→ Bootstrap handlers
→ final verify
→ remove deploy-incomplete
```

`deploy-guest` не скачивает LXC template во время deploy.

## 13. Management SSH identity handler

Точный lifecycle определён в [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

Стандартная пара:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Read-only state:

```text
ABSENT
VALID PAIR
BROKEN / AMBIGUOUS
```

PLAN/APPLY:

```text
ABSENT
→ APPLY / APPLY AFTER START

VALID PAIR + matching registry
→ NO CHANGE

VALID PAIR + registry missing
→ APPLY registration

BROKEN / fingerprint conflict
→ BLOCKED / STOP
```

При полном отсутствии pair APPLY создаёт ed25519 **внутри guest**, оставляет private `0600`, получает наружу только public line/fingerprint, регистрирует `/var/lib/proxmox-deployer/public-keys/<VMID>.pub` и запускает sync.

Обычный deploy не ротирует identity.

## 14. `sync-management-keys`

Это отдельная PVE-команда, а не внутренний массовый цикл `deploy-guest`.

После фактического изменения registry:

```bash
sync-management-keys
```

Команда валидирует весь registry, пересобирает aggregate, находит `management-ssh` participants, строго проверяет SSH trust, синхронизирует guest public catalog и только managed block `/root/.ssh/authorized_keys`.

Ошибка sync оставляет deploy incomplete; private key или VM/LXC не удаляются.

## 15. Project Git READ handler

`management.project_repo_read` управляет полным desired state фиксированного read-only доступа только к `zsergeyru/proxmox`.

### `true`

После verified SSH handler:

```text
получить private bytes из wrapper FD
→ проверить ожидаемый fingerprint
→ атомарно обеспечить:
  /etc/proxmox-guest/credentials/github-proxmox-read          root:root 0600
  /etc/proxmox-guest/ssh/github-proxmox-known_hosts          root:root 0644
  /etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf    root:root 0644
→ обеспечить SSH alias github-proxmox-read
→ обеспечить точный managed block Git URL rewrite только для zsergeyru/proxmox
→ verify GitHub SSH authentication этим credential без требования установленного Git client
→ если Git client уже есть: дополнительно verify git ls-remote по каноническому URL
→ после Bootstrap capability `git`: final verify обязательно включает git ls-remote
```

SSH alias:

```text
Host github-proxmox-read
HostName github.com
User git
IdentityFile /etc/proxmox-guest/credentials/github-proxmox-read
IdentitiesOnly yes
UserKnownHostsFile /etc/proxmox-guest/ssh/github-proxmox-known_hosts
StrictHostKeyChecking yes
BatchMode yes
```

Git rewrite:

```text
git@github.com:zsergeyru/proxmox.git
→ git@github-proxmox-read:zsergeyru/proxmox.git
```

Если всё соответствует — `NO CHANGE`. Неожиданный fingerprint, неизвестное содержимое управляемого config или конфликтующая rewrite-запись => `BLOCKED / STOP`.

### `false` или поле отсутствует

Новый credential не выдаётся. Если управляемые Project Git READ artifacts уже есть:

```text
PLAN = REMOVE
```

`--apply` удаляет только:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
/etc/proxmox-guest/ssh/github-proxmox-known_hosts
/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf
точную project Git rewrite-запись
```

Рабочие копии repo и чужие Git/SSH credentials не трогаются. Перед удалением ownership управляемых артефактов проверяется; ambiguous drift => STOP.

Ротация общего read key выполняется только как отдельное подтверждённое состояние с ожидаемым новым fingerprint.

## 16. Guest Bootstrap

После management handlers выполняются только явно включённые capabilities:

```text
base → git → docker → ansible_controller
```

Каждый handler:

```text
read-only check
→ NO CHANGE или APPLY
→ final verify
```

Полный контракт — [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md).

## 17. SSH trust гостевых систем

Для **только что созданного** объекта в доверенной домашней сети:

```text
известны VMID + expected IP
→ дождаться SSH
→ получить host key
→ сохранить /var/lib/pvedeploy/.ssh/known_hosts
→ немедленно повторить соединение со strict check
```

Для существующего guest неожиданная смена host key => STOP.

Автоматический `ssh-keygen -R`, очистка known_hosts ради продолжения и молчаливое повторное доверие запрещены.

## 18. Ресурсы и сеть

Разрешено безопасное увеличение диска; уменьшение и storage move в v1 запрещены.

Изменения работающего объекта, требующие stop/reboot:

```text
BLOCKED / REQUIRES-STOP
```

Автоматического stop/reboot нет.

Обычный deploy поддерживает один administrative IPv4 и один default gateway по effective state. Массовая migration и temporary dual-IP — отдельный инструмент.

## 19. Повторный запуск

Каждый запуск заново читает:

```text
Git desired state
PVE actual state
SSH actual state
management identity + registry
Project Git READ state
Bootstrap state
```

Слепого «продолжить с шага N» по старому журналу нет.

Идемпотентный APPLY выполняет только недостающие изменения. Полностью соответствующий guest => `NO CHANGE` без лишнего sync/package work.

## 20. Audit

Можно журналировать:

```text
time
VMID/name
Git SHA
PLAN/APPLY actions
UPID
public-key fingerprints
registry/sync result
host-key trust event без private material
Bootstrap result
Project Git verify result
exit code
```

Запрещено журналировать:

```text
API token secret
private SSH keys
private Git read key
полный environment
credential file contents
```

## 21. Exit codes

```text
0  SUCCESS / PLAN valid / NO CHANGE
1  validation or runtime failure
2  BLOCKED / unsafe state requiring operator action
3  foreign/ownership conflict or forbidden requested change
4  post-PVE acceptance failure: SSH / key sync / Project Git / Bootstrap / final verify
```

## 22. AI-created guests

AI Control штатно создаёт обычные managed VM/LXC через разрешённый PVE API-контур, а не через PVE-side `deploy-guest`.

Для новой Debian VM/LXC AI передаёт:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и устанавливает:

```text
management-ssh
```

AI не получает write API к PVE public-key registry и не пишет в файловую систему PVE.

Если объекту позже нужна собственная management identity, её desired state задаётся manifest `management.ssh_identity: true` и применяется штатным PVE-side handler.

## 23. Состояние реализации

Этот документ — **действующая спецификация требуемого поведения**, а не утверждение, что весь runtime уже написан.

Schema v7, resolver и рабочие manifests уже поддерживают management desired-state fields. Наличие конкретных runtime-компонентов `deploy-guest`, `sync-management-keys`, management handlers и Bootstrap handlers отслеживается только в:

[`29-implementation-status.md`](29-implementation-status.md)

Расширенные CI-проверки runtime key/deploy contract добавляются вместе с реальными runtime-скриптами, а не заранее против заглушек.

## 24. Главный принцип

> `deploy-guest` управляет одной описанной в Git VM/LXC. Новая Debian-машина получает текущий management public-key set и tag `management-ssh`; host key новой машины запоминается один раз и затем строго проверяется. `management.ssh_identity` создаёт private key только внутри владельца и регистрирует на PVE лишь `<VMID>.pub`; массовый sync остаётся отдельной командой. `management.project_repo_read` управляет только фиксированным read-only доступом к `zsergeyru/proxmox` и умеет как обеспечить, так и безопасно удалить только свои артефакты.