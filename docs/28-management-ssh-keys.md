# Инфраструктурные SSH-ключи управляемых гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для `management.ssh_identity`, канонического каталога открытых management SSH keys, PVE tag `management-ssh` и контракта `sync-management-keys`.

Общие правила безопасности задаёт [`23-security.md`](23-security.md). Формат manifest — [`30-guest-manifest.md`](30-guest-manifest.md). Поведение `deploy-guest` — [`31-deploy-guest.md`](31-deploy-guest.md). Текущая степень реализации этого контракта отдельно фиксируется в [`29-implementation-status.md`](29-implementation-status.md).

## 1. Цель и главное правило

У проекта может быть несколько независимых субъектов административного SSH-доступа:

```text
PVE deployer
AI Control
Ansible controller
будущие управляющие гости
```

Каждый субъект использует собственную пару SSH-ключей. Private key всегда остаётся у владельца; наружу и между гостями распространяются только public keys.

Главное правило:

> Новая управляемая Debian VM/LXC с момента создания получает весь актуальный набор management public keys. Если новому управляющему гостю нужна собственная исходящая SSH identity, его private key создаётся и остаётся внутри него, а на PVE регистрируется только `.pub`.

Git не используется как транспорт для management SSH keys. Для регистрации не нужен Git write credential, SSH-доступ AI к PVE или отдельный сетевой сервис.

## 2. Разделение identities

Существуют независимые контуры:

```text
deployer SSH identity
→ private key остаётся на PVE

management identity конкретного гостя
→ private key остаётся внутри этого гостя

Project Git READ credential
→ отдельный общий read-only ключ для GitHub

PVE API tokens
→ отдельные API credentials
```

Один credential не должен использоваться вместо другого.

Private management keys:

- не попадают в Git;
- не копируются в общий public-key registry;
- не передаются другим управляющим гостям;
- не выводятся в журналы;
- не ротируются обычным повторным deploy.

## 3. Канонический public-key registry на PVE

Постоянное изменяемое состояние:

```text
/var/lib/proxmox-deployer/public-keys/
├── deployer.pub
├── <VMID>.pub
├── <VMID>.pub
└── management-authorized-keys
```

Пример:

```text
/var/lib/proxmox-deployer/public-keys/
├── deployer.pub
├── 301.pub
├── 311.pub
└── management-authorized-keys
```

Владелец каталога:

```text
pvedeploy:pvedeploy
```

Рекомендуемые права:

```text
public-keys/                    0750
*.pub                           0644
management-authorized-keys      0644
```

Каталог содержит **только открытые SSH-ключи** и производный aggregate. Private keys, API tokens и Git credentials здесь запрещены.

`deployer.pub` соответствует:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Дополнительный ключ именуется только по VMID:

```text
<VMID>.pub
```

Имя гостя в filename не используется. Переименование VM/LXC не меняет identity.

`management-authorized-keys` детерминированно собирается из зарегистрированных `*.pub` и является производным файлом.

Отдельный root-helper для записи registry не используется. `pvedeploy` изменяет публичный каталог напрямую с обязательной валидацией и атомарной записью. Root trust boundary остаётся у исполняемого кода и root-only секретов, а не у открытых ключей.

## 4. Manifest contract

В schema v7 действуют два individual-only поля внутри `management`:

```yaml
management:
  ssh:
    user: root
    port: 22
  ssh_identity: true
  project_repo_read: true
```

Этот документ определяет именно:

```yaml
management:
  ssh_identity: true
```

Поле означает:

> После появления проверенного административного SSH deployer должен обеспечить собственную management SSH-пару внутри этого гостя и зарегистрировать только её открытую часть на PVE.

Поле не задаёт:

```text
key name
private-key path
registry filename
роль AI/Ansible/backup
другой VMID
произвольный secret
```

`management.ssh_identity` задаётся только в конкретном `guest.yaml` и не наследуется из defaults/profile. Если поле отсутствует, effective state нормализует его в `false`.

`management.project_repo_read` также individual-only, но использует другой credential и другой lifecycle; его точный контракт находится в [`30-guest-manifest.md`](30-guest-manifest.md) и [`31-deploy-guest.md`](31-deploy-guest.md).

## 5. Собственная management identity гостя

Стандартная пара внутри Debian-гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Требования:

```text
/etc/proxmox-guest/ssh/              root:root 0700
management_ed25519                   root:root 0600
management_ed25519.pub               root:root 0644
algorithm                             ed25519
```

При `management.ssh_identity: true` после проверенного входа deployer выполняется:

```text
проверить стандартную keypair внутри guest
→ если пары нет полностью: создать её внутри guest
→ private key оставить внутри guest
→ прочитать наружу только public line/fingerprint
→ проверить формат и соответствие пары
→ зарегистрировать /var/lib/proxmox-deployer/public-keys/<VMID>.pub
→ проверить fingerprint registry
→ при изменении registry запустить sync-management-keys
```

Обычный повторный deploy не создаёт новую пару, если существующая пара корректна.

Неоднозначное состояние всегда означает `STOP`, например:

```text
private есть, public отсутствует
public есть, private отсутствует
private/public не соответствуют друг другу
registry содержит другой fingerprint
```

Обычный deploy не лечит такое состояние генерацией новой identity и не выполняет скрытую ротацию.

Если guest-local пара корректна, а `<VMID>.pub` на PVE отсутствует, deployer может повторно получить `.pub` из гостя и зарегистрировать его после проверки.

## 6. Lifecycle identity

Identity привязана к VMID, а не к имени гостя:

```text
первое создание VM/LXC     → создать пару и зарегистрировать <VMID>.pub
повторный deploy           → использовать существующую корректную пару
переименование гостя       → ключ не менять
удаление гостя             → удалить <VMID>.pub и синхронизировать общий набор
новый guest с тем же VMID  → создать новую identity; старую не переиспользовать
ротация                    → только отдельной явной операцией
```

Оставшийся `<VMID>.pub` от удалённого объекта не считается identity новой машины с тем же VMID. Такое состояние требует явной очистки/подтверждения до регистрации нового ключа.

Удаление public key из PVE registry не удаляет private key внутри его владельца автоматически.

## 7. Участники management SSH-контура

Для runtime discovery используется технический PVE tag:

```text
management-ssh
```

Это не поле manifest и не lifecycle ownership marker.

Правила:

- `deploy-guest` ставит tag Debian-гостю, если effective state содержит `management.ssh`;
- AI или другой управляющий guest, создающий Debian VM/LXC напрямую через PVE API и передающий ей management public keys, ставит тот же tag;
- `sync-management-keys` рассматривает только объекты с `management-ssh`;
- tag является необходимым, но не достаточным условием записи: дополнительно проверяются объект, адрес, уже установленный persistent SSH trust и management-контракт; bulk `sync-management-keys` не выполняет TOFU неизвестного host key;
- отсутствие tag означает: массовая синхронизация объект не трогает.

`management-ssh` не заменяет ownership tag:

```text
proxmox-deployer
```

Поэтому AI-created объект может участвовать в management SSH sync, не принадлежать lifecycle `deploy-guest` и не иметь `proxmox-deployer`.

Удаление только tag `management-ssh` не является отзывом уже установленного public key и не должно молча удалять доступ.

## 8. Начальный набор ключей новой машины

Новая управляемая Debian VM/LXC получает **весь текущий**:

```text
management-authorized-keys
```

до первого административного SSH.

Для VM используется Cloud-Init `sshkeys`, для LXC — штатный `ssh-public-keys`.

Минимально обязательный ключ — `deployer.pub`.

Управляющий guest, создающий Debian VM/LXC через PVE API, использует свою локальную копию:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и одновременно ставит новой машине tag `management-ssh`.

## 9. Локальный public catalog внутри гостей

У участвующих Debian-гостей хранится локальная копия публичного каталога:

```text
/etc/proxmox-guest/public-keys/
├── deployer.pub
├── <VMID>.pub
├── ...
└── management-authorized-keys
```

Private keys в этом каталоге запрещены.

Каталог нужен для:

- аудита текущих management identities;
- создания новой VM/LXC управляющим guest без записи в PVE filesystem;
- передачи новому гостю полного актуального management key set.

## 10. Первое SSH-знакомство с новой машиной

Домашняя сеть проекта считается доверенной для первого подключения к **только что созданному** ожидаемому объекту.

```text
известны VMID и ожидаемый IP
→ дождаться SSH
→ получить SSH host key сервера
→ сохранить в /var/lib/pvedeploy/.ssh/known_hosts
→ немедленно повторить подключение со строгой проверкой
```

После первого сохранения все последующие подключения выполняются со строгой сверкой.

Неожиданная смена host key у известного гостя:

```text
→ STOP
```

Автоматический `ssh-keygen -R`, удаление старой записи или молчаливое принятие нового ключа запрещены.

Переиспользование IP/VMID после удаления старого объекта требует отдельного осознанного обновления доверия.

## 11. `sync-management-keys`

Это отдельная PVE-команда:

```bash
sync-management-keys
```

Она не создаёт VM/LXC, не меняет CPU/RAM/disk/network и не выполняет прикладной provisioning.

Канонический поток:

```text
/var/lib/proxmox-deployer/public-keys/
→ валидировать весь registry до первой guest mutation
→ детерминированно пересобрать management-authorized-keys
→ перечислить PVE VM/LXC с tag management-ssh
→ проверить каждого участника
→ подключиться root с deployer private key
→ синхронизировать /etc/proxmox-guest/public-keys/
→ обновить только управляемый блок /root/.ssh/authorized_keys
→ проверить результат
```

Команда может запускаться вручную. `deploy-guest` вызывает её после фактического добавления/изменения зарегистрированного management public key.

Offline guest не запускается автоматически только ради sync. Его состояние остаётся pending/error до следующей успешной синхронизации. Неизвестный SSH host key также блокирует bulk sync: первичное доверие разрешено `deploy-guest` только для только что созданного ожидаемого VMID/IP.

Ошибка sync не вызывает удаление созданного private key или разрушительный rollback VM/LXC.

## 12. Валидация registry

До изменения первого гостя должны быть проверены как минимум:

- каждый `*.pub` содержит ровно один допустимый OpenSSH public key;
- private/PEM/OpenSSH private material отсутствует;
- fingerprints вычисляются успешно;
- один fingerprint не зарегистрирован под несколькими именами;
- дополнительные filenames соответствуют `<VMID>.pub`;
- `deployer.pub` присутствует и соответствует штатному `pve_guest_ed25519.pub`;
- aggregate собирается детерминированно;
- пустой/повреждённый registry не приводит к массовому удалению доступа.

При любой ошибке — `STOP` до первой guest mutation.

Записи registry меняются атомарно.

## 13. Управляемый блок `authorized_keys`

`sync-management-keys` не уничтожает личные или неизвестные SSH-ключи.

Он владеет только своим блоком:

```text
# BEGIN PROXMOX-MANAGEMENT-KEYS
<management-authorized-keys>
# END PROXMOX-MANAGEMENT-KEYS
```

в:

```text
/root/.ssh/authorized_keys
```

Строки вне блока сохраняются без изменений.

После удаления public key из канонического registry следующая успешная синхронизация удаляет его только:

- из управляемого блока `authorized_keys`;
- из локального public catalog гостей.

## 14. Ротация и отзыв

Ротация — отдельная явная процедура, а не побочный эффект обычного deploy:

```text
создать/подтвердить новую pair у владельца
→ зарегистрировать новый public key
→ sync-management-keys
→ проверить доступ новой identity
→ только затем считать старую identity отозванной
```

Массовое удаление ключа без предварительной полной валидации registry запрещено.

## 15. Граница PVE Configuration

PVE Configuration отвечает только за общую инфраструктуру management SSH-контура:

```text
создать/проверить deployer SSH identity
→ подготовить /var/lib/proxmox-deployer/public-keys/
→ зарегистрировать deployer.pub
→ подготовить необходимые runtime prerequisites
```

Она не содержит hardcoded списка VMID управляющих гостей и не создаёт заранее ключи для `301`, `311` или будущих ролей.

Текущий PVE-хост является новым, поэтому отдельная миграция старого deployer SSH key не требуется. Если пары нет — она создаётся по текущей схеме; если корректная пара уже существует — обычный повторный запуск её сохраняет.

## 16. Разделение ответственности

```text
PVE Configuration
→ общий deployer SSH identity и базовая инфраструктура registry

guest.yaml / schema v7 / resolver
→ management.ssh
→ management.ssh_identity boolean desired state
→ management.project_repo_read boolean desired state

deploy-guest
→ management-ssh tag
→ guest-local management pair
→ регистрация <VMID>.pub
→ вызов sync после изменения registry

sync-management-keys
→ массовая синхронизация public catalog и managed authorized_keys block

AI / другой PVE API creator
→ не пишет в PVE filesystem
→ использует локальный management-authorized-keys
→ передаёт весь набор новой Debian VM/LXC
→ ставит tag management-ssh
```

Универсальный secret broker, root-helper для public-key registry, каталог management private keys на PVE, VMID allowlist ролей, Git write-доступ ради регистрации ключей и SSH-доступ AI к PVE в эту модель не входят.

## 17. Состояние реализации

Этот файл задаёт **действующий нормативный контракт**, а не список выполненных задач.

Schema v7, resolver и рабочие manifests уже понимают `management.ssh_identity` и `management.project_repo_read`. Runtime-компоненты `deploy-guest`, `sync-management-keys` и связанные guest handlers реализуются отдельно.

Актуальная граница «что уже работает в коде / что только принято как контракт» находится только в:

[`29-implementation-status.md`](29-implementation-status.md)

Так спецификация остаётся стабильным источником требуемого поведения и не устаревает при каждом следующем шаге реализации.