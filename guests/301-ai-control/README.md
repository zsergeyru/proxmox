# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; ещё не заменяет текущий `320-ai-control` до завершения live-проверки.

`301-ai-control` является целевой VM центрального AI-контура управления домашней инфраструктурой.

## Состав

```text
/opt/ai-control/
├── agents/
│   └── hermes/
│       ├── hermes-agent/          # код Hermes
│       └── ...                    # HERMES_HOME / persistent state
├── mcp/
│   └── proximo/
├── ssh/
│   ├── ai_control_ed25519(.pub)
│   └── github_proxmox_ed25519(.pub)
└── repos/
    └── proxmox/
```

Ключевое правило: конкретный AI-агент всегда устанавливается в `/opt/ai-control/agents/<agent>/`; общий MCP не размещается внутри каталога агента.

Базовые механизмы управления:

```text
Proximo MCP        → VM/LXC, guest resources/config, lifecycle, snapshots/backups
Ansible на 311     → повторяемый deploy внутри гостевых ОС через SSH
прямой SSH         → bootstrap, диагностика, разовые и аварийные действия
```

Ansible и Semaphore не размещаются внутри `301-ai-control`: они относятся к `311-dev-services`. Отдельный универсальный `deploy-mcp` не используется.

## Zero-day создание

`301` не требует существующего AI или доступа к приватному Git-репозиторию.

Канонический bootstrap разделён на два этапа:

```text
PVE + 9000 tpl-debian13
→ create-ai-control-vm.sh на PVE
→ Full Clone 9000 → 301 + Proximo PVE identity
→ базовая VM 301 готова
→ install-ai-control.sh внутри 301
→ Hermes + WebUI + Proximo
→ сгенерировать SSH identities
→ показать GitHub Deploy Key public key
→ оператор регистрирует Deploy Key
→ повторный install-ai-control.sh
→ clone zsergeyru/proxmox
```

GitHub PAT для этого пути не нужен. Executable-скрипты хранятся только в публичном `zsergeyru/proxmox-bootstrap`.

Подробная спецификация: [`../../docs/ai-control-bootstrap.md`](../../docs/ai-control-bootstrap.md).

## `create-ai-control-vm.sh`

Первый скрипт запускается на PVE от `root` и отвечает за:

- Full Clone `9000 → 301`;
- CPU/RAM/disk/network/Cloud-Init;
- `protection=0`, `onboot=1`;
- first boot и QEMU Guest Agent/cloud-init health;
- создание ограниченной PVE management identity для Proximo;
- передачу Proximo token secret, config и PVE CA внутрь `301` через QGA.

Он **не устанавливает Hermes**, не ставит пакет Proximo и не клонирует приватный Git.

## `install-ai-control.sh`

Второй скрипт запускается внутри `301` и отвечает за:

- Hermes code: `/opt/ai-control/agents/hermes/hermes-agent/`;
- `HERMES_HOME=/opt/ai-control/agents/hermes`;
- штатный Hermes Dashboard на `tcp/9119`;
- Proximo в `/opt/ai-control/mcp/proximo/`;
- `proximo doctor` до подключения MCP к Hermes;
- infrastructure SSH key;
- GitHub Deploy Key;
- Git onboarding после ручной регистрации `.pub`.

## SSH identities

Внутри `301` создаются две независимые пары:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
→ SSH в будущие managed Debian VM

/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
→ только private repo zsergeyru/proxmox
```

Private keys остаются только внутри `301` и делают backup этой VM чувствительным объектом.

При создании новой обычной Debian VM Hermes передаёт `ai_control_ed25519.pub` пользователю `ops` через Cloud-Init до первого запуска. Private key никуда не передаётся.

## Proximo

Для `301` канонический Proxmox MCP — **Proximo** (`proximo-proxmox`). Host-side bootstrap создаёт отдельную privilege-separated management identity `proximo@pve!ai-control`, а installer внутри `301` устанавливает сам Proximo.

По умолчанию Hermes получает сокращённую tool surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

Hard boundary задаётся Proxmox ACL. Обычный AI workflow не получает прав на host network, storage definitions, IAM/ACL, SDN, PVE repositories/certificates или reboot/shutdown самого гипервизора.

`301` не помещается в обычную self-managed write-зону, чтобы управляющий агент не мог случайно остановить или удалить собственную VM.

## Git после bootstrap

После первого запуска `install-ai-control.sh` выводит публичную часть:

```text
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Оператор вручную регистрирует её для:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Если Hermes должен делать `commit/push`, Deploy Key получает `Allow write access`.

После регистрации ключа повторный `install-ai-control.sh` проверяет `git ls-remote` и клонирует:

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

До развёртывания `311` прямой SSH из `301` допустим для bootstrap необходимых гостей.

## Критерии готовности 301

`301` можно считать готовым заменить `320` только после проверки:

1. `create-ai-control-vm.sh` создал корректный Full Clone `301` из `9000`;
2. Hermes находится именно в `/opt/ai-control/agents/hermes/`;
3. Hermes Dashboard доступен и защищён авторизацией;
4. model/provider настроен;
5. Proximo находится в `/opt/ai-control/mcp/proximo/`;
6. `proximo doctor` подтверждает ожидаемую границу прав;
7. GitHub Deploy Key зарегистрирован и private repo клонируется в `/opt/ai-control/repos/proxmox`;
8. Hermes через Proximo создаёт тестовый Full Clone из `9000` в `managed` pool;
9. `/opt/ai-control/ssh/ai_control_ed25519.pub` передаётся тестовой VM через Cloud-Init;
10. `301` успешно входит по SSH как `ops` в тестовую VM;
11. затем развёрнут и проверен `311-dev-services`/Ansible.

`320-ai-control` выводится из эксплуатации только после успешной проверки этой цепочки.

Подробности: [`../../docs/ai-control-bootstrap.md`](../../docs/ai-control-bootstrap.md), [`../../docs/ai-control.md`](../../docs/ai-control.md), [`../../docs/bootstrap.md`](../../docs/bootstrap.md) и [`../311-dev-services/decisions/001-deployment-tooling.md`](../311-dev-services/decisions/001-deployment-tooling.md).
