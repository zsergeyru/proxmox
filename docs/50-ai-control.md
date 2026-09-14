# AI Control — архитектурные принципы

`301-ai-control` — целевая VM центрального AI-контура. `320-ai-control` сохраняется как bootstrap/recovery узел до завершения live-проверки `301`.

## Разделение ответственности

```text
Proximo MCP
→ VM/LXC lifecycle, guest-level config, snapshots/backups и Proxmox diagnostics

Ansible на 311-dev-services
→ повторяемая конфигурация ОС и приложений внутри гостей через SSH

прямой SSH из 301
→ bootstrap, диагностика, разовые и аварийные действия
```

Ansible/Semaphore не размещаются в `301`; они относятся к `311-dev-services`.

## Zero-day bootstrap

Каноническая цепочка разделена на три независимых уровня:

```text
Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh
→ 301 ai-control
→ prepare-ai-control.sh
→ общая AI-платформа: Docker + Proximo + SSH/Git tooling
→ install-ai-agent.sh --agent hermes
→ Hermes + Dashboard
→ GitHub Deploy Key registration
→ повторный install-ai-agent.sh --agent hermes
→ clone zsergeyru/proxmox
→ дальнейшее развёртывание из Git
```

Executable-скрипты хранятся только в публичном `zsergeyru/proxmox-bootstrap`. Подробности: [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

GitHub PAT для zero-day пути не нужен.

## Общая структура 301

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── ssh/
│   ├── ai_control_ed25519(.pub)
│   └── github_proxmox_ed25519(.pub)
├── repos/
│   └── proxmox/
└── state/
```

Главные правила:

> Конкретный AI-агент устанавливается только в `/opt/ai-control/agents/<agent>/`.

> Общие runtime/capabilities — Docker, Proximo, SSH identities и Git checkout — не принадлежат Hermes и не дублируются внутри каталога агента.

## Общая AI-платформа

`prepare-ai-control.sh` готовит VM независимо от выбранного агента. Он устанавливает Docker Engine, Docker Compose plugin, Git/OpenSSH, Python runtime, общий Proximo, создаёт обе SSH identity и проверяет `proximo doctor`.

Docker является свойством `301-ai-control`, а не конкретного агента. Поэтому будущий контейнерный агент можно установить без изменения PVE bootstrap и без переустановки общей платформы.

## Агенты

Установка выполняется отдельным вызовом:

```bash
install-ai-agent.sh --agent <agent>
```

Сейчас реализован `hermes`. Архитектура допускает добавление `agent-zero` или другого агента без изменения первых двух этапов.

Hermes:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
Dashboard=tcp/9119
```

Только agent-installer знает, как подключить общий Proximo к конкретному AI-клиенту и как поднять его WebUI/service.

## Proximo

Канонический Proxmox MCP для `301` — **Proximo** (`proximo-proxmox`):

```text
/opt/ai-control/mcp/proximo/
```

Host-side bootstrap создаёт privilege-separated identity:

```text
proximo@pve!ai-control
```

Для уменьшения MCP surface используется:

```text
PROXIMO_TOOLSETS=pve.guests
```

Hard boundary задаётся PVE token/ACL. `301` не получает штатных прав на host network, IAM/ACL, SDN, storage definitions, certificates/repositories или reboot/shutdown PVE и не включается в собственную self-managed write-зону.

## SSH identities

Infrastructure key:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся в `301`; `.pub` передаётся managed Debian VM через Cloud-Init пользователю `ops` до первого запуска.

GitHub Deploy Key:

```text
/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Он используется только для `zsergeyru/proxmox`. `.pub` регистрируется вручную в Deploy keys; для `commit/push` включается `Allow write access`.

Private Git намеренно не клонируется на этапе подготовки платформы. Clone происходит уже после установки AI-агента:

```text
/opt/ai-control/repos/proxmox
```

## Создание managed Debian VM

```text
Full Clone from 9000
→ managed pool
→ protection=0, если guest.yaml не требует protection: true
→ CPU/RAM/disk/network
→ ciuser=ops
→ sshkeys=/opt/ai-control/ssh/ai_control_ed25519.pub
→ first start
→ проверить QEMU Agent, SSH и health
```

Template `9000` остаётся `protection=1`; AI имеет право его видеть/клонировать, но не изменять или удалять.

## Дальнейшее развёртывание

После регистрации Deploy Key приватный `zsergeyru/proxmox` становится source of truth.

До появления `311` агент может выполнять bootstrap напрямую по SSH. После развёртывания `311-dev-services` штатный repeatable flow:

```text
AI agent
→ подготовить/изменить конфигурацию в Git
→ инициировать Ansible на 311
→ Ansible по SSH применяет desired state
→ проверить результат
```

## Security boundary

1. PVE privilege-separated token + ACL — hard boundary.
2. `PROXIMO_TOOLSETS=pve.guests` — уменьшение MCP surface.
3. Common platform отделена от agent-specific software.
4. Private SSH keys и provider credentials не хранятся в Git.
5. Snapshots/backups используются перед рискованными изменениями.
6. `301`, production HA и template не входят автоматически в self-managed write-zone.

Полная матрица PVE-прав: [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md).

## Готовность к замене 320

```text
create-ai-control-vm.sh
→ prepare-ai-control.sh
→ Docker + Proximo doctor
→ install-ai-agent.sh --agent hermes
→ Hermes Dashboard
→ GitHub Deploy Key
→ clone private repo
→ test managed VM из 9000
→ Cloud-Init infrastructure .pub
→ SSH 301 → test VM
→ 311/Ansible
```

До успешной live-проверки этой цепочки `320-ai-control` не удаляется.
