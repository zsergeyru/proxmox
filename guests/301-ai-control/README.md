# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; ещё не заменяет текущий `320-ai-control` до завершения live-проверки.

`301-ai-control` является целевой VM центрального AI-контура управления домашней инфраструктурой.

## Состав

```text
Hermes Agent
├── встроенный Web Dashboard :9119
├── Proximo MCP (stdio)
├── Git client
├── infrastructure SSH identity
└── GitHub Deploy Key identity
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

Канонический путь:

```text
PVE + 9000 tpl-debian13
→ public zsergeyru/proxmox-bootstrap/bootstrap-ai-control.sh
→ Full Clone 9000 → 301
→ bootstrap через QEMU Guest Agent
→ Hermes + Dashboard + Proximo
→ сгенерировать SSH identities
→ показать GitHub Deploy Key public key
→ оператор регистрирует Deploy Key
→ Hermes клонирует zsergeyru/proxmox
```

GitHub PAT для этого пути не нужен.

## SSH identities

Внутри `301` создаются две независимые пары:

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
→ SSH в будущие managed Debian VM

/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
→ только git@github.com:zsergeyru/proxmox.git
```

Private keys остаются только внутри `301` и входят в чувствительные данные backup этой VM.

При создании новой обычной Debian VM Hermes передаёт `ai_control_ed25519.pub` пользователю `ops` через Cloud-Init до первого запуска.

## Proximo

Для `301` канонический Proxmox MCP — **Proximo** (`proximo-proxmox`). Bootstrap создаёт отдельную privilege-separated management identity `proximo@pve!ai-control`.

По умолчанию Hermes видит только:

```text
PROXIMO_TOOLSETS=pve.guests
```

Hard boundary задаётся Proxmox ACL. Обычный AI workflow не получает прав на host network, storage definitions, IAM/ACL, SDN, PVE repositories/certificates или reboot/shutdown самого гипервизора.

## Git после bootstrap

После регистрации Deploy Key Hermes клонирует:

```text
git@github.com:zsergeyru/proxmox.git
→ /home/ops/proxmox
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

1. Hermes Dashboard доступен и защищён авторизацией;
2. model/provider настроен;
3. `proximo doctor` подтверждает ожидаемую границу прав;
4. GitHub Deploy Key зарегистрирован и private repo клонируется;
5. Hermes через Proximo создаёт тестовый Full Clone из `9000` в `managed` pool;
6. public infrastructure key передаётся тестовой VM через Cloud-Init;
7. `301` успешно входит по SSH как `ops` в тестовую VM;
8. затем развёрнут и проверен `311-dev-services`/Ansible.

`320-ai-control` выводится из эксплуатации только после успешной проверки этой цепочки.

Подробности: [`../../docs/ai-control.md`](../../docs/ai-control.md), [`../../docs/bootstrap.md`](../../docs/bootstrap.md) и [`../311-dev-services/decisions/001-deployment-tooling.md`](../311-dev-services/decisions/001-deployment-tooling.md).
