# Первичная и повторяемая настройка гостевых систем

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для Guest Bootstrap v1, границы `deploy-guest` ↔ Ansible и требований к готовности административного доступа.

Общая политика SSH-ключей находится в [`23-security.md`](23-security.md). Формат `guest.yaml` — в [`30-guest-manifest.md`](30-guest-manifest.md). Общий PLAN/APPLY-контракт — в [`31-deploy-guest.md`](31-deploy-guest.md).

## 1. Принятая модель

Guest Bootstrap входит в первую версию `deploy-guest`.

```text
deploy-guest.py
→ жизненный цикл PVE
→ начальный административный доступ
→ проверка SSH root
→ явно запрошенный Guest Bootstrap v1
→ финальная проверка
→ передача управления Ansible
→ штатная повторяемая настройка ОС и приложений
```

Ключевое правило:

> Административный SSH — часть готовности гостевой системы к управлению. Guest Bootstrap начинается только после проверенного `root SSH`.

`bootstrap` является действующим машинным интерфейсом schema v6. `provisioning` остаётся будущим интерфейсом и в действующую схему пока не входит.

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
→ проверить/применить явно запрошенные bootstrap capabilities
→ проверить результат каждой capability
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

`deploy-guest` не создаёт и не ротирует эту пару. Открытый ключ используется при создании гостя, закрытый — для проверки SSH и выполнения Guest Bootstrap.

SSH host keys гостей хранятся отдельно:

```text
/var/lib/pvedeploy/.ssh/known_hosts
```

Правила первичного доверия и смены host key определены в [`31-deploy-guest.md`](31-deploy-guest.md).

## 5. Начальный доступ: VM

Шаблон `9000` не содержит постоянного проектного `authorized_keys`.

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

Закрытый ключ внутрь VM не передаётся.

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
PVE state → SSH root → финальная проверка → SUCCESS
```

Если блок есть:

```text
PVE state → SSH root → Bootstrap PLAN/APPLY → финальная проверка → SUCCESS
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

## 11. `base`

`base` — узкая базовая подготовка, а не универсальный список пакетов.

Она может обеспечивать только системные предпосылки, необходимые следующим bootstrap-capabilities и штатному Ansible-управлению. Точный набор устанавливаемых компонентов является версионируемым кодом capability, а не произвольным YAML-списком.

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

Она не передаёт автоматически закрытые GitHub credentials и не клонирует произвольные репозитории из данных manifest.

Если конкретному управляющему узлу нужен checkout проекта, его credential и точный сценарий должны иметь отдельный безопасный контракт, а не появляться как секрет внутри `guest.yaml`.

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

## 15. Ошибка Bootstrap

Guest Bootstrap является частью полного deploy v1.

Если запрошенная capability завершилась ошибкой или не прошла финальную проверку:

```text
→ deploy считается FAILED
→ следующие capabilities не выполняются
→ автоматический разрушительный rollback не выполняется
→ объект сохраняется
→ deploy-incomplete остаётся
→ журнал фиксирует capability и ошибку без секретов
```

Следующий запуск заново проверяет фактическое состояние и выполняет только недостающие изменения. Он не продолжает слепо с сохранённого номера шага.

## 16. `deploy-incomplete`

Для нового объекта `deploy-incomplete` устанавливается как можно раньше после создания и снимается только после:

```text
PVE configuration verified
+
SSH root verified
+
все запрошенные bootstrap capabilities verified
+
final PVE state verified
```

Если существующий уже управляемый объект без `deploy-incomplete` требует реального изменения Bootstrap, перед первой изменяющей SSH-командой deploy должен установить `deploy-incomplete`. После успешной полной приёмки тег снимается.

Read-only PLAN и `NO CHANGE` не должны создавать этот тег.

## 17. Что не относится к Guest Bootstrap

В Bootstrap v1 не входят:

- произвольный список пакетов;
- произвольные shell-команды из manifest;
- `rootfs/` sync;
- PostgreSQL, MQTT, Grafana, Gitea/Gogs, Semaphore, Jenkins и другие приложения;
- прикладные Compose stacks;
- управление секретами приложений;
- обычные Ansible roles для сервисов;
- управление `authorized_keys` уже существующих гостей как универсальная функция.

Всё это относится к последующей повторяемой конфигурации или отдельным специально спроектированным операциям.

## 18. Повторяемая настройка через Ansible

После Bootstrap штатная конфигурация ОС и приложений выполняется Ansible с `311-dev-services`.

Будущий высокоуровневый интерфейс `provisioning` может быть введён позднее, но сейчас он не входит в schemas и не реализуется скрыто внутри `deploy-guest`.

Знания о конкретной установке сервисов живут в Ansible roles/playbooks и соответствующих guest-файлах проекта, а не в `deploy-guest.py`.

## 19. Специальный Bootstrap `311-dev-services`

`311-dev-services` — штатный управляющий узел Ansible и первый целевой сценарий полного Bootstrap v1.

Его manifest явно содержит:

```yaml
bootstrap:
  capabilities:
    base: true
    git: true
    docker: true
    ansible_controller: true
```

Последовательность:

```text
deploy-guest 311 --apply
→ создать LXC 311
→ настроить PVE state
→ запустить
→ проверить SSH root
→ base
→ git
→ docker
→ ansible_controller
→ проверить все capabilities
→ финально сверить PVE
→ снять deploy-incomplete
→ SUCCESS
```

Целевой базовый результат:

```text
311-dev-services
├── Debian 13
├── SSH root только по ключу
├── Git
├── Docker Engine + Compose plugin
└── Ansible controller / EE
```

Semaphore, Git-сервис и CI устанавливаются уже после этого через Ansible.

## 20. Обычные Linux-гости

Обычный guest без `bootstrap` получает PVE-состояние и административный SSH, после чего конфигурируется с `311`.

Гость может явно запросить только нужный поднабор Bootstrap. Например Docker-host может иметь:

```yaml
bootstrap:
  capabilities:
    base: true
    docker: true
```

Это не означает установку приложений внутри Docker.

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

Начальная установка SSH-ключа, host-key trust и проверка SSH находятся в ядре `deploy-guest.py`, а не в `bootstrap/base.py`.

`scripts/guest_config.py` остаётся единым resolver для validator и deployer и определяет допустимые capability, зависимости и канонический порядок.

## 22. Ограничения безопасности

- секреты, пароли, токены и закрытые ключи не хранятся в `guest.yaml`;
- GitHub Deploy Key PVE не используется как SSH-ключ гостя;
- Guest Bootstrap использует только выделенный административный PVE guest key;
- capability handlers не печатают секреты и полный environment;
- AI, PVE и Ansible сохраняют независимые пары ключей;
- Bootstrap не расширяется произвольными командами ради удобства;
- все сетевые скачивания/репозитории, которые появятся в handlers, должны иметь явный проектный контракт и проверяемый источник.

## 23. Итоговая цепочка

```text
PVE Configuration
   └─ pve_guest_ed25519

            ↓

guest.yaml schema v6
   ↓
scripts/guest_config.py
   ↓
PVE desired state + ordered bootstrap capabilities
   ↓
deploy-guest.py
   ├─ PLAN / APPLY PVE
   ├─ initial root SSH
   ├─ verified root SSH
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

Главное правило: **Guest Bootstrap v1 доводит новую Debian VM/LXC от проверенного SSH до минимально требуемой инфраструктурной готовности; всё прикладное и долговременное управление остаётся Ansible.**
