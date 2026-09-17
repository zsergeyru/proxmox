# Инфраструктурные SSH-ключи управляемых гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для каталога инфраструктурных открытых SSH-ключей, `management.ssh_identity`, генерации собственной пары гостя, PVE tag `management-ssh` и синхронизации открытых ключей между управляемыми Debian VM/LXC.

Общие правила безопасности задаёт [`23-security.md`](23-security.md). Поведение `deploy-guest` задаёт [`31-deploy-guest.md`](31-deploy-guest.md), а формат `guest.yaml` — [`30-guest-manifest.md`](30-guest-manifest.md). Этот документ является основным источником именно для жизненного цикла **административных SSH-ключей гостевых систем**.

## 1. Цель

У проекта есть несколько независимых субъектов, которым нужен прямой административный SSH-доступ к управляемым Debian VM/LXC:

```text
средство развёртывания на PVE
AI Control
Ansible
будущие управляющие гостевые системы
```

У каждого субъекта должна быть собственная пара SSH-ключей. Закрытый ключ остаётся только на той системе, которой он принадлежит. Открытые части собираются в один канонический каталог на PVE и распространяются по управляемым гостям.

Главное правило:

> Любая новая управляемая Debian VM/LXC должна с момента создания получить актуальный набор инфраструктурных открытых SSH-ключей, чтобы уже существующие управляющие контуры могли войти в неё. Новый управляющий гость может создать собственную пару; `deploy-guest` забирает только её открытую часть, после чего отдельная синхронизация распространяет обновлённый каталог по уже существующим гостям.

Git в распространении этих административных ключей не участвует. Для этого не требуется Git write credential, SSH-доступ AI к PVE или отдельный сетевой сервис регистрации ключей.

## 2. Разделение закрытых и открытых ключей

Закрытый административный ключ всегда находится там, откуда инициируется SSH-доступ:

```text
ключ deployer
→ закрытая часть остаётся на PVE

ключ управляющего гостя
→ закрытая часть создаётся и остаётся внутри этого гостя

другой будущий управляющий контур
→ закрытая часть остаётся внутри соответствующей системы
```

Закрытые ключи:

- не копируются в общий каталог открытых ключей;
- не передаются между управляющими гостями;
- не помещаются в Git;
- не выводятся в журналы;
- не ротируются автоматически при обычном повторном запуске.

Открытые части не являются секретами и могут безопасно распространяться между управляемыми гостями.

## 3. Канонический каталог на PVE

Канонический каталог открытых административных ключей находится только у deploy-контура на PVE:

```text
/etc/proxmox-deployer/public-keys/
├── deployer.pub
├── <VMID>-<name>.pub
├── <VMID>-<name>.pub
└── management-authorized-keys
```

Например после появления AI Control и Ansible-контроллера каталог может выглядеть так:

```text
/etc/proxmox-deployer/public-keys/
├── deployer.pub
├── 301-ai-control.pub
├── 311-dev-services.pub
└── management-authorized-keys
```

`management-authorized-keys` — детерминированно собранный файл из всех зарегистрированных `*.pub`. Он является производным файлом и может быть пересобран из отдельных открытых ключей.

`deployer.pub` соответствует существующей паре средства развёртывания:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Имя `pve_guest_ed25519` сохраняется как существующее имя файла. По смыслу это SSH-идентичность **deployer для входа в гостевые ОС**, а не «ключ самого Proxmox».

PVE Configuration обязана подготовить сам каталог и зарегистрировать в нём `deployer.pub`, но **не знает заранее**, какие будущие VM/LXC будут AI-, Ansible- или иными управляющими системами. Состав дополнительных ключей определяется только при развёртывании конкретных гостей.

## 4. Единый manifest-раздел `management`

Все запросы, относящиеся к административному управлению конкретным гостем, группируются в одном разделе:

```yaml
management:
  ssh:
    user: root
    port: 22
  ssh_identity: true
  project_repo_read: true
```

Смысл полей разделён:

```text
management.ssh
→ как проект входит в этот guest по административному SSH

management.ssh_identity: true
→ самому гостю нужна собственная management SSH identity

management.project_repo_read: true
→ гостю нужен фиксированный read-only credential проекта
```

Этот документ определяет `management.ssh_identity`. `management.project_repo_read` описан в [`30-guest-manifest.md`](30-guest-manifest.md), [`31-deploy-guest.md`](31-deploy-guest.md) и [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md); Git credential не является частью SSH public-key registry.

Для гостя, которому нужна собственная административная SSH-пара, целевой manifest-интерфейс:

```yaml
management:
  ssh_identity: true
```

Смысл поля ровно один:

> После появления административного SSH-доступа deployer должен обеспечить собственную management SSH-пару внутри этого гостя и зарегистрировать её открытую часть в каноническом каталоге PVE.

Поле не задаёт:

```text
имя private key
путь private key
имя public key на PVE
другой guest
роль AI/Ansible/backup
VMID получателя
произвольный secret
```

Все пути и правила фиксированы реализацией. Поэтому `deploy-guest` не знает, является ли гость AI Control, Ansible или будущим контроллером. Он видит только `management.ssh_identity: true`.

`management.ssh_identity` задаётся только в конкретном `guest.yaml` и не наследуется из `defaults.yaml` или profile. То же правило действует для `management.project_repo_read`. При этом базовый `management.ssh.user/port` может приходить из общих defaults.

На момент принятия этой спецификации `management.ssh_identity` и `management.project_repo_read` ещё не реализованы в действующей schema/resolver. До реализации schemas, resolver, validator и tests их не следует добавлять в рабочие `guest.yaml`.

## 5. Собственная пара внутри гостя

Для `management.ssh_identity: true` используется стандартное место внутри Debian-гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Требования:

```text
каталог /etc/proxmox-guest/ssh      root:root 0700
private key                         root:root 0600
public key                          root:root 0644
алгоритм                            ed25519
```

Последовательность `deploy-guest`:

```text
создать/привести VM/LXC к требуемому состоянию
→ обеспечить вход root по ключу deployer
→ если management.ssh_identity != true: не создавать собственную пару
→ если management.ssh_identity == true:
     проверить стандартную пару внутри гостя
     если пары нет — создать её внутри гостя
     private key оставить внутри гостя
     прочитать наружу только .pub
     проверить формат и fingerprint
     зарегистрировать .pub на PVE как <VMID>-<name>.pub
→ выполнить синхронизацию открытых ключей
```

Обычный повторный запуск не создаёт новую пару, если корректная пара уже существует.

Недопустимы скрытая ротация и автоматическое восстановление новой пары при неоднозначном состоянии. Например:

```text
private есть, public отсутствует
public есть, private отсутствует
private/public не соответствуют друг другу
зарегистрированный fingerprint отличается от фактического без явной процедуры ротации
```

→ `STOP`, а не молчаливое создание новой identity.

Если пара внутри гостя корректна, но её открытая часть отсутствует в каталоге PVE, deployer может повторно получить `.pub` из гостя и зарегистрировать её после проверки fingerprint.

## 6. `sync-management-keys`

Распространение открытых ключей выполняет **отдельный PVE-скрипт**, а не `deploy-guest` как основную функцию:

```bash
sync-management-keys
```

Назначение команды:

```text
/etc/proxmox-deployer/public-keys/
        ↓
проверить все зарегистрированные public keys
        ↓
собрать management-authorized-keys
        ↓
найти PVE VM/LXC с tag management-ssh
        ↓
проверить принадлежность объекта management SSH-контуру
        ↓
по SSH от имени root с ключом deployer
        ↓
синхронизировать каталог public keys
        ↓
синхронизировать управляемый набор root authorized_keys
        ↓
проверить результат
```

Команда не создаёт VM/LXC, не меняет их ресурсы и сеть и не выполняет прикладной provisioning.

Она может запускаться оператором вручную в любой момент. Кроме того, `deploy-guest` должен вызвать её после успешной регистрации **нового или изменённого** management public key. Обычный deploy без изменения каталога ключей синхронизацию не запускает.

Если автоматический запуск синхронизации после регистрации ключа завершился ошибкой, новый private key не удаляется и разрушительный rollback не выполняется. Запуск считается незавершённым до успешного повторного `sync-management-keys`.

## 7. Какие гости участвуют в синхронизации

Для участия используется отдельный **технический PVE tag**:

```text
management-ssh
```

Это runtime-маркер Proxmox, а не ещё одно поле manifest.

Правила:

- `deploy-guest` устанавливает `management-ssh`, если effective state развёртываемого Debian-гостя содержит `management.ssh`;
- AI Control или другой управляющий guest, создающий Debian VM/LXC напрямую через PVE API и передающий ей `management-authorized-keys`, также устанавливает `management-ssh`;
- `sync-management-keys` рассматривает только VM/LXC с этим tag;
- наличие tag является необходимым, но не достаточным условием записи: тип объекта, ожидаемый Debian management SSH-контракт, SSH host key и возможность безопасной аутентификации всё равно проверяются fail-closed;
- отсутствие tag означает, что `sync-management-keys` объект не трогает.

Тег `management-ssh` не означает владение lifecycle со стороны `deploy-guest`. Для этого существует отдельный tag `proxmox-deployer`. Поэтому AI-created гостевая система может иметь `management-ssh`, не имея `proxmox-deployer`.

Новая машина, созданная `deploy-guest`, изначально получает как минимум `deployer.pub`, весь текущий `management-authorized-keys` и tag `management-ssh`.

Новая машина, создаваемая AI/другим управляющим гостем, должна получить **весь актуальный локальный `management-authorized-keys`**, включая `deployer.pub`, и тот же tag `management-ssh`.

Шаблоны, HAOS и другие системы, не участвующие в Debian management SSH-контракте, tag `management-ssh` не получают.

## 8. Локальная копия каталога в гостях

У каждого участвующего Debian-гостя хранится read-only по смыслу локальная копия текущего публичного каталога:

```text
/etc/proxmox-guest/public-keys/
├── deployer.pub
├── <VMID>-<name>.pub
├── ...
└── management-authorized-keys
```

Закрытых ключей в этом каталоге нет.

Эта копия нужна не только для аудита. Управляющий гость, который сам создаёт новую VM/LXC через PVE API, использует:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

как исходный набор `sshkeys` для новой машины.

Поэтому агенту не нужен SSH-доступ к PVE, чтобы узнать ключ deployer, Ansible или другого уже зарегистрированного управляющего контура. Он получает актуальный список обычной синхронизацией **от PVE к гостю**.

## 9. Создание новой машины управляющим гостем

Если AI Control или другой управляющий гость создаёт VM/LXC напрямую через разрешённый PVE API-контур, правило одно:

```text
прочитать локальный management-authorized-keys
→ передать весь набор public keys новой VM/LXC
→ установить PVE tag management-ssh
→ создать/клонировать объект
→ запустить
→ проверить доступность согласно своему сценарию
```

Управляющий гость не регистрирует public keys на PVE и не получает право записи в `/etc/proxmox-deployer`.

Если самой новой машине затем требуется собственная identity, она должна быть в проекте описана как:

```yaml
management:
  ssh_identity: true
```

и пройти штатный `deploy-guest`, который создаст/проверит пару и зарегистрирует её `.pub`. В v1 не вводится удалённый API регистрации ключей со стороны AI.

## 10. Синхронизация `authorized_keys`

`sync-management-keys` не должен уничтожать неизвестные или личные SSH-ключи.

Он управляет только своим выделенным блоком в:

```text
/root/.ssh/authorized_keys
```

Например логически:

```text
# BEGIN PROXMOX-MANAGEMENT-KEYS
<содержимое management-authorized-keys>
# END PROXMOX-MANAGEMENT-KEYS
```

При повторной синхронизации заменяется только этот блок. Строки вне него сохраняются без изменений.

Это позволяет:

- добавить новый инфраструктурный public key во все управляемые гости;
- удалить отозванный ключ из канонического каталога и затем убрать его из управляемого блока всех гостей;
- не ломать личные, аварийные или иные ключи, которыми этот механизм не владеет.

`/root/.ssh` и `authorized_keys` должны иметь безопасные владельца и права. Изменение выполняется атомарно с проверкой итогового содержимого.

## 11. Валидация публичного каталога

До первой записи в гостевые системы `sync-management-keys` обязан проверить каталог целиком:

- каждый `*.pub` содержит ровно один допустимый OpenSSH public key;
- private key или PEM/OpenSSH private material в каталоге отсутствуют;
- fingerprints вычисляются успешно;
- один fingerprint не зарегистрирован под несколькими именами;
- `management-authorized-keys` собирается детерминированно;
- пустой или повреждённый каталог не приводит к массовому удалению доступа;
- `deployer.pub` присутствует и соответствует штатному `pve_guest_ed25519.pub`.

При любой ошибке проверка завершается до изменения первого гостя.

В журнал можно писать имя public key и fingerprint. Содержимое закрытых ключей никогда не журналируется.

## 12. Ротация и удаление

Обычный deploy и обычная синхронизация не ротируют private keys.

Ротация отдельной management identity — явная процедура:

```text
создать/подтвердить новую пару у владельца
→ зарегистрировать новую public часть на PVE
→ sync-management-keys
→ проверить доступ новым private key
→ только после этого удалить/отозвать старый public key
→ снова sync-management-keys
```

Если канонический `*.pub` удалён осознанно, следующая успешная синхронизация удаляет соответствующую строку **только из управляемого блока** `authorized_keys` и из локальной копии публичного каталога.

Удаление public key из каталога не удаляет private key внутри его владельца автоматически.

## 13. Что не относится к этой модели

Отдельно существуют:

```text
PVE API token deployer@pve!host-deploy
PVE API token ai-agent@pve!infra
общий GitHub Deploy Key READ ONLY
SSH host keys серверов
личные SSH-ключи человека
```

Общий Git read-only credential:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

не добавляется в `management-authorized-keys` и не используется для входа `root@guest`.

`management.ssh_identity: true` никак не выдаёт Git credential и не изменяет PVE ACL. `management.project_repo_read: true` использует другой private credential и другой lifecycle, несмотря на общее логическое пространство `management` в manifest.

## 14. Граница PVE Configuration

PVE Configuration знает только общую инфраструктуру deploy-контура:

```text
создать/проверить SSH identity deployer
подготовить /etc/proxmox-deployer/public-keys
зарегистрировать deployer.pub
установить/подготовить sync-management-keys
```

PVE Configuration **не содержит списка**:

```text
301
311
AI Control
Ansible
backup-controller
других будущих управляющих VM/LXC
```

Поэтому изменение VMID, добавление нового управляющего сервиса или перестройка структуры гостевых систем не требует изменения PVE Configuration только ради management SSH keys.

## 15. Разделение ответственности

Итоговый контракт:

```text
PVE Configuration
→ создаёт механизм и identity deployer

guest.yaml / effective state
→ management.ssh означает административный SSH-контур гостя
→ management.ssh_identity: true означает «этому гостю нужна собственная management identity»
→ management.project_repo_read: true означает «этому гостю нужен project Git READ credential»

deploy-guest
→ для management.ssh ставит PVE tag management-ssh
→ при management.ssh_identity создаёт/проверяет пару внутри гостя
→ private оставляет внутри гостя
→ забирает только .pub
→ регистрирует .pub в каноническом каталоге
→ после изменения каталога запускает sync-management-keys

sync-management-keys
→ рассматривает только PVE-объекты с tag management-ssh
→ fail-closed проверяет объект и SSH trust
→ обновляет локальную копию public keys и управляемый блок authorized_keys

AI/другой создатель Debian VM/LXC
→ не пишет на PVE filesystem
→ читает локальную копию management-authorized-keys
→ передаёт весь набор новой VM/LXC
→ ставит новой машине tag management-ssh
```

Это намеренно узкая модель. Универсальный secret broker, каталог закрытых ключей, VMID allowlist для публичных ключей, Git write-доступ ради регистрации ключей и SSH-доступ AI к PVE в неё не входят.

## 16. Состояние реализации

На момент принятия документа это **целевой контракт**, который ещё требует реализации:

1. `management.ssh_identity` и `management.project_repo_read` в source/effective schemas и resolver;
2. запрета наследования этих двух guest-local запросов из defaults/profile;
3. проверок validator/tests;
4. каталога `/etc/proxmox-deployer/public-keys/` в PVE Configuration;
5. обработки `management.ssh_identity` в `deploy-guest`;
6. автоматического PVE tag `management-ssh` для deployer-created Debian-гостей;
7. требования ставить `management-ssh` при создании Debian-гостей AI/другим управляющим контуром;
8. отдельного `sync-management-keys`, использующего tag как область обнаружения;
9. стандартных каталогов `/etc/proxmox-guest/ssh/` и `/etc/proxmox-guest/public-keys/`;
10. использования локального `management-authorized-keys` AI-контуром при создании новых Debian VM/LXC.

До реализации этих пунктов документ определяет согласованную архитектуру, но не должен создавать ложное впечатление, что действующие schema v6 и текущие scripts уже поддерживают этот интерфейс.