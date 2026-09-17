# Файловая структура Public Bootstrap / PVE Configuration

**Тип:** Справочник  
**Статус:** Действующий  
**Основной источник:** Да — для путей, файлов состояния, учётных данных, владельцев и рабочей структуры на стороне PVE-хоста.

Парная инструкция: [`20-pve-initialization.md`](20-pve-initialization.md). Точный контракт management SSH keys: [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

Основной принцип:

```text
Public Bootstrap
→ временная рабочая область только для первоначального доступа к закрытому репозиторию
→ /var/lib/proxmox-bootstrap удаляется после успешного первого запуска

PVE Configuration
→ создаёт и обслуживает постоянную инфраструктурную структуру
→ состояние хранится в /var/lib/proxmox-deployer/state
→ создаёт identity deployer и канонический каталог открытых management keys

оба компонента
→ используют /run/lock/proxmox-orchestration.lock

граница доверия
→ root владеет основной копией исходного кода и мастер-копией общего Git read-key
→ pvedeploy владеет изменяемой рабочей областью deploy-контура, включая public-key registry
→ pvedeploy не имеет постоянного доступа к root-only Git read-key
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

`pvedeploy` может читать исходный код, но не может изменять `repo`, `.git` или root-only мастер-копию Git read-key. Отдельного `scripts/pve/create-template.sh` нет. `deploy-guest.py` появится после реализации средства развёртывания.

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

Root-controlled конфигурация и секреты:

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

Постоянное изменяемое состояние deploy-контура:

```text
/var/lib/proxmox-deployer/
├── repo/
├── state/
├── cache/
└── public-keys/
    ├── deployer.pub
    ├── <VMID>.pub
    └── management-authorized-keys
```

Назначение:

```text
config.yaml
→ постоянные параметры средства развёртывания на PVE-хосте

ssh/github_proxmox_repo_ed25519
→ мастер-копия общего GitHub Deploy Key только для чтения zsergeyru/proxmox
→ доступна постоянно только root
→ используется самим PVE для обновления доверенной копии проекта
→ может временно передаваться конкретному запуску deploy-guest через отдельный FD,
  если guest явно требует management.project_repo_read: true

ssh/pve_guest_ed25519
→ закрытый SSH-ключ deployer для управляемых VM/LXC
→ этим же ключом PVE Configuration проверяет реальный root SSH на проверочном клоне

/var/lib/proxmox-deployer/public-keys/
→ канонический каталог только открытых административных SSH-ключей
→ deployer.pub соответствует /etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
→ <VMID>.pub появляется для гостя с management.ssh_identity: true
→ имя гостя в имени файла не используется
→ management-authorized-keys детерминированно собирается из зарегистрированных *.pub
→ каталог изменяется pvedeploy напрямую без отдельного привилегированного helper

ssh/config + known_hosts
→ принадлежащие root настройки SSH для доступа к закрытому Git-репозиторию

secrets/host-deploy.token
→ учётные данные deployer@pve!host-deploy

secrets/ai-agent-infra.token
→ учётные данные ai-agent@pve!infra
```

`public-keys/` не является secret store. В нём запрещены private keys, API tokens и Git credentials. Жизненный цикл каталога и его распространение задаёт [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

Технический PVE tag `management-ssh` не является файлом этого каталога и не хранится в manifest. Он находится в конфигурации конкретного PVE-объекта и служит областью обнаружения для `sync-management-keys`.

Общий Git read-key не является универсальным secret store и не открывает `pvedeploy` доступ к другим root-only credentials. `guest.yaml` не задаёт путь этого файла и не может запросить произвольный secret.

Основная конфигурация SSH включает строгую проверку ключа сервера, `BatchMode yes`, ограниченный `ConnectTimeout` и параметры контроля живого соединения.

Текущий PVE-хост вводится как новая машина без старого management-контура. Поэтому отдельная миграция прежнего deployer SSH key сейчас не нужна: если пары `pve_guest_ed25519` ещё нет, PVE Configuration создаёт её по текущей схеме; после первого создания ключ больше не заменяется автоматически.

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
/var/lib/proxmox-deployer/public-keys      pvedeploy:pvedeploy 0750
public-keys/*.pub                          pvedeploy:pvedeploy 0644
public-keys/management-authorized-keys     pvedeploy:pvedeploy 0644
/var/lib/pvedeploy                         pvedeploy:pvedeploy
/var/lib/pvedeploy/.ssh                    pvedeploy:pvedeploy 0700
/var/lib/pvedeploy/.ssh/known_hosts        pvedeploy:pvedeploy 0600
/var/log/proxmox-deployer                  root:pvedeploy 0750
/var/log/proxmox-deployer/audit            pvedeploy:pvedeploy 0750
```

`pvedeploy` может изменять только собственное runtime-состояние deploy-контура, включая public-key registry и базу SSH host keys управляемых гостей. Это не даёт ему права записи в доверенную root-owned копию проекта и не открывает root-only Git credential или AI token.

Отдельный привилегированный helper для добавления/удаления management public keys не требуется. Запись в registry всё равно выполняется только штатной логикой с полной проверкой формата, fingerprint, имени `<VMID>.pub`, отсутствия дубликатов и атомарной заменой файлов.

Требования к `pvedeploy`:

```text
system UID < 1000
primary group = pvedeploy
home = /var/lib/pvedeploy
shell = /bin/bash
```

`deploy-guest`, запущенный с ограниченными правами, сможет читать манифесты и исходный код и использовать свои штатные учётные данные, но не сможет изменить код или метаданные Git, которые затем исполняет `root`.

Для `management.project_repo_read` действует отдельное правило временной передачи секрета:

```text
root-wrapper
→ сам проверяет effective state текущего гостя
→ если management.project_repo_read=false/отсутствует: ключ не открывается
→ если management.project_repo_read=true: root открывает только фиксированный github_proxmox_repo_ed25519
→ передаёт значение дочернему deploy-процессу через выделенный file descriptor
→ значение не помещается в argv/environment и не журналируется
→ после завершения процесса FD закрывается
```

Это не даёт `pvedeploy` постоянного права чтения root-only файла и не создаёт интерфейс «запросить secret по имени».

Секреты создаются с безопасным `umask`, не попадают в Git и не выводятся в обычные журналы. Новый секрет API-токена записывается атомарно; обычная ошибка или прерывание откатывает только новый токен, созданный текущим запуском и ещё не зафиксированный как готовый.

### 5.1. SSH host key управляемых гостевых систем

`known_hosts` для GitHub и `known_hosts` гостевых систем — разные файлы с разными владельцами.

Root-only файл:

```text
/etc/proxmox-deployer/ssh/known_hosts
```

используется для доступа PVE к GitHub. Гость, получивший локальную копию общего Git read-key, должен иметь собственную проверяемую запись host key GitHub; root-only `known_hosts` PVE гостю не копируется как скрытое состояние.

Постоянная база SSH host key управляемых VM/LXC:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

принадлежит `pvedeploy` и используется `deploy-guest` и `sync-management-keys` для проверки SSH-ключей серверов гостевых систем.

Для только что созданного нового гостя в доверенной домашней сети допускается первое запоминание ключа сервера для вычисленного ожидаемого адреса. Сразу после сохранения все дальнейшие подключения выполняются со строгой сверкой этой записи.

Для уже известного гостя неожиданная смена SSH host key означает остановку автоматической работы. Старый ключ не удаляется автоматически ради продолжения.

Подробные правила определяет [`31-deploy-guest.md`](31-deploy-guest.md).

Потеря этого файла не является потерей закрытых учётных данных, но уничтожает сохранённую базу доверия к SSH host key существующих гостей. Поэтому существующие объекты после такой потери не должны молча приниматься заново без отдельного подтверждённого сценария восстановления.

## 6. Изменяемая рабочая область

```text
/var/lib/proxmox-deployer/
├── repo/          доверенная root копия, неизменяемая для pvedeploy
├── state/         состояние настройки и проверки шаблона
├── cache/         изменяемый кэш pvedeploy
└── public-keys/   изменяемый канонический registry открытых management keys
```

Закрытые SSH-ключи и секреты токенов в `state/`, `cache/` и `public-keys/` не хранятся. Исключение по всему дереву `/var/lib/proxmox-deployer` — только root-owned доверенная копия Git в `repo/`; её содержимое определяется самим репозиторием и не является secret store.

Домашний каталог `pvedeploy` содержит пользовательское служебное состояние, которое должно быть изменяемым самим `pvedeploy`, в частности постоянную базу SSH host key гостевых систем.

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

`deploy-guest` и `sync-management-keys` пишут аудит своих запусков в `audit/` согласно профильным спецификациям.

Правила: постоянный журнал без ANSI-кодов, без секретов токенов и закрытых ключей, с операциями, предупреждениями, fingerprints public keys и ревизией исходного кода, но без полного дампа переменных окружения.

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
- мастер-копию общего GitHub Deploy Key только для чтения;
- пару SSH-ключей deployer для гостевых систем;
- конфигурацию SSH;
- root-only `known_hosts` для GitHub.

Канонический каталог открытых management keys:

```text
/var/lib/proxmox-deployer/public-keys/
```

секретом не является, но его полезно сохранять как инфраструктурное состояние. Он также может быть восстановлен из проверенных `.pub` владельцев соответствующих identities. Дополнительные ключи именуются только `<VMID>.pub`.

Отдельно следует сохранять либо иметь документированный сценарий восстановления базы доверия:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Она не содержит секрета, но используется для обнаружения неожиданной смены SSH host key существующей гостевой системы.

PVE tag `management-ssh` также не является секретом, но входит в runtime-состояние management-контура и после аварийного восстановления должен сверяться с ожидаемыми Debian-гостями до запуска массовой синхронизации.

Локальная копия на том же SSD не считается полноценной копией для аварийного восстановления.

## 11. Стабильные команды

```text
/usr/local/sbin/
├── pve-configuration-status
├── deploy-guest
└── sync-management-keys
```

`pve-configuration-status` читает `state.json`.

`deploy-guest` после реализации будет небольшой командой-обёрткой над исходным кодом из основной закрытой копии репозитория. Его рабочая среда не получает права записи в эту копию и не получает постоянного доступа к root-only Git read-key.

`sync-management-keys` — отдельная команда массовой синхронизации только открытых management SSH keys по объектам с PVE tag `management-ssh`. Она не создаёт VM/LXC и не заменяет `deploy-guest`. Точный контракт: [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

Для проверки шаблона отдельная постоянная команда не устанавливается: явный запуск выполняется через `bootstrap-pve.sh --smoke-test-template` либо напрямую через `configure-pve.sh --smoke-test-template`.

## 12. Итоговое дерево

```text
/
├── etc/proxmox-deployer/
│   ├── config.yaml
│   ├── ssh/
│   │   ├── github_proxmox_repo_ed25519      # master Git READ credential, root-only
│   │   ├── github_proxmox_repo_ed25519.pub
│   │   ├── pve_guest_ed25519                # deployer private key
│   │   ├── pve_guest_ed25519.pub
│   │   ├── config
│   │   └── known_hosts
│   └── secrets/
├── run/lock/
│   └── proxmox-orchestration.lock
├── var/lib/proxmox-deployer/       root:pvedeploy 0750
│   ├── repo/                       root-owned
│   ├── state/                      root:pvedeploy
│   │   └── template-smoke.json
│   ├── cache/                      pvedeploy:pvedeploy
│   └── public-keys/                pvedeploy:pvedeploy 0750
│       ├── deployer.pub
│       ├── <VMID>.pub
│       └── management-authorized-keys
├── var/lib/pvedeploy/
│   └── .ssh/                       pvedeploy:pvedeploy 0700
│       └── known_hosts             pvedeploy:pvedeploy 0600
├── var/log/proxmox-deployer/       root:pvedeploy
│   └── audit/                      pvedeploy:pvedeploy
├── var/backups/proxmox-configuration/
├── var/backups/proxmox-secrets/
└── usr/local/sbin/
    ├── pve-configuration-status
    ├── deploy-guest
    └── sync-management-keys
```

Внутри участвующего Debian-гостя стандартные management-пути:

```text
/etc/proxmox-guest/
├── ssh/
│   ├── management_ed25519          # только если management.ssh_identity: true
│   └── management_ed25519.pub
├── public-keys/
│   ├── deployer.pub
│   ├── <VMID>.pub
│   └── management-authorized-keys
└── credentials/
    └── github-proxmox-read          # только если management.project_repo_read: true
```

Само участие гостя в массовой синхронизации обозначается в PVE tag `management-ssh`; этот marker не является файлом `/etc/proxmox-guest`.

Главный принцип:

> Public Bootstrap использует отдельную временную область только для первоначального доступа. Основная копия исходного кода и мастер-копия общего Git read-key принадлежат `root`; `pvedeploy` не получает постоянного доступа к этому секрету. Канонический набор открытых management SSH keys является изменяемым состоянием `pvedeploy` и хранится в `/var/lib/proxmox-deployer/public-keys/`; отдельный привилегированный helper для него не нужен. Новому гостю один раз запоминается SSH host key ожидаемого адреса, после чего неожиданная смена ключа блокирует автоматическую работу. Дополнительные management identities привязаны к VMID через `<VMID>.pub`, а не к имени гостя.