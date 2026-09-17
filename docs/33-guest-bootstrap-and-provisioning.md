# Первичная и повторяемая настройка гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для Guest Bootstrap v1, границы `deploy-guest` ↔ Ansible и требований к готовности административного доступа.

Общая политика SSH и секретов находится в [`23-security.md`](23-security.md). Точный жизненный цикл management SSH keys — в [`28-management-ssh-keys.md`](28-management-ssh-keys.md). Формат `guest.yaml` — в [`30-guest-manifest.md`](30-guest-manifest.md). Общий PLAN/APPLY-контракт — в [`31-deploy-guest.md`](31-deploy-guest.md).

## 1. Принятая модель

Guest Bootstrap входит в первую версию `deploy-guest`.

```text
deploy-guest.py
→ жизненный цикл PVE
→ передать актуальный management-authorized-keys при создании
→ проверка SSH root private key deployer
→ management_key, если явно запрошен
→ project_repo_read, если явно запрошен
→ явно запрошенный Guest Bootstrap v1
→ финальная проверка
→ передача управления Ansible
→ штатная повторяемая настройка ОС и приложений
```

Ключевое правило:

> Административный SSH — часть готовности гостевой системы к управлению. Генерация собственной management identity, передача project Git credential и Guest Bootstrap начинаются только после проверенного `root SSH` со стороны deployer.

`bootstrap` является действующим машинным интерфейсом schema v6. `access.project_repo_read` и `management_key` приняты как целевые интерфейсы, но ещё не реализованы в действующей schema v6. Их реализация должна следовать зафиксированным контрактам без введения универсального secret API или специальных полей для AI/Ansible. `provisioning` остаётся будущим интерфейсом.

## 2. Ответственность `deploy-guest.py`

`deploy-guest.py` отвечает за:

```text
создать или найти VM/LXC
→ CPU / RAM / диск / сеть
→ pool / protection / параметры запуска
→ при создании установить текущий management-authorized-keys штатным механизмом типа гостя
→ запустить, если требуется
→ дождаться SSH root
→ проверить доступ private key deployer
→ при management_key=true обеспечить собственную keypair гостя и зарегистрировать только .pub на PVE
→ после нового/изменённого public key вызвать sync-management-keys
→ при access.project_repo_read materialize фиксированный общий Git READ credential
→ проверить/применить явно запрошенные bootstrap capabilities
→ проверить результат каждого управляемого шага
→ завершить deploy только после полной приёмки
```

`deploy-guest` не должен превращаться во второе средство управления конфигурацией Linux и приложений.

Массовое распространение management public keys не является внутренним циклом `deploy-guest`; этим занимается отдельный [`sync-management-keys`](28-management-ssh-keys.md), который deploy вызывает только после фактического изменения канонического public-key registry.

## 3. Административный пользователь

Для управляемых Debian VM/LXC:

```text
root:22
```

Пароль `root` заблокирован, вход по паролю отключён, разрешена только аутентификация по ключам.

Универсальный пользователь `ops` не является частью целевого контракта гостевых систем.

## 4. SSH identity deployer

Развёртывание со стороны PVE использует постоянную пару:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Историческое имя файла сохраняется, но по смыслу это SSH identity **deployer**, а не ключ самого Proxmox.

`deploy-guest` не создаёт и не ротирует эту пару. PVE Configuration регистрирует её открытую часть как:

```text
/etc/proxmox-deployer/public-keys/deployer.pub
```

Она обязательно входит в `management-authorized-keys`, поэтому deployer должен иметь SSH-доступ к любой новой управляемой Debian VM/LXC независимо от того, создана она deployer или AI.

SSH host keys гостей хранятся отдельно:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Правила первичного доверия и смены host key определены в [`31-deploy-guest.md`](31-deploy-guest.md).

## 5. Начальный доступ: VM

Шаблон `9000` не содержит постоянного проектного `authorized_keys`, management private keys и общего Git read-key.

Новый Full Clone до первого запуска получает актуальный публичный набор через Cloud-Init:

```text
Full Clone from 9000
→ hostname / CPU / RAM / disk / network
→ ciuser=root
→ sshkeys=<management-authorized-keys>
→ Cloud-Init update
→ start
→ QGA/Cloud-Init readiness при необходимости
→ SSH root private key deployer
```

Никакие private management keys внутрь обычной VM не передаются.

## 6. Начальный доступ: LXC

Для Debian LXC:

```text
разрешить конкретный установленный Debian template
→ создать LXC с ssh-public-keys=<management-authorized-keys>
→ запустить
→ проверить SSH root private key deployer
```

Proxmox устанавливает открытые ключи `root` при создании контейнера. `base` не участвует в получении начального SSH-доступа.

Управляющий guest, создающий новую Debian VM/LXC напрямую через PVE API, использует свою локальную копию:

```text
/etc/proxmox-guest/public-keys/management-authorized-keys
```

и также передаёт весь набор новой машине.

## 7. Собственная management identity гостя

Целевой флаг:

```yaml
management_key: true
```

означает, что гостю нужна собственная SSH identity для исходящего административного доступа к другим управляемым Debian-гостям.

После проверенного входа deployer:

```text
проверить /etc/proxmox-guest/ssh/management_ed25519[.pub]
→ если пары нет — создать ed25519 внутри гостя
→ private key оставить только в госте
→ получить наружу только .pub
→ проверить fingerprint
→ зарегистрировать на PVE как <VMID>-<name>.pub
→ вызвать sync-management-keys
```

`management_key` не является capability `ansible_controller`, не означает AI/Ansible роль и не содержит path/name/private material. Точный контракт: [`28-management-ssh-keys.md`](28-management-ssh-keys.md).

## 8. Интерфейс Guest Bootstrap v1

Bootstrap задаётся только явно в конкретном `guest.yaml`:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Он не наследуется из `defaults.yaml` или профиля.

Если блока нет:

```text
PVE state → SSH root → optional management_key → optional project_repo_read → финальная проверка → SUCCESS
```

Если блок есть:

```text
PVE state → SSH root → optional management_key → optional project_repo_read → Bootstrap PLAN/APPLY → финальная проверка → SUCCESS
```

Разрешённый набор v1:

- `base` — минимальные системные предпосылки для дальнейшего управляемого обслуживания;
- `git` — Git как инфраструктурный инструмент;
- `docker` — проектная установка Docker Engine и Compose plugin;
- `ansible_controller` — контейнеризированный управляющий Ansible / Execution Environment для `311-dev-services`.

Произвольные имена capabilities запрещены.

## 9. Зависимости и порядок

Зависимости являются частью контракта и должны быть явно выражены в manifest:

```text
base

git
└─ base

docker
└─ base

ansible_controller
├─ base
├─ git
└─ docker
```

Resolver не добавляет зависимости автоматически.

Канонический порядок:

```text
base → git → docker → ansible_controller
```

Даже если YAML записан в другом порядке, PLAN и APPLY используют этот порядок.

Bootstrap требует:

```yaml
boot:
  start_after_deploy: true
```

поскольку он выполняется только через уже проверенный SSH root.

## 10. PLAN для Bootstrap

Без `--apply` Guest Bootstrap не имеет права изменять гостевую ОС.

Для работающего и доступного гостя PLAN может выполнить только read-only проверки, позволяющие определить состояние capability.

Типовой вывод:

```text
Bootstrap
  base                APPLY | NO CHANGE
  git                 APPLY | NO CHANGE
  docker              APPLY | NO CHANGE
  ansible_controller  APPLY | NO CHANGE
```

Для нового объекта, который ещё не существует, или для объекта, который нельзя проверить по SSH без изменения его состояния, PLAN не запускает гостя и не выполняет команды установки. В таком случае он показывает детерминированно запрошенные действия как требующие выполнения после появления SSH, например `APPLY AFTER START`.

PLAN не должен делать package install/update, менять файлы, генерировать management private key, регистрировать `.pub`, запускать sync, запускать/включать сервисы или выполнять другие изменяющие SSH-команды.

## 11. APPLY каждой capability

Каждый обработчик строится по модели:

```text
read-only check
→ если уже соответствует: NO CHANGE
→ если не соответствует: apply
→ final verify
```

Capability должна быть идемпотентной: повторный `deploy-guest <VMID> --apply` не должен переустанавливать или ломать уже соответствующее состояние.

Изменения выполняются только через административный SSH гостя. Основная PVE-логика не использует `pct exec`, `qm guest exec` или локальный root-обход как скрытый второй канал настройки ОС.

### 11.1. Безопасное поведение установки

Перед изменением handler сначала выполняет read-only check. Если требуемое состояние уже достигнуто, он возвращает `NO CHANGE` и не выполняет `apt-get update`, установку пакетов, перезапуск сервисов или сетевые скачивания только ради «обновления на всякий случай».

Если изменение действительно нужно:

- все удалённые команды и ожидания имеют конечный timeout;
- блокировка `apt`/`dpkg` может ожидаться только конечное время;
- перед изменяющей операцией пакетный менеджер должен быть в однозначно рабочем состоянии;
- обнаруженный незавершённый или повреждённый `dpkg`/`apt` означает STOP, а не скрытый автоматический ремонт;
- `apt-get update` разрешён только как подготовка к реально требуемой установке или настройке репозитория capability;
- устанавливаются только явно перечисленные в версионируемом коде capability пакеты;
- для обычных пакетов используется минимальная установка без необязательных рекомендаций, если capability явно не требует обратного;
- уже установленная приемлемая версия не обновляется только потому, что появилась более новая;
- после изменения handler обязан выполнить собственную final verify.

В Guest Bootstrap v1 запрещены:

```text
apt upgrade
apt full-upgrade
apt dist-upgrade
apt autoremove
автоматический purge/remove пакетов
автоматический reboot/shutdown гостя
```

Capability может запустить или перезапустить только принадлежащий ей сервис и только когда это необходимо для достижения её собственного состояния.

Для сетевой установки запрещены:

```text
curl ... | sh
wget ... | sh
```

и отключение проверки TLS. Внешний repository, signing key, URL или прямой артефакт должен быть заранее определён версионируемым кодом capability, а не приходить произвольной строкой из `guest.yaml`.

## 12. `base`

`base` — узкая базовая подготовка, а не универсальный список полезных пакетов.

В v1 она гарантирует только:

```text
python3
python3-apt
ca-certificates
rsync
```

Read-only check `base` подтверждает как минимум:

```text
python3 запускается
Python module apt импортируется
системное CA bundle существует
rsync запускается
```

Если всё уже присутствует, `base` возвращает `NO CHANGE`.

`base` не владеет и не переустанавливает OpenSSH, Git, Docker, Ansible, locale/timezone, пользовательские пакеты или прикладные сервисы.

Запрещён интерфейс вида:

```yaml
bootstrap:
  packages:
    - nginx
    - postgresql
```

Прикладное ПО не относится к `base`.

## 13. `git`

`git` обеспечивает наличие рабочего Git-клиента и проверяет его работоспособность.

Capability `git` **не означает выдачу GitHub credential**. Она не создаёт Deploy Key, не выбирает repository и не клонирует произвольные репозитории из данных manifest.

Доступ к основному приватному проектному репозиторию выражается отдельно:

```yaml
access:
  project_repo_read: true
```

Поэтому возможны разные состояния:

```text
git=true, project_repo_read=false
→ Git client есть, credential проекта не выдаётся

project_repo_read=true
→ выдаётся фиксированный общий read-only credential проекта
→ наличие Git client обеспечивается отдельно соответствующей системой/Bootstrap
```

## 13.1. `project_repo_read`

Для `zsergeyru/proxmox` принят один общий GitHub Deploy Key только для чтения. Мастер-копия хранится root-only на PVE:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

`guest.yaml` может запросить только `project_repo_read: true`. Он не может выбрать имя секрета, другой ключ, путь на PVE, repository или write-доступ.

Root-wrapper передаёт содержимое фиксированного read-key `deploy-guest` только на время конкретного APPLY через отдельный file descriptor. `pvedeploy` не получает постоянного доступа к исходному root-only файлу.

Стандартная локальная копия:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

с `root:root 0600`.

Это **не** ключ административного входа в гостя и он не добавляется в `authorized_keys` или management public-key registry.

Read-only check подтверждает наличие, владельца/права и ожидаемый fingerprint без вывода private key. Если Git client уже доступен, final verify может дополнительно выполнить read-only проверку доступа к `git@github.com:zsergeyru/proxmox.git`.

Если credential уже соответствует — `NO CHANGE`. Обычный deploy не удаляет его автоматически только потому, что поле позже исчезло: отзыв общего credential требует отдельной явной операции.

Если AI требуется запись в GitHub, write credential является отдельным контуром.

## 14. `docker`

`docker` устанавливает и проверяет проектно одобренный Docker Engine + Compose plugin.

Для Docker внутри LXC обязательны требования [`32-docker-in-lxc-policy.md`](32-docker-in-lxc-policy.md):

```text
unprivileged == true
nesting == true
keyctl == true
```

После APPLY должны успешно выполняться как минимум:

```bash
docker version
docker info
docker compose version
```

Capability не должна бесконтрольно обновлять Docker при каждом запуске.

## 15. `ansible_controller`

`ansible_controller` предназначен для `311-dev-services` и создаёт управляющую среду Ansible / Execution Environment поверх уже работающего Docker.

Она **не создаёт отдельный Ansible SSH keypair**. Собственная административная identity 311 задаётся общим для всех управляющих гостей флагом:

```yaml
management_key: true
```

Это сохраняет разделение: capability описывает ПО, а `management_key` — SSH identity гостя.

`ansible_controller` не устанавливает Semaphore, Gitea/Gogs, Jenkins или другие прикладные DevOps-сервисы. Эти сервисы относятся к штатному Ansible provisioning.

## 16. Ошибка management key, Bootstrap или Project Git access

`management_key`, Guest Bootstrap и явно запрошенный `project_repo_read` являются частью полной приёмки deploy.

Если собственная keypair не создана/не проверена, public key не зарегистрирован, обязательный `sync-management-keys` после новой регистрации не завершился, Git credential не прошёл проверку или capability завершилась ошибкой:

```text
→ deploy считается FAILED
→ следующие зависимые шаги не выполняются
→ автоматический разрушительный rollback не выполняется
→ объект сохраняется
→ deploy-incomplete остаётся
→ журнал фиксирует тип шага и ошибку без секретов
```

Следующий запуск заново проверяет фактическое состояние и выполняет только недостающие изменения.

## 17. `deploy-incomplete`

Для нового объекта `deploy-incomplete` устанавливается как можно раньше после создания и снимается только после:

```text
PVE configuration verified
+
SSH root verified
+
management_key verified and public key synced, если запрошен
+
project_repo_read verified, если запрошен
+
все запрошенные bootstrap capabilities verified
+
final PVE state verified
```

Если существующий уже управляемый объект требует реального изменения management identity, Project Git access или Bootstrap, перед первой изменяющей SSH-командой deploy устанавливает `deploy-incomplete`. Read-only PLAN и `NO CHANGE` не должны создавать этот тег.

## 18. Что не относится к Guest Bootstrap

В Bootstrap v1 не входят:

- управление management SSH identity — это ядро deploy/access и [`28-management-ssh-keys.md`](28-management-ssh-keys.md);
- массовая синхронизация public keys — это `sync-management-keys`;
- произвольный список пакетов;
- произвольные shell-команды из manifest;
- `rootfs/` sync;
- PostgreSQL, MQTT, Grafana, Gitea/Gogs, Semaphore, Jenkins и другие приложения;
- прикладные Compose stacks;
- управление секретами приложений;
- универсальный secret broker;
- произвольные Git credentials/repositories;
- обычные Ansible roles для сервисов.

Фиксированный `project_repo_read` также относится к ядру deploy/access, а не к capability `git`.

## 19. Повторяемая настройка через Ansible

После Bootstrap штатная конфигурация ОС и приложений выполняется Ansible с `311-dev-services`.

Будущий высокоуровневый интерфейс `provisioning` может быть введён позднее, но сейчас он не входит в schemas и не реализуется скрыто внутри `deploy-guest`.

Знания о конкретной установке сервисов живут в Ansible roles/playbooks и соответствующих guest-файлах проекта, а не в `deploy-guest.py`.

## 20. Специальный Bootstrap `311-dev-services`

`311-dev-services` — штатный управляющий узел Ansible и первый целевой сценарий полного Bootstrap v1.

Его действующий manifest сейчас явно содержит Bootstrap:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

После реализации целевых manifest interfaces 311 также должен содержать:

```yaml
management_key: true

access:
  project_repo_read: true
```

Последовательность:

```text
deploy-guest 311 --apply
→ создать LXC 311 с текущим management-authorized-keys
→ настроить PVE state
→ запустить
→ проверить SSH root private key deployer
→ создать/проверить собственную management keypair 311
→ зарегистрировать 311-dev-services.pub на PVE
→ sync-management-keys
→ materialize общий Git READ credential
→ base
→ git
→ docker
→ ansible_controller
→ проверить management identity, Project Git access и capabilities
→ финально сверить PVE
→ снять deploy-incomplete
→ SUCCESS
```

Целевой базовый результат:

```text
311-dev-services
├── Debian 13
├── SSH root только по ключам
├── собственный management private key
├── локальная копия общего management public-key registry
├── общий credential READ для zsergeyru/proxmox
├── Git
├── Docker Engine + Compose plugin
└── Ansible controller / EE
```

Semaphore, Git-сервис и CI устанавливаются уже после этого через Ansible.

## 21. Обычные Linux-гости

Обычный guest без `management_key`, `bootstrap` и `project_repo_read` получает PVE-состояние, актуальный `management-authorized-keys` и административный SSH. Затем он может конфигурироваться с 311.

Гость может явно запросить только нужный поднабор Bootstrap, например:

```yaml
bootstrap:
  capabilities:
    base: true
    docker: true
```

Это не создаёт ему private management key и не выдаёт доступ к проектному Git.

## 22. Структура реализации

Для v1 ожидается разделение ядра deploy и capability handlers:

```text
scripts/
├── pve/
│   ├── deploy-guest.py
│   └── sync-management-keys.py   # либо эквивалентный отдельный executable
└── bootstrap/
    ├── base.py
    ├── git.py
    ├── docker.py
    └── ansible_controller.py
```

Начальный management public-key set, host-key trust, проверка SSH, обработка `management_key`, регистрация `.pub` и материализация фиксированного `project_repo_read` находятся в ядре deploy/access, а не в `bootstrap/base.py`, `bootstrap/git.py` или `ansible_controller.py`.

Массовое распространение public keys остаётся отдельной командой `sync-management-keys`, даже если `deploy-guest` вызывает её после появления нового ключа.

`scripts/guest_config.py` остаётся единым resolver для validator и deployer.

## 23. Ограничения безопасности

- секреты, пароли, токены и закрытые ключи не хранятся в `guest.yaml`;
- `guest.yaml` не может запросить secret по имени или указать путь к нему;
- `management_key` — только boolean и не задаёт путь/private material/роль/VMID;
- private management key гостя создаётся и остаётся внутри владельца;
- наружу из управляющего гостя забирается только `.pub`;
- общий GitHub Deploy Key используется только как read-only credential `zsergeyru/proxmox`;
- мастер-копия общего Git read-key на PVE остаётся root-only;
- `pvedeploy` получает содержимое read-key только через временный FD конкретного запуска с `project_repo_read=true`;
- общий Git read-key не используется как SSH-ключ гостя или PVE;
- возможный AI write credential в GitHub остаётся отдельным;
- capability handlers не печатают секреты и полный environment;
- Bootstrap не расширяется произвольными командами ради удобства;
- все сетевые скачивания/репозитории должны иметь явный проектный контракт и проверяемый источник.

## 24. Итоговая цепочка

```text
PVE Configuration
   ├─ deployer private key
   ├─ public-key registry/deployer.pub
   └─ github_proxmox_repo_ed25519  (master READ, root-only)

            ↓

guest.yaml
   ├─ management_key                # принятый, ожидает реализации schema
   ├─ access.project_repo_read      # принятый, ожидает реализации schema
   └─ bootstrap.capabilities        # действующий schema v6
            ↓
scripts/guest_config.py
            ↓
PVE desired state + management identity + Project Git access + ordered bootstrap capabilities
            ↓
deploy-guest.py
   ├─ PLAN / APPLY PVE
   ├─ current management-authorized-keys при создании
   ├─ verified root SSH deployer
   ├─ management_key → guest-local private key + PVE public registry
   ├─ при новом .pub → sync-management-keys
   ├─ project_repo_read → standard local credential
   ├─ Guest Bootstrap v1
   │    base → git → docker → ansible_controller
   └─ final verification
             ↓
       Ansible on 311
             ↓
   roles / playbooks
             ↓
     application services
```

Главное правило: **Guest Bootstrap v1 доводит новую Debian VM/LXC от проверенного SSH до минимально требуемой инфраструктурной готовности; собственная management identity и общий Git read-only credential являются отдельными ядровыми механизмами deploy/access; всё прикладное и долговременное управление остаётся Ansible.**