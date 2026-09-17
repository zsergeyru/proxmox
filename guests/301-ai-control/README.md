# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; `320-ai-control` сохраняется до завершения приёмочной проверки.

Основная архитектурная спецификация находится в [`../../docs/50-ai-control.md`](../../docs/50-ai-control.md), порядок ввода в работу — в [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md), а жизненный цикл административных SSH-ключей — в [`../../docs/28-management-ssh-keys.md`](../../docs/28-management-ssh-keys.md). Этот README фиксирует только особенности конкретной VM `301`.

## Архитектура каталогов

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── repos/
│   └── proxmox/
└── state/

/etc/proxmox-guest/
├── ssh/
│   ├── management_ed25519
│   └── management_ed25519.pub
├── public-keys/
│   └── management-authorized-keys
└── credentials/
    └── github-proxmox-read
```

Ключевое правило:

> Конкретный AI-агент устанавливается в `/opt/ai-control/agents/<agent>/`. Proximo, административная management SSH-пара гостя, локальная копия общего каталога открытых ключей и рабочая копия Git являются общими компонентами `301`, а не собственностью Hermes.

## Целевая последовательность

Старые архивные скрипты не считаются действующей реализацией.

```text
1. создание VM 301 со стороны PVE
   deploy-guest → Full Clone текущего шаблона 9000 → SSH root ключом deployer

2. если management_key=true
   внутри 301 создаётся стандартная management SSH-пара
   → private остаётся только в 301
   → deployer забирает только .pub
   → регистрирует его в каноническом public-key registry на PVE
   → sync-management-keys разносит обновлённый каталог

3. общая AI-платформа
   Docker + общие инструменты + Proximo + Project Git READ

4. конкретный агент
   Hermes или другой агент → интерфейс, интеграции и конфигурация
```

## Целевой manifest

После реализации соответствующих schema/resolver целевой `guest.yaml` 301 должен явно запрашивать:

```yaml
management_key: true

access:
  project_repo_read: true
```

`management_key` означает только создание собственной административной SSH-пары внутри 301 и регистрацию её открытой части у deployer. Поле не содержит имя ключа, путь или роль `ai-control`.

`access.project_repo_read` означает только выдачу фиксированного общего read-only Git credential для `zsergeyru/proxmox`.

Оба интерфейса на момент фиксации этой документации ещё не реализованы в действующей schema v6, поэтому в рабочий `guest.yaml` они добавляются только вместе с реализацией schemas/resolver/validator.

## Management SSH

Собственная пара 301 находится в стандартном месте гостя:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Использование:

```text
private key
→ остаётся только внутри 301
→ используется AI Control для SSH root@guest

public key
→ deployer регистрирует на PVE
→ sync-management-keys распространяет по управляемым Debian-гостям
```

301 не пишет в каталог PVE и не получает SSH-доступ к PVE ради регистрации ключа.

Локальная копия общего публичного каталога:

```text
/etc/proxmox-guest/public-keys/
└── management-authorized-keys
```

обновляется со стороны PVE командой `sync-management-keys`.

## Создание гостевой системы самим AI

Для новой обычной Debian VM/LXC:

```text
AI-агент
→ читает /etc/proxmox-guest/public-keys/management-authorized-keys
→ Proximo
→ создаёт или клонирует объект сразу в managed
→ передаёт ВЕСЬ актуальный набор public keys новой машине
→ VM: Cloud-Init sshkeys
→ LXC: ssh-public-keys
→ запускает и проверяет
```

Так новая машина сразу доступна существующим административным контурам, включая deployer и Ansible, без прямого обмена ключами между 301 и 311 и без доступа AI к файловой системе PVE.

Если новой машине нужна собственная management identity, это отдельное требование её `guest.yaml` через `management_key: true`; штатную регистрацию такой пары выполняет deploy-контур.

## Project Git READ

Для чтения закрытого проектного репозитория 301 использует общий GitHub Deploy Key только для чтения, а не management SSH-пару.

Локальная копия:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
```

Рабочая копия проекта:

```text
/opt/ai-control/repos/proxmox
```

Git READ credential не добавляется в `authorized_keys`. Если AI когда-либо потребуется `git push`, это отдельный write-контур и отдельный credential.

## Proximo и граница PVE

Основной MCP — Proximo (`proximo-proxmox`):

```text
/opt/ai-control/mcp/proximo/
```

AI работает через:

```text
ai-agent@pve!infra
→ /pool/managed
```

301 штатно не получает SSH/root на PVE, не изменяет каталог `/etc/proxmox-deployer/` и не управляет PVE-пользователями, ACL, сетью хоста, storage definitions, сертификатами или репозиториями PVE.

## Взаимодействие с 311

301 и 311 не обмениваются административными ключами напрямую.

```text
301 создаёт свою management пару
→ deployer регистрирует 301.pub

311 создаёт свою management пару
→ deployer регистрирует 311.pub

sync-management-keys
→ распространяет единый public-key registry по обоим гостям и остальным управляемым Debian VM/LXC
```

Ansible остаётся в `311-dev-services`. Прямой SSH из AI Control допустим для диагностики и разовых действий; повторяемая конфигурация ОС и приложений выполняется Ansible.

## Готовность

Полный перечень приёмочных проверок находится в [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md). До замены `320` должны быть подтверждены как минимум Proximo, ACL `managed`, management SSH, локальная копия public-key registry, Project Git READ и резервное копирование.