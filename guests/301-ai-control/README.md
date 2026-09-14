# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; `320-ai-control` сохраняется до завершения live-проверки.

## Архитектура каталогов

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── ssh/
│   ├── ai_control_ed25519(.pub)
│   └── github_proxmox_repo_ed25519(.pub)
├── repos/
│   └── proxmox/
└── state/
```

Ключевое правило:

> Конкретный AI-агент всегда устанавливается в `/opt/ai-control/agents/<agent>/`. Docker, Proximo, SSH identities и Git checkout являются общими компонентами `301` и не принадлежат Hermes.

## Три этапа zero-day bootstrap

```text
1. create-ai-control-vm.sh
   PVE → создать и настроить VM 301 + Proximo PVE identity

2. prepare-ai-control.sh
   внутри 301 → Docker + common tooling + Proximo + SSH identities

3. install-ai-agent.sh --agent <agent>
   внутри 301 → установить выбранный AI-агент и его UI/integration
```

Это позволяет заменить Hermes другим агентом или установить несколько агентов, не пересоздавая VM и не переустанавливая общую платформу.

## Этап 1 — VM

`create-ai-control-vm.sh`:

- Full Clone `9000 → 301`;
- CPU/RAM/disk/network/Cloud-Init;
- `protection=0`, `onboot=1`;
- first boot + QGA/cloud-init health;
- отдельный `proximo@pve!ai-control` token/ACL;
- передача Proximo token/config/PVE CA внутрь `301`.

Он не устанавливает Docker или AI software.

## Этап 2 — общая AI-платформа

`prepare-ai-control.sh` устанавливает независимо от агента:

- Docker Engine;
- Docker Buildx;
- Docker Compose plugin;
- Git/OpenSSH/curl/jq/OpenSSL;
- Python/venv/pip;
- Proximo в `/opt/ai-control/mcp/proximo`.

Он также создаёт:

```text
/opt/ai-control/ssh/ai_control_ed25519(.pub)
/opt/ai-control/ssh/github_proxmox_repo_ed25519(.pub)
```

и выполняет `proximo doctor`.

После этого этапа `/opt/ai-control/agents/` **может быть пустым** — VM уже подготовлена как reusable AI platform, но конкретный агент ещё не выбран.

## Этап 3 — конкретный агент

Вызов:

```bash
install-ai-agent.sh --agent hermes
```

Сейчас реализован installer Hermes. Будущие агенты добавляются в третий этап без изменения первых двух.

Hermes устанавливается строго в:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
Dashboard=tcp/9119
```

Agent installer подключает уже существующий общий Proximo как MCP и настраивает agent-specific WebUI/systemd/credentials.

## Proximo

Канонический Proxmox MCP — **Proximo** (`proximo-proxmox`):

```text
/opt/ai-control/mcp/proximo/
```

Используется ограниченная surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

Hard boundary задаётся PVE ACL. `301` не получает прав на host network, IAM/ACL, SDN, storage definitions, repositories/certificates или reboot/shutdown самого PVE и не входит в собственную self-managed write-зону.

## SSH identities

Infrastructure identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Public key передаётся будущим managed Debian VM через Cloud-Init пользователю `ops`. Private key остаётся только в `301`.

GitHub Deploy Key:

```text
/opt/ai-control/ssh/github_proxmox_repo_ed25519
/opt/ai-control/ssh/github_proxmox_repo_ed25519.pub
```

`.pub` вручную регистрируется в `zsergeyru/proxmox → Settings → Deploy keys`. Для `commit/push` включается `Allow write access`.

## Git появляется после AI

`prepare-ai-control.sh` генерирует GitHub key, но private repo не клонирует.

После установки агента `install-ai-agent.sh` проверяет Deploy Key. Если доступ уже зарегистрирован:

```text
git@github.com:zsergeyru/proxmox.git
→ /opt/ai-control/repos/proxmox
```

Если ключ ещё не зарегистрирован, installer показывает `.pub`; после регистрации запускается повторно и использует те же agent/config/keys.

## Механизмы управления

```text
Proximo MCP        → VM/LXC lifecycle, guest config, snapshots/backups
Ansible на 311     → repeatable deploy внутри гостевых ОС
прямой SSH         → bootstrap, diagnostics, one-off и recovery
```

Ansible/Semaphore не размещаются внутри `301`.

## Критерии готовности

До замены `320` проверить:

1. корректный Full Clone `9000 → 301`;
2. `prepare-ai-control.sh` оставляет рабочие Docker и Compose;
3. Proximo находится в `/opt/ai-control/mcp/proximo` и `doctor` показывает ожидаемую границу;
4. обе SSH identity созданы;
5. `install-ai-agent.sh --agent hermes` устанавливает Hermes только в `/opt/ai-control/agents/hermes`;
6. Dashboard работает и защищён auth;
7. Deploy Key зарегистрирован и private repo клонируется;
8. Proximo создаёт test managed VM из `9000`;
9. `ai_control_ed25519.pub` попадает в Cloud-Init тестовой VM;
10. `301` входит в неё по SSH как `ops`;
11. затем проверен `311-dev-services`/Ansible.

Подробности: [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md), [`../../docs/50-ai-control.md`](../../docs/50-ai-control.md), [`../../docs/31-bootstrap.md`](../../docs/31-bootstrap.md).
