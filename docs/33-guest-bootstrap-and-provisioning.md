# Первичная и повторяемая настройка гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для Guest Bootstrap v1, границы `deploy-guest` ↔ Ansible и требований к готовности административного доступа.

Общая политика SSH-ключей и общего Git read-only credential находится в [`23-security.md`](23-security.md). Формат `guest.yaml` — в [`30-guest-manifest.md`](30-guest-manifest.md). Общий PLAN/APPLY-контракт — в [`31-deploy-guest.md`](31-deploy-guest.md).

## 1. Принятая модель

Guest Bootstrap входит в первую версию `deploy-guest`.

```text
deploy-guest.py
→ жизненный цикл PVE
→ начальный административный доступ
→ проверка SSH root
→ project_repo_read, если явно запрошен
→ явно запрошенный Guest Bootstrap v1
→ финальная проверка
→ передача управления Ansible
→ штатная повторяемая настройка ОС и приложений
```

Ключевое правило:

> Административный SSH — часть готовности гостевой системы к управлению. Любая передача project Git credential и Guest Bootstrap начинается только после проверенного `root SSH`.

`bootstrap` является действующим машинным интерфейсом schema v6. `access.project_repo_read` принят как целевой интерфейс, но ещё не реализован в действующей schema v6; его реализация должна следовать зафиксированному контракту без введения универсального secret API. `provisioning` остаётся будущим интерфейсом и в действующую схему пока не входит.

## 2. Ответственность `deploy-guest.py`

`deploy-guest.py` отвечает за:

```text
создать или найти VM/LXC
→ CPU / RAM / диск / сеть
→ pool / protection / параметры запуска
→ установить начальный открытый SSH-ключ PVE штатным механизмом типа гостя
→ запустить, если требуется
→ дождаться SSH root
→ проверить административный доступ
→ при access.project_repo_read materialize фиксированный общий Git READ credential
→ проверить/применить явно запрошенные bootstrap capabilities
→ проверить результат каждого управляемого шага
→ завершить deploy только после полной приёмки
```

`deploy-guest` не должен превращаться во второе средство управления конфигурацией Linux и приложений.

## 3. Административный пользователь

Для управляемых Debian VM/LXC:

```text
root:22
```

Пароль `root` заблокирован, вход по паролю отключён, разрешена только аутентификация по ключам.

Универсальный пользователь `ops` не является частью целевого контракта гостевых систем.

## 4. SSH-ключ PVE-хоста

Развёртывание со стороны PVE использует постоянную пару:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
/etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

`deploy-guest` не создаёт и не ротирует эту пару. Открытый ключ используется при создании гостя, закрытый — для проверки SSH и выполнения управляемых действий внутри гостя.

SSH host keys гостей хранятся отдельно:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Правила первичного доверия и смены host key определены в [`31-deploy-guest.md`](31-deploy-guest.md).

## 5. Начальный доступ: VM

Шаблон `9000` не содержит постоянного проектного `authorized_keys` и не содержит общего Git read-key.

Новый Full Clone до первого запуска получает открытый ключ PVE через Cloud-Init:

```text
Full Clone from 9000
→ hostname / CPU / RAM / disk / network
→ ciuser=root
→ sshkeys=<pve_guest_ed25519.pub>
→ Cloud-Init update
→ start
→ QGA/Cloud-Init readiness при необходимости
→ SSH root
```

Закрытый административный ключ внутрь VM не передаётся.

## 6. Начальный доступ: LXC

Для Debian LXC:

```text
разрешить конкретный установленный Debian template
→ создать LXC с ssh-public-keys
→ запустить
→ проверить SSH root
```

Proxmox устанавливает открытый ключ для `root` при создании контейнера. `base` не участвует в получении начального SSH-доступа.

## 7. Интерфейс Guest Bootstrap v1

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
PVE state → SSH root → optional project_repo_read → финальная проверка → SUCCESS
```

Если блок есть:

```text
PVE state → SSH root → optional project_repo_read → Bootstrap PLAN/APPLY → финальная проверка → SUCCESS
```

Разрешённый набор v1:

- `base` — минимальные системные предпосылки для дальнейшего управляемого обслуживания;
- `git` — Git как инфраструктурный инструмент;
- `docker` — проектная установка Docker Engine и Compose plugin;
- `ansible_controller` — контейнеризированный управляющий Ansible / Execution Environment для `311-dev-services`.

Произвольные имена capabilities запрещены.

## 8. Зависимости и порядок

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

## 9. PLAN для Bootstrap

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

PLAN не должен делать package install/update, менять файлы, запускать/включать сервисы или выполнять другие изменяющие SSH-команды.

## 10. APPLY каждой capability

Каждый обработчик строится по модели:

```text
read-only check
→ если уже соответствует: NO CHANGE
→ если не соответствует: apply
→ final verify
```

Capability должна быть идемпотентной: повторный `deploy-guest <VMID> --apply` не должен переустанавливать или ломать уже соответствующее состояние.

Изменения выполняются только через административный SSH гостя. Основная PVE-логика не использует `pct exec`, `qm guest exec` или локальный root-обход как скрытый второй канал настройки ОС.

### 10.1. Безопасное поведение установки

Общие правила обязательны для всех capability handlers v1.

Перед изменением handler сначала выполняет read-only check. Если требуемое состояние уже достигнуто, он возвращает `NO CHANGE` и не выполняет `apt-get update`, установку пакетов, перезапуск сервисов или сетевые скачивания только ради «обновления на всякий случай».

Если изменение действительно нужно:

- все удалённые команды и ожидания имеют конечный timeout;
- блокировка `apt`/`dpkg` может ожидаться только конечное время; после timeout запуск останавливается;
- перед изменяющей операцией пакетный менеджер должен быть в однозначно рабочем состоянии;
- обнаруженный незавершённый или повреждённый `dpkg`/`apt` означает STOP, а не скрытый автоматический ремонт;
- `apt-get update` разрешён только как подготовка к реально требуемой установке или настройке репозитория capability;
- устанавливаются только явно перечисленные в версионируемом коде capability пакеты;
- для обычных пакетов используется минимальная установка без необязательных рекомендаций, если capability явно не требует обратного;
- уже установленная приемлемая версия не обновляется только потому, что в репозитории появилась более новая;
- после изменения handler обязан выполнить собственную final verify и только после неё перейти к следующей capability.

В Guest Bootstrap v1 запрещены скрытые общесистемные операции:

```text
apt upgrade
apt full-upgrade
apt dist-upgrade
apt autoremove
автоматический purge/remove пакетов
автоматический reboot/shutdown гостя
```

Capability может запустить или перезапустить только принадлежащий ей сервис и только когда это необходимо для достижения её собственного состояния. Перезагрузка всей гостевой ОС требует отдельного будущего контракта и не выполняется автоматически.

Для сетевой установки запрещены конструкции вида:

```text
curl ... | sh
wget ... | sh
```

и отключение проверки TLS. Внешний repository, signing key, URL или прямой артефакт должен быть заранее определён версионируемым кодом capability, а не приходить произвольной строкой из `guest.yaml`. HTTPS используется с проверкой сертификата; для пакетных репозиториев проверяется подпись, а для прямых артефактов при их использовании должен быть предусмотрен проверяемый механизм целостности.

## 11. `base`

`base` — узкая базовая подготовка, а не универсальный список полезных пакетов.

В v1 она гарантирует только следующий обязательный минимум Debian-гостя:

```text
python3
python3-apt
ca-certificates
rsync
```

Назначение этого набора:

- `python3` — базовый Python runtime для последующего управляемого обслуживания и Ansible-модулей;
- `python3-apt` — штатная Python-интеграция Debian с APT;
- `ca-certificates` — доверенное системное хранилище CA для проверяемого HTTPS;
- `rsync` — базовый транспорт синхронизации файлов для последующего управляемого развёртывания.

Read-only check `base` должен однозначно подтвердить как минимум:

```text
python3 запускается
Python module apt импортируется
системное CA bundle существует
rsync запускается
```

Если всё уже присутствует, `base` возвращает `NO CHANGE`. Это нормальный ожидаемый случай для VM, созданной из подготовленного template `9000`.

`base` не владеет и не переустанавливает:

```text
OpenSSH
Git
curl/wget/jq
Docker
Ansible
locale/timezone
пользовательские пакеты
прикладные сервисы
```

SSH уже обязан работать до запуска Bootstrap. Git, Docker и Ansible относятся к собственным capabilities. Другие утилиты capability устанавливает сама только тогда, когда они являются её явной технической зависимостью.

Точный набор `base` является частью контракта v1 и не расширяется «полезными» пакетами без изменения спецификации. В частности `base` не выполняет обновление всей ОС и не меняет системные настройки, не необходимые указанному минимальному набору.

Запрещён интерфейс вида:

```yaml
bootstrap:
  packages:
    - nginx
    - postgresql
```

Прикладное ПО не относится к `base`.

## 12. `git`

`git` обеспечивает наличие рабочего Git-клиента и проверяет его работоспособность.

Capability `git` **не означает выдачу GitHub credential**. Она не создаёт Deploy Key, не выбирает repository и не клонирует произвольные репозитории из данных manifest.

Доступ к основному приватному проектному репозиторию выражается отдельно через принятый контракт:

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

## 12.1. `project_repo_read`

Для `zsergeyru/proxmox` принят один общий GitHub Deploy Key только для чтения. Мастер-копия хранится root-only на PVE:

```text
/etc/proxmox-deployer/ssh/github_proxmox_repo_ed25519
```

`guest.yaml` может запросить только логическую возможность `project_repo_read: true`. Он не может выбрать имя секрета, другой ключ, путь на PVE, repository или write-доступ.

Root-wrapper передаёт содержимое фиксированного read-key `deploy-guest` только на время конкретного APPLY через отдельный file descriptor. `pvedeploy` не получает постоянного доступа к исходному root-only файлу.

Стандартная локальная копия внутри Debian-гостя:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

с `root:root 0600`.

Это **не** ключ административного входа в гостя и он не добавляется в `authorized_keys`.

Read-only check подтверждает наличие, владельца/права и ожидаемый fingerprint без вывода содержимого private key. Если Git client уже доступен, final verify может дополнительно выполнить read-only проверку доступа к `git@github.com:zsergeyru/proxmox.git`.

Если credential уже соответствует — `NO CHANGE`. Обычный deploy не удаляет его автоматически только потому, что поле позже исчезло: отзыв общего credential требует отдельной явной операции.

Если AI требуется запись в GitHub, write credential является отдельным контуром и не входит в `project_repo_read`.

## 13. `docker`

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

Capability не должна бесконтрольно обновлять Docker при каждом запуске. Изменение политики установки или версии происходит через изменение версионируемого кода проекта и последующую проверку.

## 14. `ansible_controller`

`ansible_controller` предназначен для `311-dev-services` и создаёт управляющую среду Ansible / Execution Environment поверх уже работающего Docker.

Она не устанавливает Semaphore, Gitea/Gogs, Jenkins или другие прикладные DevOps-сервисы. Эти сервисы относятся к штатному Ansible provisioning.

Capability должна иметь собственный read-only check и финальную проверку, позволяющую однозначно подтвердить готовность управляющего Ansible-окружения.

## 15. Ошибка Bootstrap или Project Git access

Guest Bootstrap и явно запрошенный `project_repo_read` являются частью полной приёмки deploy.

Если запрошенная capability завершилась ошибкой, Git credential не материализовался/не прошёл проверку или другая обязательная проверка не прошла:

```text
→ deploy считается FAILED
→ следующие зависимые шаги не выполняются
→ автоматический разрушительный rollback не выполняется
→ объект сохраняется
→ deploy-incomplete остаётся
→ журнал фиксирует тип шага и ошибку без секретов
```

Следующий запуск заново проверяет фактическое состояние и выполняет только недостающие изменения. Он не продолжает слепо с сохранённого номера шага.

## 16. `deploy-incomplete`

Для нового объекта `deploy-incomplete` устанавливается как можно раньше после создания и снимается только после:

```text
PVE configuration verified
+
SSH root verified
+
project_repo_read verified, если запрошен
+
все запрошенные bootstrap capabilities verified
+
final PVE state verified
```

Если существующий уже управляемый объект без `deploy-incomplete` требует реального изменения Project Git access или Bootstrap, перед первой изменяющей SSH-командой deploy должен установить `deploy-incomplete`. После успешной полной приёмки тег снимается.

Read-only PLAN и `NO CHANGE` не должны создавать этот тег.

## 17. Что не относится к Guest Bootstrap

В Bootstrap v1 не входят:

- произвольный список пакетов;
- произвольные shell-команды из manifest;
- `rootfs/` sync;
- PostgreSQL, MQTT, Grafana, Gitea/Gogs, Semaphore, Jenkins и другие приложения;
- прикладные Compose stacks;
- управление секретами приложений;
- универсальный secret broker;
- произвольные Git credentials/repositories;
- обычные Ansible roles для сервисов;
- управление `authorized_keys` уже существующих гостей как универсальная функция.

Фиксированный `project_repo_read` относится к ядру deploy/access, а не к capability `git` и не превращает Bootstrap в универсальное управление секретами.

Всё прикладное относится к последующей повторяемой конфигурации или отдельным специально спроектированным операциям.

## 18. Повторяемая настройка через Ansible

После Bootstrap штатная конфигурация ОС и приложений выполняется Ansible с `311-dev-services`.

Будущий высокоуровневый интерфейс `provisioning` может быть введён позднее, но сейчас он не входит в schemas и не реализуется скрыто внутри `deploy-guest`.

Знания о конкретной установке сервисов живут в Ansible roles/playbooks и соответствующих guest-файлах проекта, а не в `deploy-guest.py`.

## 19. Специальный Bootstrap `311-dev-services`

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

После реализации принятого `access` interface целевой manifest 311 также должен содержать:

```yaml
access:
  project_repo_read: true
```

Последовательность:

```text
deploy-guest 311 --apply
→ создать LXC 311
→ настроить PVE state
→ запустить
→ проверить SSH root
→ materialize общий Git READ credential
→ base
→ git
→ docker
→ ansible_controller
→ проверить Project Git access и все capabilities
→ финально сверить PVE
→ снять deploy-incomplete
→ SUCCESS
```

Целевой базовый результат:

```text
311-dev-services
├── Debian 13
├── SSH root только по ключу
├── общий credential READ для zsergeyru/proxmox
├── Git
├── Docker Engine + Compose plugin
└── Ansible controller / EE
```

Semaphore, Git-сервис и CI устанавливаются уже после этого через Ansible.

## 20. Обычные Linux-гости

Обычный guest без `bootstrap` и без `project_repo_read` получает PVE-состояние и административный SSH, после чего конфигурируется с `311`.

Гость может явно запросить только нужный поднабор Bootstrap. Например Docker-host может иметь:

```yaml
bootstrap:
  capabilities:
    base: true
    docker: true
```

Это не означает установку приложений внутри Docker и не выдаёт ему доступ к проектному Git.

## 21. Структура реализации

Для v1 ожидается разделение ядра deploy и capability handlers:

```text
scripts/
├── pve/
│   └── deploy-guest.py
└── bootstrap/
    ├── base.py
    ├── git.py
    ├── docker.py
    └── ansible_controller.py
```

Начальная установка административного SSH-ключа, host-key trust, проверка SSH и материализация фиксированного `project_repo_read` credential находятся в ядре deploy/access, а не в `bootstrap/base.py` или `bootstrap/git.py`.

`scripts/guest_config.py` остаётся единым resolver для validator и deployer и после реализации `access` должен определять его допустимую семантику вместе со schemas.

## 22. Ограничения безопасности

- секреты, пароли, токены и закрытые ключи не хранятся в `guest.yaml`;
- `guest.yaml` не может запросить secret по имени или указать путь к нему;
- общий GitHub Deploy Key используется только как read-only credential `zsergeyru/proxmox`;
- мастер-копия общего Git read-key на PVE остаётся root-only;
- `pvedeploy` получает содержимое read-key только через временный FD конкретного запуска с `project_repo_read=true`;
- общий Git read-key не используется как SSH-ключ гостя или PVE;
- возможный AI write credential в GitHub остаётся отдельным;
- Guest Bootstrap использует только выделенный административный PVE guest key;
- capability handlers не печатают секреты и полный environment;
- административные AI, PVE и Ansible SSH-пары остаются независимыми;
- Bootstrap не расширяется произвольными командами ради удобства;
- все сетевые скачивания/репозитории, которые появятся в handlers, должны иметь явный проектный контракт и проверяемый источник.

## 23. Итоговая цепочка

```text
PVE Configuration
   ├─ pve_guest_ed25519
   └─ github_proxmox_repo_ed25519  (master READ, root-only)

            ↓

guest.yaml
   ├─ access.project_repo_read       # принятый, ожидает реализации schema
   └─ bootstrap.capabilities         # действующий schema v6
            ↓
scripts/guest_config.py
            ↓
PVE desired state + Project Git access + ordered bootstrap capabilities
            ↓
deploy-guest.py
   ├─ PLAN / APPLY PVE
   ├─ initial root SSH
   ├─ verified root SSH
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

Главное правило: **Guest Bootstrap v1 доводит новую Debian VM/LXC от проверенного SSH до минимально требуемой инфраструктурной готовности; read-only доступ к основному Git выдаётся отдельно и только явным `project_repo_read`; всё прикладное и долговременное управление остаётся Ansible.**