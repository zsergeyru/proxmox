# ADR 003 — Proximo MCP для AI control

## Статус

**Актуально.**

- `320-ai-control`: уже переведён на **Proximo** (`proximo-proxmox`).
- `301-ai-control`: целевой канонический MCP — **Proximo** (`proximo-proxmox`).

Таким образом, для текущего bootstrap control plane и будущего основного control plane используется один и тот же Proximo MCP.

## Контекст

`ai-control` должен уметь создавать и сопровождать управляемые VM/LXC, но не должен получать обычный административный доступ к самому PVE host.

Нужны:

- Proxmox API вместо shell на гипервизоре;
- privilege-separated API token;
- видимая/проверяемая граница разрешений;
- guest provisioning/configuration, включая Cloud-Init;
- snapshots/backups/lifecycle;
- возможность уменьшить MCP tool surface;
- audit trail для изменений.

## Каноническое решение

Для `320-ai-control` и `301-ai-control` используется:

```text
Proximo / proximo-proxmox
```

Для целевого `301` установка выполняется zero-day bootstrap-скриптом из публичного `zsergeyru/proxmox-bootstrap`.

Целевая локальная схема `301`:

```text
Hermes
  │ stdio MCP
  ▼
Proximo
  │ HTTPS Proxmox API
  ▼
PVE
```

Отдельный HTTP MCP daemon для одного локального Hermes в `301` не нужен. Если позже появится реальная необходимость в общем сетевом MCP для нескольких клиентов, это оформляется отдельным решением.

## Management identity

Каноническая PVE identity Proximo:

```text
user:       proximo@pve
token:      proximo@pve!ai-control
privsep:    true
pool:       managed
```

Token secret:

- создаётся локально на PVE;
- показывается Proxmox только при создании;
- напрямую передаётся в `301` через QEMU Guest Agent;
- хранится внутри `301` в `/etc/ai-control/secrets/proximo-pve-token`;
- не записывается в Git;
- не хранится отдельным plaintext-файлом на PVE;
- при утрате требует явной ротации.

При privilege separation эффективные права являются пересечением прав пользователя и token, поэтому необходимые ACL выдаются обоим.

## Tool surface

По умолчанию используется:

```text
PROXIMO_TOOLSETS=pve.guests
```

Цель — не публиковать агенту без необходимости MCP domains для host network, SDN, access/IAM и других административных плоскостей.

Tool filtering — дополнительная защита. Hard boundary задаётся Proxmox ACL/token.

## Разрешённая зона

AI control должен уметь:

- видеть PVE/node/guest state в пределах необходимых audit-прав;
- использовать template `9000` как источник Full Clone;
- создавать новые гости сразу в `managed` resource pool;
- изменять CPU/RAM/disk/network/Cloud-Init управляемых гостей;
- start/stop/reboot управляемых гостей;
- делать snapshots/backups;
- выполнять guest-level diagnostics, которые разрешены выданными правами.

Template `9000` остаётся защищённым и не должен быть доступен на изменение/удаление.

`301-ai-control` не входит в обычную self-managed write-зону: control plane не должен случайно удалить или остановить самого себя. Для `320` действует тот же принцип до его вывода из эксплуатации.

## Запрещённая зона

Обычный AI control workflow не получает прав на:

```text
PVE physical NIC / vmbr* / host routes
storage definitions
users / groups / realms
roles / ACL / API tokens
Datacenter/host firewall
SDN
PVE repositories
certificates / ACME
reboot/shutdown PVE host
shell/exec на PVE host
```

Наличие соответствующих tools в Proximo не означает, что token должен уметь их выполнить.

Полная проектная матрица прав остаётся в [`002-proxmox-permissions.md`](./002-proxmox-permissions.md).

## Проверка границы

После установки/обновления Proximo обязательно выполняется:

```bash
proximo doctor
```

Нужно убедиться, что ожидаемые guest capabilities находятся в `can`, а host/IAM опасные действия остаются в `cannot`.

Для `301` результат сохраняется в `/opt/ai-control/state/proximo-doctor.json`.

Live-test целевой цепочки:

```text
Hermes + Proximo
→ Full Clone 9000 в managed pool
→ protection=0
→ Cloud-Init ops + ai_control_ed25519.pub
→ first start
→ QEMU Agent
→ SSH ops из 301
```

## Разделение функций

```text
Proximo
→ уровень виртуализации и guest-level Proxmox operations

Ansible на 311-dev-services
→ повторяемая конфигурация гостевых ОС и приложений

прямой SSH из ai-control
→ bootstrap, диагностика, аварийные и разовые действия
```

Proximo не заменяет Ansible как штатный повторяемый deploy-механизм внутри ОС.

## Главный принцип

> Proximo (`proximo-proxmox`) — единственный канонический Proxmox MCP для `320-ai-control` и `301-ai-control`; реальная граница его власти задаётся privilege-separated Proxmox token и ACL, а не возможностями самого MCP.
