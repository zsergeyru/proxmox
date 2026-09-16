# Файловая структура Public Bootstrap / PVE Configuration

**Тип:** Справочник  
**Статус:** Действующий  
**Основной источник:** Да — для путей, файлов состояния, учётных данных, владельцев и рабочей структуры на стороне PVE-хоста.

Парная инструкция: [`20-pve-initialization.md`](20-pve-initialization.md).

Основной принцип:

```text
Public Bootstrap
→ временная рабочая область только для первоначального доступа к закрытому репозиторию
→ /var/lib/proxmox-bootstrap удаляется после успешного первого запуска

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ состояние хранится в /var/lib/proxmox-deployer/state

оба компонента
→ используют /run/lock/proxmox-orchestration.lock

граница доверия
→ root владеет основной копией исходного кода и Git-ключом
→ pvedeploy владеет только своей изменяемой рабочей областью развёртывания
```

## 1. Public Bootstrap

Публичный репозиторий:

```text
zsergeyru/proxmox-bootstrap
└── bootstrap-pve.sh
```

До первого успешного перехода к PVE Configuration используется временная область:

```text
/var/lib/proxmox-bootstrap/
├── github_proxmox_repo_ed25519
├── github_proxmox_repo_ed25519.pub
├── known_hosts
├── ssh_config
└── private-repo/
```

Каталог создаётся как `root:root 0700`.

`private-repo/` — одноразовая рабочая копия репозитория. При продолжении прерванного запуска её можно привести к свежему `FETCH_HEAD` через `reset --hard` и `git clean -ffdx`, включая игнорируемые кэши и артефакты сборки. Это правило относится только к временной области.

После успешной PVE Configuration Public Bootstrap удаляет `/var/lib/proxmox-bootstrap` и записывает:

```text
/var/lib/proxmox-deployer/state/bootstrap-complete
```

## 2. Исходный код PVE Configuration

Основная закрытая точка входа:

```text
zsergeyru/proxmox/scripts/pve/setup/configure-pve.sh
```

Структура:

```text
scripts/pve/setup/
├── configure-pve.sh
├── render-template-cloud-init.py
├── tests/
│   ├── test-template-contract.sh
│   ├── test-template-guest-exec.sh
│   ├── test-template-smoke.sh
│   ├── test-token-rollback.sh
│   └── test-source-trust.sh
└── lib/
    ├── 00-common.sh
    ├── 10-preflight.sh
    ├── 20-system.sh
    ├── 30-storage.sh
    ├── 40-runtime.sh
    ├── 50-access.sh
    ├── 60-template-contract.sh
    ├── 61-template-source.sh
    ├── 62-template-build.sh
    ├── 63-template-smoke.sh
    └── 70-tooling.sh
```

Постоянная основная копия репозитория:

```text
/var/lib/proxmox-deployer/repo/
```

Это доверенная `root` рабочая копия закрытого репозитория:

```text
владелец дерева repo: root:root
запись для group/other: запрещена
родитель /var/lib/proxmox-deployer: root:pvedeploy 0750
```

Родитель намеренно недоступен `pvedeploy` на запись: иначе ограниченный пользователь мог бы удалить или переименовать даже каталог `repo/`, принадлежащий `root`, и заменить его целиком.

Перед автоматическим обновлением Git проверяется полное отсутствие локальных изменений: отслеживаемых, подготовленных к коммиту, неотслеживаемых и игнорируемых. Такие изменения не стираются молча. В отличие от временной копии, `git clean -ffdx` здесь не применяется.

`pvedeploy` может читать исходный код, но не может изменять `repo`, `.git` или Git-ключ. Отдельного `scripts/pve/create-template.sh` нет. `deploy-guest.py` появится после реализации средства развёртывания.

## 3. Общая блокировка запуска

```text
/run/lock/proxmox-orchestration.lock
```

Public Bootstrap открывает блокировку на `fd 9` и передаёт этот дескриптор PVE Configuration. При прямом запуске `configure-pve.sh` берёт ту же блокировку самостоятельно.

Блокировка защищает одновременно:

- обновление основной копии закрытого репозитория;
- изменение постоянной рабочей структуры;
- конфигурацию PVE-хоста;
- сборку шаблона `9000`;
- проверочную VM `9099`;
- переходы файлов состояния и итоговых отметок.

Блокировка дополняет, но не заменяет файловую границу доверия: `pvedeploy` физически не имеет права записи в основной исходный код.

## 4. Постоянная конфигурация средства развёртывания

```text
/etc/proxmox-deployer/
├── config.yaml
├── ssh/
│   ├── github_proxmox_repo_ed25519
│   ├── github_proxmox_repo_ed25519.pub
│   ├── pve_guest_ed25519
│   ├── pve_guest_ed25519.pub
│   ├── config
│   └── known_hosts
└── secrets/
    ├── host-deploy.token
    └── ai-agent-infra.token
```

Назначение:

```text
config.yaml
→ постоянные параметры средства развёртывания на PVE-хосте

ssh/github_proxmox_repo_ed25519
→ доступный только root ключ GitHub Deploy Key только для чтения zsergeyru/proxmox

ssh/pve_guest_ed25519
→ SSH-ключ pvedeploy для управляемых VM/LXC
→ этим же ключом PVE Configuration проверяет реальный root SSH на проверочном клоне

ssh/config + known_hosts
→ принадлежащие root настройки SSH для доступа к закрытому Git-репозиторию

secrets/host-deploy.token
→ учётные данные deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ учётные данные ai-agent@pve!infra
```

Основная конфигурация SSH включает строгую проверку ключа сервера, `BatchMode yes`, ограниченный `ConnectTimeout` и параметры контроля живого соединения.

## 5. Владельцы и права

Ключевое разделение:

```text
/etc/proxmox-deployer/                     root:root
/etc/proxmox-deployer/ssh/                root:pvedeploy 0750
github_proxmox_repo_ed25519                root:root 0600
github_proxmox_repo_ed25519.pub            root:root 0644
ssh/config                                 root:root 0600
ssh/known_hosts                            root:root 0644
pve_guest_ed25519                          pvedeploy:pvedeploy 0600
pve_guest_ed25519.pub                      pvedeploy:pvedeploy 0644
/etc/proxmox-deployer/secrets/             root:pvedeploy 0710
host-deploy.token                          root:pvedeploy 0640
ai-agent-infra.token                       root:root 0600
/var/lib/proxmox-deployer                  root:pvedeploy 0750
/var/lib/proxmox-deployer/repo             root-owned, group/other non-writable
/var/lib/proxmox-deployer/state            root:pvedeploy 0750
/var/lib/proxmox-deployer/cache            pvedeploy:pvedeploy 0750
/var/lib/pvedeploy                         pvedeploy:pvedeploy
/var/lib/pvedeploy/.ssh                    pvedeploy:pvedeploy 0700
/var/lib/pvedeploy/.ssh/known_hosts        pvedeploy:pvedeploy 0600
/var/log/proxmox-deployer                  root:pvedeploy 0750
/var/log/proxmox-deployer/audit            pvedeploy:pvedeploy 0750
```

Требования к `pvedeploy`:

```text
system UID < 1000
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

Будущий `deploy-guest`, запущенный с ограниченными правами, сможет читать манифесты и исходный код и использовать свои учётные данные, но не сможет изменить код или метаданные Git, которые затем исполняет `root`.

Секреты создаются с безопасным `umask`, не попадают в Git и не выводятся в обычные журналы. Новый секрет API-токена записывается атомарно; обычная ошибка или прерывание откатывает только новый токен, созданный текущим запуском и ещё не зафиксированный как готовый.

### 5.1. SSH host key управляемых гостевых систем

`known_hosts` для GitHub и `known_hosts` гостевых систем — разные файлы с разными владельцами.

Root-only файл:

```text
/etc/proxmox-deployer/ssh/known_hosts
```

используется только для доступа основной копии Git к GitHub.

Постоянная база SSH host key управляемых VM/LXC:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

принадлежит `pvedeploy` и используется `deploy-guest` для строгой проверки SSH-ключей серверов гостевых систем.

Правила первичного принятия и реакции на неожиданную смену ключа определяет [`31-deploy-guest.md`](31-deploy-guest.md).

Потеря этого файла не является потерей закрытых учётных данных, но уничтожает сохранённую базу доверия к SSH host key существующих гостей. Поэтому существующие объекты после такой потери не должны молча приниматься заново без отдельного подтверждённого сценария восстановления.

## 6. Изменяемая рабочая область

```text
/var/lib/proxmox-deployer/
├── repo/    доверенная root копия, неизменяемая для pvedeploy
├── state/   состояние настройки и проверки шаблона
└── cache/   изменяемый кэш pvedeploy
```

Закрытые SSH-ключи и секреты токенов здесь не хранятся.

Домашний каталог `pvedeploy` содержит только пользовательское служебное состояние, которое должно быть изменяемым самим `pvedeploy`, в частности постоянную базу SSH host key гостевых систем.

Кэш образов шаблона:

```text
/var/lib/vz/template/cache/debian13/
```

Временный фрагмент Cloud-Init для сборщика создаётся в `local:snippets` только на время сборки `9000`. Если существует незавершённая VM-сборщик, автоматическая очистка не выполняется.

Временный файл проверки SSH-ключа VM `9099`:

```text
/run/pve-template-smoke-known-hosts.XXXXXX
```

Он используется только для проверки SSH-ключа сервера временной VM до и после перезагрузки и удаляется после успеха либо при обычном `EXIT/INT/TERM`. При ошибке сама VM `9099` не удаляется.

## 7. Файлы состояния

```text
/var/lib/proxmox-deployer/state/
├── state.json
├── version
├── last-run.json
├── last-revision
├── template-smoke.json
└── bootstrap-complete
```

Назначение:

```text
state.json
→ текущее состояние PVE Configuration

version
→ PVE_CONFIGURATION_VERSION

last-run.json
→ время, результат и ревизия исходного кода без секретов

last-revision
→ ревизия основной постоянной копии закрытого репозитория

template-smoke.json
→ состояние pending/passed реальной проверки полного клона шаблона 9000
→ при passed хранит ревизию, machine-id, ядро, размер rootfs и fingerprint SSH-ключа проверочного клона

bootstrap-complete
→ первоначальный Public Bootstrap успешно завершён
```

Названия `state.json`, `last-run.json`, `template-smoke.json`, значения `pending/passed` и другие машинные поля оставлены без перевода, потому что это часть программного интерфейса.

`state.json`, `last-run.json`, `version` и `template-smoke.json` записываются через временный файл и атомарный `mv`.

`template-smoke.json=status=pending` — долговечная отметка незавершённой проверки: после прерывания следующий обычный запуск PVE Configuration обязан вернуться к проверке. `passed` записывается только после полного успешного цикла и удаления VMID `9099`.

Файлы состояния не являются единственным основанием считать систему исправной: каждый запуск заново проверяет PVE, учётные данные, состояние Git, владельцев исходного кода и фактическое состояние VMID `9000/9099`.

## 8. Журналы

```text
/var/log/proxmox-deployer/
├── configure-pve.log
└── audit/
```

Создание шаблона `9000` и проверка полного клона `9099` пишут в тот же `configure-pve.log`; отдельного контура журналирования для этих операций нет.

`deploy-guest` пишет отдельный журнал каждого запуска в `audit/` согласно [`31-deploy-guest.md`](31-deploy-guest.md).

Правила: постоянный журнал без ANSI-кодов, без секретов токенов и закрытых ключей, с операциями, предупреждениями и ревизией исходного кода, но без полного дампа переменных окружения.

## 9. Снимки конфигурации

```text
/var/backups/proxmox-configuration/YYYYMMDD-HHMMSS/
```

Это снимок конфигурации и диагностических данных PVE-хоста перед значимыми изменениями, а не резервная копия VM. Не следует бездумно архивировать весь `/etc/pve`, поскольку это `pmxcfs`.

## 10. Резервирование инфраструктурных секретов и состояния доверия

Защищённая локальная область:

```text
/var/backups/proxmox-secrets/
```

Внешняя политика резервного копирования должна включать как минимум:

- секреты API-токенов;
- основной GitHub Deploy Key;
- пару SSH-ключей PVE для гостевых систем;
- конфигурацию SSH;
- root-only `known_hosts` для GitHub.

Отдельно следует сохранять либо иметь документированный сценарий восстановления базы доверия:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Она не содержит секрета, но используется для обнаружения неожиданной смены SSH host key существующей гостевой системы.

Локальная копия на том же SSD не считается полноценной копией для аварийного восстановления.

## 11. Стабильные команды

```text
/usr/local/sbin/
├── pve-configuration-status
└── deploy-guest
```

`pve-configuration-status` читает `state.json`.

`deploy-guest` после реализации будет небольшой командой-обёрткой над исходным кодом из основной закрытой копии репозитория. Его рабочая среда не получает права записи в эту копию.

Для проверки шаблона отдельная постоянная команда не устанавливается: явный запуск выполняется через `bootstrap-pve.sh --smoke-test-template` либо напрямую через `configure-pve.sh --smoke-test-template`.

## 12. Итоговое дерево

```text
/
├── etc/proxmox-deployer/
│   ├── config.yaml
│   ├── ssh/
│   └── secrets/
├── run/lock/
│   └── proxmox-orchestration.lock
├── var/lib/proxmox-deployer/       root:pvedeploy 0750
│   ├── repo/                       root-owned
│   ├── state/                      root:pvedeploy
│   │   └── template-smoke.json
│   └── cache/                      pvedeploy:pvedeploy
├── var/lib/pvedeploy/
│   └── .ssh/                       pvedeploy:pvedeploy 0700
│       └── known_hosts             pvedeploy:pvedeploy 0600
├── var/log/proxmox-deployer/       root:pvedeploy
│   └── audit/                      pvedeploy:pvedeploy
├── var/backups/proxmox-configuration/
├── var/backups/proxmox-secrets/
└── usr/local/sbin/
    ├── pve-configuration-status
    └── deploy-guest
```

Главный принцип:

> Public Bootstrap использует отдельную временную область только для первоначального доступа. Основная копия исходного кода и Git-ключ принадлежат `root`; `pvedeploy` получает только необходимую изменяемую рабочую область. При сомнительном состоянии проверка шаблона или гостевой системы останавливается без разрушительной очистки.