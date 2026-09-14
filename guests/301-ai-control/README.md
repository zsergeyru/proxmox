# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; ещё не заменяет текущий `320-ai-control` до завершения live-проверки.

`301-ai-control` является целевой VM центрального AI-контура управления домашней инфраструктурой.

## Состав

```text
/opt/ai-control/
├── agents/
│   └── hermes/
│       └── Hermes + штатный WebUI
├── mcp/
│   └── proximo/
├── repos/
│   └── proxmox/
└── state/
```

Базовые механизмы управления:

```text
Proximo MCP        → VM/LXC, guest resources/config, lifecycle, snapshots/backups
Ansible на 311     → повторяемый deploy внутри гостевых ОС через SSH
прямой SSH         → bootstrap, диагностика, разовые и аварийные действия
```

Ansible и Semaphore не размещаются внутри `301-ai-control`: они относятся к `311-dev-services`. Отдельный универсальный `deploy-mcp` не используется.

## Zero-day создание

`301` не требует существующего AI или доступа к приватному Git-репозиторию.

Bootstrap разделён на два этапа:

```text
PVE + 9000 tpl-debian13
→ create-ai-control-vm.sh на PVE
→ Full Clone 9000 → 301
→ базовая VM 301 готова
→ install-ai-control.sh внутри 301
→ Hermes + WebUI + Proximo
→ сгенерировать SSH identities
→ показать GitHub Deploy Key public key
→ оператор регистрирует Deploy Key
→ повторная проверка installer
→ Hermes клонирует zsergeyru/proxmox
```

GitHub PAT для этого пути не нужен.

Подробная спецификация: [`../../docs/ai-control-bootstrap.md`](../../docs/ai-control-bootstrap.md).

## Разделение скриптов

### `create-ai-control-vm.sh`

Первый скрипт запускается на PVE от `root` и отвечает за:

- Full Clone `9000 → 301`;
- CPU/RAM/disk/network/Cloud-Init;
- `protection=0`, `onboot=1`;
- first boot и QEMU Guest Agent/cloud-init health;
- создание ограниченной PVE management identity для Proximo;
- безопасную передачу Proximo token secret внутрь `301`.

Он не устанавливает Hermes и не клонирует приватный Git.

### `install-ai-control.sh`

Второй скрипт запускается внутри `301` и отвечает за:

- `/opt/ai-control/agents/hermes/`;
- Hermes и штатный WebUI;
- `/opt/ai-control/mcp/proximo/`;
- Proximo и `proximo doctor`;
- infrastructure SSH key;
- GitHub Deploy Key;
- Git onboarding после ручной регистрации `.pub`.

Ключевое правило проекта: конкретный AI-агент всегда устанавливается в `/opt/ai-control/agents/<agent>/`; общий MCP не размещается внутри каталога агента.

## SSH identities

Внутри `301` создаются две независимые пары:

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
→ SSH в будущие managed Debian VM

/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
→ только private repo zsergeyru/proxmox
```

Private keys остаются только внутри `301` и входят в чувствительные данные backup этой VM.

При создании новой обычной Debian VM Hermes передаёт `ai_control_ed25519.pub` пользователю `ops` через Cloud-Init до первого запуска.

## Proximo

Для `301` канонический Proxmox MCP — **Proximo** (`proximo-proxmox`). Host-side bootstrap создаёт отдельную privilege-separated management identity `proximo@pve!ai-control`, а installer внутри `301` устанавливает и конфигурирует сам Proximo.

По умолчанию Hermes видит только:

```text
PROXIMO_TOOLSETS=pve.guests
```

Hard boundary задаётся Proxmox ACL. Обычный AI workflow не получает прав на host network, storage definitions, IAM/ACL, SDN, PVE repositories/certificates или reboot/shutdown самого гипервизора.

`301` не помещается в обычную self-managed write-зону, чтобы управляющий агент не мог случайно остановить или удалить собственную VM.

## Git после bootstrap

После первого запуска `install-ai-control.sh` выводит только публичную часть GitHub Deploy Key.

Оператор вручную регистрирует её для:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Если Hermes должен делать `commit/push`, Deploy Key получает `Allow write access`.

После регистрации ключа повторная проверка installer выполняет GitHub SSH test и клонирует:

```text
zsergeyru/proxmox
→ /opt/ai-control/repos/proxmox
```

После этого приватный репозиторий становится источником истины для дальнейшего развёртывания.

Целевой сценарий повторяемого изменения:

```text
Hermes
→ изменить/подготовить конфигурацию в Git
→ инициировать Ansible на 311-dev-services
→ Ansible по SSH применяет изменение на нужном госте
→ проверить результат
```

Semaphore служит ручным web-интерфейсом к Ansible для пользователя и не является обязательным звеном между Hermes и Ansible.

До развёртывания `311` прямой SSH из `301` допустим для bootstrap необходимых гостей.

## Критерии готовности 301

`301` можно считать готовым заменить `320` только после проверки:

1. `create-ai-control-vm.sh` создал корректный Full Clone `301` из `9000`;
2. Hermes установлен именно в `/opt/ai-control/agents/hermes`;
3. Hermes WebUI доступен и защищён поддерживаемой авторизацией;
4. model/provider настроен;
5. Proximo установлен в `/opt/ai-control/mcp/proximo`;
6. `proximo doctor` подтверждает ожидаемую границу прав;
7. GitHub Deploy Key зарегистрирован и private repo клонируется в `/opt/ai-control/repos/proxmox`;
8. Hermes через Proximo создаёт тестовый Full Clone из `9000` в `managed` pool;
9. public infrastructure key передаётся тестовой VM через Cloud-Init;
10. `301` успешно входит по SSH как `ops` в тестовую VM;
11. затем развёрнут и проверен `311-dev-services`/Ansible.

`320-ai-control` выводится из эксплуатации только после успешной проверки этой цепочки.

Подробности: [`../../docs/ai-control-bootstrap.md`](../../docs/ai-control-bootstrap.md), [`../../docs/ai-control.md`](../../docs/ai-control.md), [`../../docs/bootstrap.md`](../../docs/bootstrap.md) и [`../311-dev-services/decisions/001-deployment-tooling.md`](../311-dev-services/decisions/001-deployment-tooling.md).
