# AI Control — архитектурная спецификация

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для целевой архитектуры `301-ai-control`, границ управления и взаимодействия Proximo, Git, SSH и Ansible.

Пошаговый ввод `301-ai-control` находится в [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md). Точные права PVE задаёт [`25-pve-access-control.md`](25-pve-access-control.md), общую политику безопасности — [`23-security.md`](23-security.md), а жизненный цикл management SSH keys и их синхронизацию — [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

## 1. Разделение ответственности

```text
Proximo MCP + ai-agent@pve!infra
→ жизненный цикл обычных VM/LXC в зоне managed
→ создание, клонирование, настройка, запуск, остановка, снимки, резервное копирование, удаление и диагностика

deploy-guest + deployer@pve!host-deploy на стороне PVE
→ человек и локальная автоматика PVE-хоста
→ детерминированное планирование и применение из манифестов гостевых систем

Ansible на 311-dev-services
→ повторяемая настройка ОС и приложений внутри гостевых систем через SSH

прямой SSH из AI Control
→ диагностика, первичная настройка, разовые и аварийные действия внутри гостевой ОС
```

`deploy-guest` не является обязательным защитным шлюзом для AI-агента. AI и человек используют разные учётные записи PVE.

AI не получает SSH/root-доступ к самому PVE только ради создания гостевых систем или обмена management public keys.

## 2. `301-ai-control`

`301-ai-control` — целевая VM центрального AI-контура.

`320-ai-control` остаётся временным устаревающим управляющим узлом до успешного ввода `301` по инструкции и отдельного решения о выводе `320`.

Целевая структура приложения:

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── repos/
│   └── proxmox/
└── state/
```

Инфраструктурные credentials и management keys не обязаны жить внутри `/opt/ai-control`; для них используются стандартные пути управляемого гостя:

```text
/etc/proxmox-guest/ssh/
/etc/proxmox-guest/public-keys/
/etc/proxmox-guest/credentials/
```

Точные пути определяет [`28-management-ssh-keys.md`](28-management-ssh-keys.md) и файловая политика гостя.

## 3. Proximo и `managed`

Основной MCP для Proxmox — Proximo (`proximo-proxmox`).

Жёсткая граница доступа PVE задаётся учётной записью, токеном и ACL.

Основная зона изменения для `ai-agent@pve!infra`:

```text
/pool/managed
```

Обычный жизненный цикл Debian-гостя management-контура:

```text
AI-агент
→ Proximo
→ создать или клонировать сразу с pool=managed
→ настроить CPU, RAM, диски, сеть и параметры гостевой системы
→ передать актуальный management-authorized-keys
→ установить PVE tag management-ssh
→ запустить и проверить
→ дальнейшее управление гостевой системой через PVE и/или SSH в разрешённых пределах
```

AI может самостоятельно создавать, клонировать, настраивать и удалять обычные разрешённые VM/LXC через Proximo.

Шаблон `9000` остаётся отдельным источником клонирования и не входит в обычную зону изменения AI.

AI Control штатно не получает прав на:

- сеть PVE-хоста;
- администрирование пользователей, ролей и ACL;
- администрирование инфраструктуры SDN;
- изменение определений хранилищ;
- сертификаты и репозитории самого PVE;
- межсетевой экран хоста;
- перезагрузку и выключение физического PVE;
- изменение `/etc/proxmox-deployer` напрямую.

Точный набор привилегий и матрица ACL принадлежат [`25-pve-access-control.md`](25-pve-access-control.md).

## 4. Доступ PVE и гостевой management-контракт независимы

Пул `managed` определяет **права AI на объект Proxmox**, но не определяет содержимое `authorized_keys` внутри Linux.

```text
PVE ACL / pool
→ жизненный цикл и PVE-конфигурация объекта

management.ssh + public keys внутри guest
→ административный SSH root внутрь ОС

management.ssh_identity
→ собственная исходящая SSH identity гостя, если нужна

management.project_repo_read
→ только исходящий read-only доступ гостя к zsergeyru/proxmox
```

Эти уровни не подменяют друг друга.

Перемещение VM/LXC в `managed` или из него само по себе не должно генерировать private keys, регистрировать public keys или менять Git credential.

Технический PVE tag `management-ssh` означает участие Debian-гостя в `sync-management-keys`, но не означает членство в pool `managed` и не означает lifecycle ownership со стороны `deploy-guest`.

## 5. Management SSH identity AI Control

AI Control должен иметь собственную административную SSH identity, отличную от deployer и Ansible.

После реализации целевого manifest-интерфейса для `301` используется:

```yaml
management:
  ssh_identity: true
```

Это не означает специальной логики `301` внутри PVE Configuration или `deploy-guest`.

Общий алгоритм одинаков для любого такого гостя:

```text
deploy-guest получает SSH root своим deployer key
→ внутри гостя создаёт/проверяет стандартную management keypair
→ private key остаётся только внутри гостя
→ наружу забирается только .pub
→ public key регистрируется на PVE
→ sync-management-keys распространяет обновлённый public registry
```

Стандартная private identity гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
```

AI использует её для прямого SSH к другим управляемым Debian-гостям.

AI не должен передавать этот private key на PVE, в Git, в другие управляющие VM или в новый guest.

## 6. Локальная копия management public-key registry

`sync-management-keys` с PVE доставляет в AI Control актуальный публичный каталог:

```text
/etc/proxmox-guest/public-keys/
└── management-authorized-keys
```

В нём находятся только открытые административные SSH-ключи, например deployer, AI Control, Ansible и будущих зарегистрированных управляющих identities.

Этот файл является источником ключей для **новых Debian VM/LXC, создаваемых AI**.

AI не запрашивает каталог с PVE по SSH и не записывает public keys обратно на PVE. Направление синхронизации:

```text
PVE canonical public-key registry
→ sync-management-keys
→ 301 local public-key copy
```

`sync-management-keys` рассматривает только PVE VM/LXC с tag `management-ssh` и дополнительно выполняет fail-closed проверки объекта и SSH trust.

## 7. Создание гостевой системы со стороны PVE

Последовательность по требуемому состоянию из Git:

```text
оператор
→ deploy-guest <VMID> [--apply]
→ deployer@pve!host-deploy
→ создать/клонировать Debian VM/LXC
→ передать текущий management-authorized-keys
→ установить PVE tag management-ssh
→ запустить
→ проверить SSH root private key deployer
→ при management.ssh_identity=true создать/проверить собственную keypair гостя и зарегистрировать .pub
→ при management.project_repo_read=true материализовать общий Git READ credential
→ выполнить явно запрошенный Guest Bootstrap
→ при появлении нового public key вызвать sync-management-keys
```

Начальный SSH-доступ `root` не зависит от `bootstrap.capabilities.base`.

Сам `301` является специальным только с точки зрения порядка bootstrap: его первоначальное создание выполняется со стороны PVE и не зависит от уже работающего AI Control. Механизм management SSH identity при этом остаётся общим и не привязан к VMID 301.

## 8. Гостевая система, создаваемая AI

При создании обычной Debian VM/LXC, участвующей в management SSH-контуре, AI обязан использовать локально синхронизированный набор:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

Последовательность:

```text
AI-агент
→ прочитать локальный management-authorized-keys
→ Proximo
→ ai-agent@pve!infra
→ создать/клонировать объект в managed
→ передать весь набор public keys через Cloud-Init/LXC ssh-public-keys
→ установить PVE tag management-ssh
→ запустить
→ проверить
```

Ключ deployer присутствует в этом наборе обязательно. Поэтому PVE-side `sync-management-keys` впоследствии может однозначно обнаружить по tag и войти в созданную AI машину без отдельной передачи SSH access.

AI не обязан и штатно не может запускать локальный `/usr/local/sbin/deploy-guest` на PVE.

Если новой управляющей машине нужна **собственная** management identity, её целевой `guest.yaml` должен содержать:

```yaml
management:
  ssh_identity: true
```

Регистрация новой `.pub` выполняется штатным `deploy-guest`. В v1 AI не получает отдельный удалённый API записи в PVE public-key registry.

## 9. Ansible

Ansible и Semaphore размещаются в `311-dev-services`, а не в AI Control.

`311`, как и любой другой управляющий guest с собственной административной identity, после реализации целевого интерфейса использует:

```yaml
management:
  ssh_identity: true
```

Его private key остаётся внутри 311. Открытая часть попадает в тот же PVE registry и затем `sync-management-keys` доставляет её в 301 и остальные управляемые Debian-гости с tag `management-ssh`.

Поэтому 311 не должен подключаться к PVE или 301 для «регистрации» своего public key.

После синхронизации AI автоматически знает Ansible public key через свой локальный public catalog, а уже существующие гости получают Ansible key в управляемом блоке `authorized_keys`.

## 10. Git

Закрытый `zsergeyru/proxmox` остаётся основным источником воспроизводимой конфигурации проекта.

Для чтения проекта AI Control использует общий проектный read-only Deploy Key, выдаваемый по целевому manifest-интерфейсу:

```yaml
management:
  project_repo_read: true
```

Локальная рабочая копия AI:

```text
/opt/ai-control/repos/proxmox
```

Локальная копия общего Git READ credential:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

используется только для `clone/fetch/read` `zsergeyru/proxmox`.

Git read-key не входит в management public-key registry и не используется для административного SSH. Объединение полей в разделе `management` не объединяет сами credentials.

AI может подготавливать изменения проекта в своей рабочей копии. Возможность отправлять их обратно в GitHub (`push`, ветки, PR через Git credential) является отдельным write-контуром и не входит ни в `management.project_repo_read`, ни в management key registry.

## 11. Неизменяемые правила безопасности

1. Токен PVE и ACL определяют допустимые операции AI в Proxmox.
2. `ai-agent@pve!infra` штатно управляет обычными гостевыми системами только в `managed`.
3. Прямой SSH-доступ внутрь гостевой системы является отдельной границей доступа.
4. AI private management key остаётся внутри AI Control.
5. AI не получает SSH/root-доступ к PVE ради management key sync.
6. Канонический public-key registry изменяется только PVE-side deploy/sync механизмом.
7. AI получает public registry только в направлении `PVE → guest` через `sync-management-keys`.
8. Новая Debian VM/LXC management-контура, создаваемая AI, получает весь текущий `management-authorized-keys` и tag `management-ssh`.
9. Закрытые SSH-ключи, токены и credentials внешних сервисов не хранятся в Git.
10. Вход `root` по паролю отключён.
11. `301`, рабочий Home Assistant, устаревающий `320` и шаблон `9000` не входят автоматически в обычную зону изменения AI `managed`.
12. Общий Git read-key даёт только чтение `zsergeyru/proxmox` и не повышается до write.
13. `management.ssh_identity` и `management.project_repo_read` не являются универсальными интерфейсами секретов и не наследуются из defaults/profile.
14. `management-ssh` является техническим PVE tag, а не ещё одним manifest-флагом.

## 12. Состояние реализации

`management.ssh_identity`, `management.project_repo_read`, PVE public-key registry, PVE tag `management-ssh` и `sync-management-keys` уже приняты как целевой архитектурный контракт, но ещё требуют реализации schemas/resolver/deployer/scripts.

Поэтому до реализации текущий `guest.yaml` 301 не должен получать неизвестные действующей schema v6 поля только ради соответствия документации.

Основной источник по состоянию реализации: [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

## 13. Связанные документы

- [`25-pve-access-control.md`](25-pve-access-control.md) — точные роли, привилегии и ACL PVE;
- [`23-security.md`](23-security.md) — общая политика SSH и секретов;
- [`28-management-ssh-keys.md`](28-management-ssh-keys.md) — management SSH identity, public-key registry, tag `management-ssh` и `sync-management-keys`;
- [`30-guest-manifest.md`](30-guest-manifest.md) — единый раздел `management` и его целевые поля;
- [`31-deploy-guest.md`](31-deploy-guest.md) — поведение deploy и безопасное применение;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — Guest Bootstrap и граница Ansible;
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md) — инструкция создания и ввода `301`;
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — спецификация шаблона `9000`.

Главное правило:

> AI создаёт новые управляемые Debian VM/LXC со всем локально синхронизированным набором management public keys и tag `management-ssh`; его собственный private key остаётся внутри AI Control, а обновление общего публичного набора выполняется только PVE-side `deploy-guest` + `sync-management-keys`, без SSH-доступа AI к PVE.