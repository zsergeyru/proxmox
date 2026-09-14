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

На новом сервере до появления AI приватный Git не нужен. Каноническая цепочка:

```text
Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh на PVE
→ 301 ai-control
→ install-ai-control.sh внутри 301
→ Hermes + Dashboard + Proximo + SSH identities
→ вручную зарегистрировать GitHub Deploy Key
→ повторный install-ai-control.sh
→ clone zsergeyru/proxmox
→ дальнейшее развёртывание из Git
```

Executable-скрипты хранятся только в публичном `zsergeyru/proxmox-bootstrap`. Подробности: [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

GitHub PAT для zero-day пути не нужен.

## Каталоги 301

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   │   └── hermes-agent/
│   └── <future-agent>/
├── mcp/
│   ├── proximo/
│   └── <future-mcp>/
├── ssh/
│   ├── ai_control_ed25519(.pub)
│   └── github_proxmox_ed25519(.pub)
└── repos/
    └── proxmox/
```

Главное правило:

> Конкретный AI-агент устанавливается в `/opt/ai-control/agents/<agent>/`. Общие MCP располагаются отдельно в `/opt/ai-control/mcp/`.

Поэтому Hermes не устанавливается прямо в `/opt/ai-control`, `/home/ops` или каталог MCP.

## Hermes

Основной агент — Hermes.

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
```

Штатный Dashboard запускается как systemd service на `tcp/9119` и защищается локальными credentials. Model/provider credential задаётся после первого запуска и не хранится в Git.

## Proximo

Канонический Proxmox MCP для `301` — **Proximo** (`proximo-proxmox`).

```text
/opt/ai-control/mcp/proximo/
```

Host-side bootstrap создаёт отдельную privilege-separated identity:

```text
user:  proximo@pve
token: proximo@pve!ai-control
```

Token secret передаётся непосредственно PVE → `301` через QEMU Guest Agent и хранится в:

```text
/etc/ai-control/secrets/proximo-pve-token
```

Конфигурация и PVE CA:

```text
/etc/ai-control/proximo/proximo.env
/etc/ai-control/proximo/pve-root-ca.pem
```

Для уменьшения MCP surface используется:

```text
PROXIMO_TOOLSETS=pve.guests
```

Перед подключением к Hermes выполняется `proximo doctor`. Окончательная граница полномочий задаётся PVE token/ACL: AI не должен получать штатные права на host network, IAM/ACL, SDN, storage definitions, certificates/repositories или reboot/shutdown PVE.

`301` не включается в собственную обычную self-managed write-зону.

## SSH identities

### Infrastructure key

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся в `301`. При создании новой managed Debian VM Hermes передаёт `.pub` пользователю `ops` через Cloud-Init **до первого запуска**.

После этого:

```text
301 Hermes
→ SSH ops@guest
→ /opt/ai-control/ssh/ai_control_ed25519
```

Прямой SSH нужен для bootstrap, diagnostics, one-off и emergency операций. После появления `311` повторяемые изменения идут через Ansible.

### GitHub Deploy Key

```text
/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Ключ используется только для `zsergeyru/proxmox`. Installer показывает только `.pub`; оператор вручную добавляет его в GitHub Deploy keys. Для `commit/push` включается `Allow write access`.

После регистрации Git checkout находится в:

```text
/opt/ai-control/repos/proxmox
```

## Создание managed Debian VM

Базовый порядок:

```text
Full Clone from 9000
→ guest должен попасть в managed pool
→ protection=0, если guest.yaml не требует protection: true
→ CPU/RAM/disk/network
→ ciuser=ops
→ sshkeys=/opt/ai-control/ssh/ai_control_ed25519.pub
→ qm/cloud-init update через разрешённый Proxmox API workflow
→ first start
→ проверить QEMU Agent, SSH и health
```

Template `9000` остаётся `protection=1`; AI имеет право его видеть/клонировать, но не изменять или удалять.

## Git и дальнейшее развёртывание

После регистрации Deploy Key приватный `zsergeyru/proxmox` становится source of truth.

До появления `311` Hermes может выполнять bootstrap необходимых гостей напрямую по SSH. После развёртывания `311-dev-services` штатный repeatable flow:

```text
Hermes
→ подготовить/изменить конфигурацию в Git
→ инициировать Ansible на 311
→ Ansible по SSH применяет desired state
→ проверить результат
```

Semaphore остаётся ручным WebUI к Ansible и не является обязательным посредником между Hermes и Ansible.

## Что не размещается в 301

- DNS/VPN/PBR → `109-network-gateway`;
- MQTT/Zigbee2MQTT/ESPHome → `211-automation-services`;
- Ansible/Semaphore/Git/CI services → `311-dev-services`;
- обычные приложения → `321-app-services`;
- STT/TTS → `331-ai-services`;
- monitoring → `401-monitoring`;
- Frigate → `501-frigate`.

Git-клиент и рабочий checkout `zsergeyru/proxmox` внутри `301` допустимы; отдельный Git service там не разворачивается.

## Security boundary

Основные уровни защиты:

1. PVE privilege-separated token + ACL — hard boundary.
2. `PROXIMO_TOOLSETS=pve.guests` — уменьшение MCP surface.
3. `301`, production HA и template не входят автоматически в self-managed write-zone.
4. Private SSH keys и provider credentials не хранятся в Git.
5. Snapshots/backups перед рискованными изменениями.
6. Proximo audit log и проверка health после действий.
7. Сначала создать и проверить замену, затем удалять старое.

Полная матрица PVE-прав: [`../guests/320-ai-control/decisions/002-proxmox-permissions.md`](../guests/320-ai-control/decisions/002-proxmox-permissions.md).

## Готовность к замене 320

`301` становится основным control plane только после live-проверки всей цепочки:

```text
create-ai-control-vm.sh
→ install-ai-control.sh
→ Hermes Dashboard
→ proximo doctor
→ GitHub Deploy Key
→ clone private repo
→ Proximo создаёт тестовую VM из 9000
→ infrastructure .pub попадает в Cloud-Init
→ SSH 301 → test VM как ops
→ 311/Ansible управляет тестовым гостем
```

До этого `320-ai-control` не удаляется.
