# ADR 003 — Proxmox MCP для AI control

## Статус

**Обновлено.**

- `320-ai-control`: фактически установленный `gordcurrie/proxmox-mcp` остаётся legacy/observed state и не требует немедленной замены только ради унификации.
- `301-ai-control`: целевой канонический MCP — **Proximo** (`proximo-proxmox`).

То есть старый выбор не переносится автоматически в новую control-plane VM.

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

## Решение для 301

Для `301-ai-control` используется:

```text
Proximo / proximo-proxmox
```

Установка выполняется zero-day bootstrap-скриптом из публичного `zsergeyru/proxmox-bootstrap`.

Proximo запускается локально как stdio MCP-процесс Hermes. Отдельный HTTP MCP daemon для одного локального Hermes не нужен:

```text
Hermes
  │ stdio MCP
  ▼
Proximo
  │ HTTPS Proxmox API
  ▼
PVE
```

Если позже появится реальная необходимость в общем сетевом MCP для нескольких клиентов, это оформляется отдельно; Proximo поддерживает отдельные remote faces, но zero-day архитектура ими не усложняется.

## Management identity

Bootstrap создаёт отдельную identity:

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
- хранится внутри `301` в файле режима `0600`;
- не записывается в Git;
- не должен оставаться отдельным plaintext-файлом на PVE.

При privilege separation эффективные права являются пересечением прав пользователя и token, поэтому нужные ACL выдаются обоим.

## Tool surface

В Hermes по умолчанию используется:

```text
PROXIMO_TOOLSETS=pve.guests
```

Цель — не публиковать агенту без необходимости MCP domains для host network, SDN, access/IAM и других административных плоскостей.

Tool filtering — только дополнительная защита. Hard boundary задаётся Proxmox ACL/token.

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

Сам `301-ai-control` не обязан входить в обычную self-managed write-зону: control plane не должен случайно удалить/остановить самого себя.

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

Если Proximo технически содержит tools для этих областей, это не означает, что token должен уметь их выполнить.

Полная проектная матрица прав остаётся в [`002-proxmox-permissions.md`](./002-proxmox-permissions.md).

## Проверка границы

После bootstrap обязательно выполняется:

```bash
proximo doctor
```

Результат сохраняется внутри `301` для диагностики. До перевода `301` в основной control plane нужно убедиться, что ожидаемые guest capabilities находятся в `can`, а host/IAM опасные действия остаются в `cannot`.

Затем проводится live-test:

```text
Hermes + Proximo
→ Full Clone 9000 в managed pool
→ protection=0
→ Cloud-Init ops + ai_control_ed25519.pub
→ first start
→ QEMU Agent
→ SSH ops из 301
```

Без этой проверки zero-day bootstrap считается подготовленным, но не полностью live-validated.

## Почему не переносим gordcurrie/proxmox-mcp с 320

На `320` он уже установлен и может продолжать работать до миграции. Но целевой `301` строится заново и не обязан наследовать исторически выбранный transport.

Proximo выбран для новой схемы потому, что сочетает:

- least-privilege модель поверх Proxmox ACL;
- встроенный `doctor` для проверки фактической границы token;
- PLAN/PROVE/UNDO подход для mutations;
- provisioning/config/cloud-init surface;
- управляемое сокращение tool surface через `PROXIMO_TOOLSETS`;
- локальный stdio transport, достаточный для одного Hermes в `301`.

## Разделение функций

```text
Proximo
→ уровень виртуализации и guest-level Proxmox operations

Ansible на 311-dev-services
→ повторяемая конфигурация гостевых ОС и приложений

прямой SSH из 301
→ bootstrap, диагностика, аварийные и разовые действия
```

Даже если MCP умеет выполнять дополнительные операции, он не заменяет Ansible как штатный повторяемый deploy-механизм внутри ОС.

## Главный принцип

> Proximo — канонический Proxmox MCP для `301-ai-control`; реальная граница его власти задаётся отдельным privilege-separated Proxmox token и ACL, а не возможностями самого MCP.
