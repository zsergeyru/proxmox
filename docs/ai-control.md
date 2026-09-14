# AI Control — архитектурные принципы

`ai-control` — отдельная VM управляющего AI-контура. Целевой VMID — `301`; текущая `320-ai-control` сохраняется как bootstrap до развёртывания и проверки новой VM.

## Назначение

AI-агенты должны уметь администрировать домашнюю инфраструктуру, а не только собственный контейнер.

Базовое разделение ответственности:

```text
Proximo MCP
→ создание и жизненный цикл VM/LXC, guest-level config, snapshots/backups и диагностика Proxmox

Ansible в 311-dev-services
→ повторяемые изменения внутри гостевых ОС через SSH: пакеты, файлы, systemd, Docker/Compose и приложения

прямой SSH из ai-control
→ bootstrap, диагностика, разовые административные действия и аварийные сценарии

специализированные MCP/API
→ используются там, где дают реальное преимущество
```

Отдельный универсальный `deploy-mcp` не является частью целевой архитектуры.

Решение по Ansible/Semaphore и простой модели `rootfs/` зафиксировано в [`../guests/311-dev-services/decisions/001-deployment-tooling.md`](../guests/311-dev-services/decisions/001-deployment-tooling.md). Матрица Proxmox-прав управляющего контура зафиксирована в [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md). Историческое решение по MCP для `320` и переход на Proximo для `301` описаны в [`../guests/320-ai-control/decisions/003-proxmox-mcp.md`](../guests/320-ai-control/decisions/003-proxmox-mcp.md).

Подробная спецификация zero-day развёртывания `301` находится в [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

## Zero-day bootstrap 301

Для нового компьютера не предполагается наличие Git, AI-агентов или доступа к приватному `zsergeyru/proxmox`. Единственная обязательная база — установленный Proxmox VE и готовый template `9000 tpl-debian13`.

Zero-day bootstrap разделён на два независимых публичных скрипта:

```text
create-ai-control-vm.sh
→ выполняется на PVE
→ создаёт и подготавливает VM 301
→ создаёт bootstrap management identity/token для Proximo

install-ai-control.sh
→ выполняется внутри 301
→ устанавливает Hermes/WebUI/Proximo
→ создаёт SSH identities
→ подготавливает GitHub Deploy Key onboarding
```

Канонический путь:

```text
чистый PVE + template 9000
→ public create-ai-control-vm.sh
→ Full Clone 9000 → 301
→ базовая VM 301 готова
→ public install-ai-control.sh внутри 301
→ Hermes + встроенный WebUI
→ Proximo
→ management SSH identity
→ GitHub Deploy Key identity
→ вывести public key человеку
→ человек регистрирует Deploy Key для zsergeyru/proxmox
→ повторная проверка install-ai-control.sh
→ clone приватного repo в /opt/ai-control/repos/proxmox
→ дальше инфраструктура разворачивается из Git
```

Bootstrap до запуска Hermes **не зависит от приватного Git** и не требует GitHub PAT. Это позволяет восстановить control plane на совершенно новом сервере.

Канонические executable-скрипты хранятся только в публичном репозитории `zsergeyru/proxmox-bootstrap`; здесь хранится документация и desired state.

## Hermes и WebUI

Основной AI-агент `301-ai-control` — Hermes.

Ключевое правило размещения:

```text
/opt/ai-control/agents/hermes/
```

Все будущие агенты также размещаются только в `/opt/ai-control/agents/<agent>/`.

Для первичной настройки и дальнейшей работы используется штатный WebUI/Dashboard Hermes; целевой порт проекта — `tcp/9119`.

`install-ai-control.sh` обязан использовать поддерживаемый Hermes механизм авторизации и не оставлять WebUI намеренно открытым без auth. Конкретные config/env names проверяются по фактической версии Hermes при реализации скрипта.

Через Dashboard после развёртывания задаются model/provider credentials. Эти секреты невозможно получить из пустого сервера автоматически, поэтому это нормальный первый ручной шаг после создания `301`.

## Proximo

Для целевого `301-ai-control` канонический Proxmox MCP — **Proximo** (`proximo-proxmox`).

Размещение:

```text
/opt/ai-control/mcp/proximo/
```

Общие MCP не устанавливаются внутрь каталога Hermes.

`create-ai-control-vm.sh` создаёт отдельную Proxmox management identity:

```text
user:  proximo@pve
token: proximo@pve!ai-control
privilege separation: enabled
```

Secret token передаётся непосредственно с PVE в `301` через QEMU Guest Agent и хранится только внутри `301` в защищённом файле. Из PVE временная копия удаляется после передачи.

`install-ai-control.sh` устанавливает сам Proximo, связывает его с подготовленным credential и проверяет границу прав через `proximo doctor`.

Для Hermes по умолчанию публикуется только guest-oriented surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

Это дополнительная защита поверх Proxmox ACL. Окончательной границей полномочий остаются privilege-separated token и ACL самого PVE.

Обычный AI workflow не получает права менять PVE host network, storage definitions, IAM/ACL, SDN, repositories, certificates или reboot/shutdown гипервизора.

## SSH identities 301

У `301` две независимые SSH identity. Они создаются `install-ai-control.sh` **внутри самой VM** и никогда не помещаются в template или Git.

### Infrastructure key

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
```

Private key остаётся только в `301`. Public key используется Hermes при создании новых managed Debian VM: он передаётся пользователю `ops` через Cloud-Init **до первого запуска**.

После этого `301` может подключаться к новой VM напрямую:

```text
301 Hermes
→ SSH ops@guest
→ ~/.ssh/ai_control_ed25519
```

### GitHub Deploy Key

```text
/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
```

Этот ключ используется только для приватного `zsergeyru/proxmox` и не используется для SSH в инфраструктурные VM.

`install-ai-control.sh` выводит только `.pub`. Оператор вручную добавляет его в:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Для автономного `commit/push` Hermes ключу требуется `Allow write access`. GitHub PAT для штатного bootstrap не нужен.

## Размещение компонентов

В `ai-control` находятся только компоненты AI-управляющего контура:

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   ├── proximo/
│   └── <future-mcp>/
├── repos/
│   └── proxmox/
└── state/
```

В `ai-control` находятся:

- Hermes и возможные будущие AI-агенты;
- Proximo и другие специализированные MCP;
- Git/SSH/API-клиенты, необходимые агентам;
- web/voice интерфейсы к агентам;
- служебный код, относящийся непосредственно к AI-управлению инфраструктурой.

Ansible и Semaphore не размещаются здесь как постоянные компоненты: это DevOps-инструменты `311-dev-services`.

Общие сервисы не должны жить внутри `ai-control` только потому, что ими пользуется один из агентов:

- DNS/VPN/PBR → `109-network-gateway`;
- MQTT/Zigbee2MQTT/ESPHome → `211-automation-services`;
- Ansible/Semaphore/Git/CI → `311-dev-services`;
- Homarr и обычные приложения → `321-app-services`;
- STT/TTS → `331-ai-services`;
- monitoring → `401-monitoring`;
- Frigate → `501-frigate`.

## Создание обычной Debian VM

Для операции уровня виртуализации AI-агент использует Proximo.

Для создания обычной Debian VM из `tpl-debian13` агент обязан соблюдать порядок:

```text
Full Clone from 9000
→ сразу поместить guest в managed pool
→ явно выставить protection=0, если guest.yaml не требует protection: true
→ применить CPU/RAM/disk/network
→ задать ciuser=ops
→ передать /home/ops/.ssh/ai_control_ed25519.pub через Cloud-Init sshkeys
→ обновить Cloud-Init
→ first start
→ проверить QEMU Agent/SSH/health
```

Причина явного `protection=0`: base template `9000` защищён (`protection=1`), и protection может оказаться в конфигурации нового клона. Снимать protection с самого template ради клонирования запрещено.

`301-ai-control` сам не помещается в обычную self-managed write-зону только ради удобства: управляющий агент не должен случайно удалить или остановить VM, внутри которой работает.

## Повторяемое развёртывание внутри гостей

После появления `311-dev-services` целевой путь такой:

```text
Hermes
→ подготовить/изменить конфигурацию в Git
→ инициировать Ansible на 311-dev-services
→ Ansible подключается по SSH к нужному гостю
→ применяет конфигурацию
→ проверяется результат
```

Semaphore предназначен прежде всего для ручного запуска тех же Ansible-сценариев человеком и не является обязательным звеном между Hermes и Ansible.

Прямой SSH из `301` сохраняется: он нужен для первоначального bootstrap, диагностики, разовых операций и восстановления, когда стандартный deploy-сценарий ещё не готов или недоступен.

## Файлы и Git

До регистрации GitHub Deploy Key `301` работает без приватного репозитория.

После регистрации ключа Hermes клонирует:

```text
zsergeyru/proxmox
→ /opt/ai-control/repos/proxmox
```

После этого документация, `guest.yaml`, `rootfs/` и решения из Git становятся источником истины.

`ai-control` не хранит копии `rootfs/` других VM/LXC вне рабочего checkout репозитория. Повторяемое применение содержимого `rootfs/` выполняется Ansible из `311-dev-services` по правилам [`../guests/README.md`](../guests/README.md).

## Права и модель защиты

AI control получает достаточные guest-level права для поставленных задач, но граница проходит между управлением гостями и управлением самим PVE host.

Разрешён lifecycle выделенных VM/LXC в managed pool: создавать и клонировать, менять guest-level ресурсы, start/stop/reboot, делать snapshots/backups и выполнять другие разрешённые guest-level операции. При этом management identity не получает права на изменение PVE host network, storage configuration, ACL, пользователей, API tokens, Datacenter/host firewall, SDN, сертификатов, PVE repositories или reboot/shutdown самого гипервизора.

Защищённые объекты `100`, текущий bootstrap `320`, сам control plane `301` и template `9000` не должны автоматически попадать в обычную self-managed write-зону. Для `9000` нужен read/clone-доступ без права изменения template.

Полная матрица и destructive-action правила определены в [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md).

Основная защита:

1. Git — источник истины для повторяемой конфигурации и кода после регистрации Deploy Key.
2. Proxmox ACL/token — hard boundary возможностей Proximo.
3. Ограниченный `PROXIMO_TOOLSETS=pve.guests` — дополнительное уменьшение MCP surface.
4. Proxmox snapshots перед рискованными изменениями.
5. Регулярные Proxmox backups.
6. Отдельный backup persistent data и Docker volumes, если они появятся.
7. Логирование действий агента и Proximo audit log.
8. Проверка health/status/logs после изменений.
9. Поэтапная замена: сначала создать/проверить новое, затем удалять старое.

Секреты, приватные SSH-ключи, пароли и API tokens в Git не хранятся.

## Переход с 320 на 301

`320-ai-control` остаётся текущим bootstrap/legacy-узлом до фактической проверки новой схемы. Его установленный MCP может отличаться от целевого состояния `301`; это observed state, а не основание переносить старый MCP в новую VM.

Переход:

```text
9000 template готов
→ create-ai-control-vm.sh создаёт 301
→ install-ai-control.sh устанавливает Hermes/WebUI/Proximo и ключи
→ проверен Proximo
→ зарегистрирован GitHub Deploy Key
→ Hermes клонировал zsergeyru/proxmox в /opt/ai-control/repos/proxmox
→ проверено создание тестовой managed VM с Cloud-Init SSH key
→ разворачивается 311-dev-services
→ проверяется Ansible deploy
→ 301 становится основным control plane
→ 320 выводится из эксплуатации отдельным решением
```

Ansible и Semaphore остаются в `311-dev-services`, а не переносятся в `301-ai-control`.
