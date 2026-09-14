# Публичный bootstrap-репозиторий

## Назначение

Публичный репозиторий:

```text
https://github.com/zsergeyru/proxmox-bootstrap
```

содержит канонические executable bootstrap-скрипты, которые можно скачать на чистый PVE или новую bootstrap-VM без доступа к приватному Git.

Приватный `zsergeyru/proxmox` остаётся source of truth для архитектуры, `guest.yaml`, `rootfs/`, политик и дальнейшего desired state. Скрипты между репозиториями не дублируются.

## Zero-day recovery chain

```text
чистый Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh
→ 301 ai-control
→ prepare-ai-control.sh внутри 301
→ Docker + Proximo + SSH/Git platform готова
→ install-ai-agent.sh --agent hermes
→ Hermes + Dashboard
→ вручную зарегистрировать GitHub Deploy Key
→ повторный install-ai-agent.sh --agent hermes
→ clone zsergeyru/proxmox
→ дальнейшее развёртывание из Git
```

Приватный Git появляется только после установки AI-агента. GitHub PAT для zero-day пути не нужен.

Подробная спецификация: [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

## `create-template.sh`

Создаёт базовый Debian 13 template:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

Template содержит Cloud-Init, QEMU Guest Agent, пользователя `ops`, locked passwords, SSH public-key auth, regular `linux-image-amd64`, VGA/noVNC + serial fallback и очищенные machine-specific identifiers. После успешной проверки template получает `protection=1`.

## `create-ai-control-vm.sh`

**Этап 1.** Выполняется от `root` на PVE.

```text
9000 tpl-debian13
→ Full Clone 301 ai-control
→ CPU/RAM/disk/network/Cloud-Init
→ protection=0
→ onboot=1
→ first start
→ QGA/cloud-init health
→ Proximo PVE user/token/ACL
→ token/config/PVE CA внутрь 301
```

Не устанавливает Docker, Proximo package или AI-агента.

Defaults:

```text
VMID:       301
Name:       ai-control
CPU:        2
RAM:        4096 MiB
Disk:       24 GiB
IPv4:       192.168.3.1/16
Gateway:    192.168.1.1
Bridge:     vmbr0
```

Proximo identity:

```text
proximo@pve!ai-control
privilege separation: enabled
managed pool: managed
```

Если token существует, но secret внутри `301` утрачен, ротация выполняется только явно:

```bash
ROTATE_PROXIMO_TOKEN=1 /root/create-ai-control-vm.sh
```

## `prepare-ai-control.sh`

**Этап 2.** Выполняется от `root` внутри `301` и готовит общую AI-платформу без установки конкретного агента.

Он устанавливает:

- Docker Engine;
- Docker Buildx;
- Docker Compose plugin;
- Git/OpenSSH/curl/jq/OpenSSL;
- Python 3/venv/pip и build tooling;
- общий Proximo MCP.

Создаёт:

```text
/opt/ai-control/
├── agents/        # пока может быть пуст
├── mcp/proximo/
├── ssh/
├── repos/
└── state/
```

Также:

- запускает `proximo doctor`;
- создаёт `ai_control_ed25519(.pub)`;
- создаёт `github_proxmox_ed25519(.pub)`;
- настраивает SSH/Git для `ops`;
- выводит public keys;
- **не клонирует private Git и не устанавливает Hermes**.

Пример запуска с PVE:

```bash
qm guest exec 301 -- /bin/bash -lc \
  'curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/prepare-ai-control.sh | bash'
```

## `install-ai-agent.sh`

**Этап 3.** Выполняется внутри `301` и устанавливает только выбранного AI-агента:

```bash
install-ai-agent.sh --agent hermes
```

Общее правило:

```text
/opt/ai-control/agents/<agent>/
```

Сейчас реализован installer для `hermes`. Добавление следующего агента не должно требовать изменений `create-ai-control-vm.sh` или `prepare-ai-control.sh`.

Для Hermes:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
Dashboard=tcp/9119
```

Третий этап подключает уже существующий общий Proximo к агенту, настраивает его WebUI/systemd и выполняет Git onboarding **только после того, как AI уже установлен**.

Если GitHub Deploy Key ещё не зарегистрирован, скрипт показывает тот же `.pub`. После регистрации повторный запуск использует существующий agent/config/keys и клонирует:

```text
/opt/ai-control/repos/proxmox
```

## Почему Docker находится на этапе 2

Docker — свойство общей VM `301`, а не Hermes. Это позволяет одному и тому же подготовленному control plane устанавливать разные агенты, включая контейнерные, без переустановки общей платформы.

Штатно должны работать:

```text
docker version
docker compose version
systemctl is-active docker
```

## GitHub onboarding

Deploy Key создаётся на этапе 2:

```text
/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Private key остаётся в `301`. `.pub` вручную добавляется в:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Для `commit/push` включается `Allow write access`.

Сам clone выполняется уже этапом 3 после установки агента.

## Infrastructure SSH identity

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся только в `301`. Public key передаётся новой managed VM через Cloud-Init пользователю `ops` до первого запуска.

## Convenience wrapper

`bootstrap-ai-control.sh` остаётся удобной обёрткой и последовательно запускает:

```text
create-ai-control-vm.sh
→ prepare-ai-control.sh
→ install-ai-agent.sh --agent ${AI_AGENT:-hermes}
```

Для диагностики/recovery предпочтительно запускать три этапа отдельно.

## CI

GitHub Actions проверяет Bash-синтаксис:

```text
create-template.sh
create-ai-control-vm.sh
prepare-ai-control.sh
install-ai-agent.sh
bootstrap-ai-control.sh
```

Для template builder дополнительно валидируются встроенный Cloud-Init YAML и guest scripts.

## Live-test

До production-перехода на `301` проверить:

```text
create-ai-control-vm.sh
→ Full Clone 9000 → 301
→ prepare-ai-control.sh
→ Docker + Compose
→ Proximo doctor
→ install-ai-agent.sh --agent hermes
→ Hermes в /opt/ai-control/agents/hermes
→ Dashboard + auth
→ Deploy Key + private repo clone
→ Proximo создаёт test managed VM
→ ai_control_ed25519.pub через Cloud-Init
→ SSH 301 → test VM
```

До этой проверки `320-ai-control` остаётся bootstrap/recovery узлом.

## Security boundary

Публичный `proxmox-bootstrap` не должен содержать PAT/API tokens, passwords/PIN, private SSH/VPN/TLS keys, реальные `.env` с secrets или model-provider credentials.

Proximo token создаётся на PVE и передаётся напрямую в `301`; private SSH keys создаются внутри `301`. Полный backup `301` считается чувствительным объектом.
