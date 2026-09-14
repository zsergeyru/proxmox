# Публичный bootstrap-репозиторий

## Назначение

Публичный репозиторий:

```text
https://github.com/zsergeyru/proxmox-bootstrap
```

содержит единственные канонические executable bootstrap-скрипты, которые можно скачать на чистый PVE или новую bootstrap-VM без доступа к приватному Git.

Приватный:

```text
https://github.com/zsergeyru/proxmox
```

остаётся source of truth для архитектуры, `guest.yaml`, `rootfs/`, политик и дальнейшего desired state.

Скрипты между двумя репозиториями не дублируются.

## Zero-day recovery chain

```text
чистый Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh
→ 301 ai-control
→ install-ai-control.sh внутри 301
→ Hermes + Dashboard + Proximo + SSH identities
→ вручную зарегистрировать GitHub Deploy Key
→ повторный install-ai-control.sh
→ clone zsergeyru/proxmox
→ дальнейшее развёртывание из Git
```

Приватный Git появляется только после того, как AI уже установлен. GitHub PAT для zero-day пути не нужен.

Подробности: [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

## Скрипты

### `create-template.sh`

Создаёт базовый Debian 13 template:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

Основные свойства:

- Debian 13 generic cloud image с SHA-512 verification;
- `ops` + locked password + `NOPASSWD sudo`;
- root password locked;
- SSH password auth disabled, public keys передаются клонам отдельно;
- QEMU Guest Agent и Cloud-Init;
- regular `linux-image-amd64` вместо cloud-kernel;
- VGA/noVNC `tty1` autologin `ops`;
- `serial0`/`ttyS0` как fallback;
- machine-id и SSH host keys очищаются перед template;
- `protection=1` включается только после успешной verification.

Запуск:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/create-template.sh \
  -o /root/create-template.sh
bash -n /root/create-template.sh
chmod +x /root/create-template.sh
/root/create-template.sh
```

### `create-ai-control-vm.sh`

**Реализован.** Выполняется от `root` на PVE. Отвечает только за уровень VM и host-side credential для Proximo:

```text
9000 tpl-debian13
→ Full Clone 301 ai-control
→ CPU/RAM/disk/network/Cloud-Init
→ protection=0
→ onboot=1
→ first start
→ QGA/cloud-init health
→ Proximo PVE user/token/ACL
→ передача token/config/CA внутрь 301
```

Он не устанавливает Hermes, не ставит Proximo package и не клонирует private Git.

Запуск:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/create-ai-control-vm.sh \
  -o /root/create-ai-control-vm.sh
bash -n /root/create-ai-control-vm.sh
chmod +x /root/create-ai-control-vm.sh
/root/create-ai-control-vm.sh
```

По умолчанию:

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

Если token существует, но его secret внутри `301` утрачен, автоматической ротации нет. Явное восстановление:

```bash
ROTATE_PROXIMO_TOKEN=1 /root/create-ai-control-vm.sh
```

### `install-ai-control.sh`

**Реализован.** Выполняется от `root` внутри `301`; обычно его удобно запускать через QEMU Guest Agent с PVE:

```bash
qm guest exec 301 -- /bin/bash -lc \
  'curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/install-ai-control.sh | bash'
```

Он создаёт:

```text
/opt/ai-control/
├── agents/
│   └── hermes/
│       └── hermes-agent/
├── mcp/
│   └── proximo/
├── ssh/
│   ├── ai_control_ed25519(.pub)
│   └── github_proxmox_ed25519(.pub)
└── repos/
    └── proxmox/
```

Hermes устанавливается строго в `agents/hermes`:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
```

Общий Proximo располагается отдельно:

```text
/opt/ai-control/mcp/proximo
```

Installer также:

- запускает `proximo doctor`;
- регистрирует Proximo как MCP Hermes;
- поднимает Hermes Dashboard на `tcp/9119` с auth;
- создаёт infrastructure SSH key;
- создаёт отдельный GitHub Deploy Key;
- показывает только публичные части ключей;
- после ручной регистрации Deploy Key клонирует `zsergeyru/proxmox` в `/opt/ai-control/repos/proxmox`.

### `bootstrap-ai-control.sh`

Старое имя сохранено как совместимая convenience-wrapper. Оно последовательно вызывает оба этапа. Для диагностики и recovery предпочтительнее использовать `create-ai-control-vm.sh` и `install-ai-control.sh` отдельно.

## GitHub onboarding

После первого `install-ai-control.sh` оператор получает:

```text
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Его нужно добавить:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Для возможности `commit/push` включить `Allow write access`.

После этого повторный `install-ai-control.sh` использует тот же private key, проверяет `git ls-remote` и завершает clone.

## Infrastructure SSH identity

Для управления будущими Debian VM используется:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся только в `301`. Public key передаётся новой VM через Cloud-Init пользователю `ops` до первого запуска.

## CI

GitHub Actions публичного repo проверяет:

```text
bash -n create-template.sh
bash -n create-ai-control-vm.sh
bash -n install-ai-control.sh
bash -n bootstrap-ai-control.sh
```

Для `create-template.sh` дополнительно проверяются встроенный Cloud-Init YAML и вложенные guest scripts.

CI не заменяет реальный PVE test.

## Live-test, который ещё требуется

До перевода `301` в production нужно проверить на реальном PVE:

```text
create-ai-control-vm.sh
→ правильный Full Clone 9000 → 301
→ install-ai-control.sh
→ Hermes в /opt/ai-control/agents/hermes
→ Dashboard + auth
→ proximo doctor
→ Deploy Key registration
→ clone private repo
→ Proximo создаёт test managed VM
→ ai_control_ed25519.pub передаётся через Cloud-Init
→ SSH 301 → test VM
```

Только после этой проверки `320-ai-control` можно выводить из эксплуатации.

## Template-Version 4

`v4` использует:

```text
vga: std
Proxmox Console → noVNC/VGA → tty1 → autologin ops
serial0: socket → ttyS0 → autologin ops (fallback)
```

Builder устанавливает regular Debian kernel и framebuffer, настраивает `Fixed 8x16`, проверяет QGA/console/SSH policy, затем очищает machine-specific state и делает template.

Base template остаётся:

```text
protection=1
```

Обычный рабочий Full Clone получает:

```text
protection=0
```

если паспорт гостя явно не требует обратного.

## Security boundary

Публичный `proxmox-bootstrap` не должен содержать:

- PAT/API tokens;
- пароли/PIN;
- private SSH/VPN/TLS keys;
- реальные `.env` с secrets;
- model-provider credentials.

Proximo token создаётся на месте на PVE и передаётся напрямую в `301`; private SSH keys создаются уже внутри `301`.

Полный backup `301` считается чувствительным объектом.
