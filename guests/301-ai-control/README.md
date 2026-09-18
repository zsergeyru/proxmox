# 301 — ai-control

**Тип:** VM  
**Статус:** целевой управляющий AI-узел; `320-ai-control` сохраняется до завершения приёмочной проверки.

Основная архитектурная спецификация находится в [`../../docs/50-ai-control.md`](../../docs/50-ai-control.md), порядок ввода в работу — в [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md), жизненный цикл administrative SSH keys — в [`../../docs/28-management-ssh-keys.md`](../../docs/28-management-ssh-keys.md), текущая степень реализации — в [`../../docs/29-implementation-status.md`](../../docs/29-implementation-status.md). Этот README фиксирует только особенности VM `301`.

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
│   ├── management_ed25519.pub
│   └── github-proxmox-known_hosts
├── public-keys/
│   └── management-authorized-keys
└── credentials/
    └── github-proxmox-read
```

Ключевое правило:

> Конкретный AI-агент устанавливается в `/opt/ai-control/agents/<agent>/`. Proximo, management SSH identity гостя, локальный public-key catalog и рабочая копия проекта являются общими компонентами `301`, а не собственностью Hermes.

## Последовательность ввода

```text
1. deploy-guest → Full Clone template 9000
   → management-authorized-keys
   → PVE tag management-ssh
   → verified SSH root deployer

2. management: ssh_identity
   → создать/проверить стандартную pair внутри 301
   → private оставить только в 301
   → зарегистрировать 301.pub на PVE
   → sync-management-keys

3. management: project_repo_read
   → обеспечить фиксированный Project Git READ desired state

4. общая AI-платформа
   → Docker + общие инструменты + Proximo + рабочая копия проекта

5. конкретный агент
   → Hermes или другой agent
```

## Действующий desired state

Schema v9 использует список management-возможностей, и рабочий `guest.yaml` 301 явно содержит:

```yaml
management:
  - ssh_identity
  - project_repo_read
```

`ssh_identity` означает только собственную исходящую administrative SSH identity 301; элемент не содержит key path/name или роль `ai-control`.

`project_repo_read` означает только фиксированный read-only доступ к `zsergeyru/proxmox`.

Наличие этих полей в schema/manifest не означает, что соответствующие runtime handlers уже написаны; это отдельно показывает [`../../docs/29-implementation-status.md`](../../docs/29-implementation-status.md).

## Management SSH

301 участвует в management SSH-контуре и должен иметь PVE tag:

```text
management-ssh
```

Собственная pair:

```text
/etc/proxmox-guest/ssh/management_ed25519
/etc/proxmox-guest/ssh/management_ed25519.pub
```

Private key остаётся только внутри 301 и используется AI Control для SSH `root@guest`.

Public key deployer регистрирует как:

```text
/var/lib/proxmox-deployer/public-keys/301.pub
```

После этого `sync-management-keys` распространяет обновлённый public-key set по участникам `management-ssh`.

301 не пишет в PVE filesystem и не получает SSH-доступ к PVE ради регистрации ключа.

Локальная копия public catalog:

```text
/etc/proxmox-guest/public-keys/
└── management-authorized-keys
```

## Создание гостевой системы самим AI

Для новой Debian VM/LXC management-контура:

```text
AI agent
→ читает локальный management-authorized-keys
→ Proximo / PVE API
→ создаёт или клонирует object в managed
→ передаёт весь актуальный public-key set
→ VM: Cloud-Init sshkeys
→ LXC: ssh-public-keys
→ ставит PVE tag management-ssh
→ запускает и проверяет
```

Так новая машина сразу доступна существующим management-контуром без прямого обмена private keys между 301 и 311 и без доступа AI к PVE filesystem.

Если новой машине нужна собственная identity, это задаётся её manifest:

```yaml
management:
  - ssh_identity
```

Регистрацию такой pair выполняет PVE-side deploy-контур.

## Project Git READ

301 использует общий read-only GitHub Deploy Key, а не management SSH pair.

Manifest:

```yaml
management:
  - project_repo_read
```

Управляемые guest-local artifacts:

```text
/etc/proxmox-guest/credentials/github-proxmox-read
/etc/proxmox-guest/ssh/github-proxmox-known_hosts
/etc/ssh/ssh_config.d/90-proxmox-project-repo-read.conf
```

Рабочая копия проекта:

```text
/opt/ai-control/repos/proxmox
```

Git READ credential не добавляется в `authorized_keys`. Git write для AI, если понадобится, является отдельным контуром.

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

301 штатно не получает SSH/root на PVE, не меняет `/etc/proxmox-deployer/`, PVE users/ACL, host network, storage definitions, certificates или PVE repositories.

## Взаимодействие с 311

301 и 311 не обмениваются management keys напрямую:

```text
301 pair → register 301.pub
311 pair → register 311.pub
          ↓
sync-management-keys
          ↓
единый public-key set на management-ssh guests
```

Ansible остаётся в `311-dev-services`. Прямой SSH из AI Control допустим для диагностики и разовых действий; повторяемая конфигурация выполняется Ansible.

## Готовность

Полный перечень приёмочных проверок находится в [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md). До замены `320` должны быть подтверждены как минимум Proximo, ACL `managed`, `management-ssh`, management identity, локальный public-key catalog, Project Git READ и backup.