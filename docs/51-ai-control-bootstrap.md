# AI Control bootstrap

## Статус

Архитектурные требования bootstrap `301-ai-control` сохраняются. Реализация scripts должна соответствовать текущей root-only management model.

Предыдущие public scripts из `zsergeyru/proxmox-bootstrap/archive/2026-09-14/` являются только историей и не используются как рабочие entrypoints.

## Архитектурная основа

Новая реализация должна соответствовать:

- [`20-pve-initialization.md`](20-pve-initialization.md) — zero-day PVE и host-side deploy;
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — файловая модель PVE bootstrap/deployer;
- [`23-security.md`](23-security.md) — management SSH identities и credential lifecycle;
- [`25-pve-access-control.md`](25-pve-access-control.md) — PVE identities, роли и `managed`;
- [`30-guest-manifest.md`](30-guest-manifest.md) — guest contract;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — initial management access, bootstrap и Ansible;
- [`50-ai-control.md`](50-ai-control.md) — архитектура `301-ai-control`;
- [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md) — contract template `9000`.

## Целевая последовательность bootstrap самого 301

`301` не должен быть необходим для собственного создания:

```text
чистый PVE
→ Public Bootstrap + PVE Configuration
→ private zsergeyru/proxmox доступен на PVE
→ current template 9000
→ host-side deploy 301-ai-control
→ root SSH по pve_guest_ed25519
→ подготовка common platform
→ создание AI guest-management SSH identity
→ установка выбранного AI agent
→ Git/SSH/MCP health checks
→ ввод 301 как control plane
```

Версия template здесь намеренно не фиксируется. Текущий baseline определяется только [`../templates/debian13/README.md`](../templates/debian13/README.md) и [`../templates/debian13/build-policy.md`](../templates/debian13/build-policy.md).

После ввода 301 обычный AI lifecycle:

```text
301 AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone обычного guest сразу в managed
→ configure/start/verify
```

`deploy-guest` остаётся human/direct-host инструментом и bootstrap-механизмом специальных случаев, но не обязательным посредником AI.

## Разделение этапов внутри 301

```text
создание VM 301
→ Proxmox / generic host-side guest deploy

подготовка общей AI platform
→ Docker/runtime, Git/OpenSSH, Python/tooling, Proximo, identities

установка AI agent
→ Hermes или другой agent-specific runtime/configuration
```

Common platform:

```text
/opt/ai-control/
├── agents/
├── mcp/
│   └── proximo/
├── ssh/
├── repos/
└── state/
```

## Proximo

Proximo остаётся общим MCP для Proxmox guest-level operations.

PVE credential `ai-agent@pve!infra` хранится и доставляется по принятой security policy. Hard boundary Proxmox задаётся PVE ACL.

После ввода 301 Proximo является штатным путём AI для create/clone/configure/start/stop/snapshot/backup/delete обычных разрешённых VM/LXC в `managed`.

Pool `managed` не является механизмом SSH-key distribution.

## Management SSH

Единый management user Debian VM/LXC:

```text
root
```

Root password locked; password authentication выключена.

Host-side initial identity:

```text
/etc/proxmox-deployer/ssh/pve_guest_ed25519
→ private key на PVE
→ root SSH для первоначального host-side deploy
```

AI Control после подготовки common platform создаёт отдельную identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Правила:

```text
private key
→ остаётся внутри AI control plane
→ не копируется на PVE
→ не хранится в Git

public key
→ может устанавливаться в root authorized_keys нужных VM/LXC
→ может передаваться новым VM через Cloud-Init
→ может передаваться новым LXC через ssh-public-keys
```

AI infrastructure key и GitHub Deploy Key — разные credentials.

Полный credential contract определён в [`23-security.md`](23-security.md) и не дублируется здесь.

## Независимость SSH от `managed`

```text
managed
→ PVE ACL / lifecycle права AI

AI public key в guest
→ direct root SSH AI
```

Перемещение guest в/из `managed` не должно автоматически редактировать `/root/.ssh/authorized_keys`.

Если direct AI SSH конкретному объекту больше не нужен, удаляется AI public key. Это не влияет на host-side PVE guest key и не меняет membership pool.

## Public-key registration

Public key не является секретом. Common-platform implementation должна уметь показать/экспортировать `ai_control_ed25519.pub` для регистрации в host-side deploy workflow.

Private key при этом не покидает 301.

Точный transport public key может быть реализован отдельной простой процедурой вместе с `deploy-guest`; отсутствие AI public key не должно блокировать первоначальный host-side deploy самого 301 или другого guest по `pve_guest_ed25519`.

Для гостя, создаваемого самим AI, `301` уже знает собственный public key и может передать его через create/Cloud-Init options.

## AI agent

Agent-specific software размещается:

```text
/opt/ai-control/agents/<agent>/
```

Hermes и будущие агенты используют common Proximo и common AI SSH identity; каждый installer не должен генерировать отдельную инфраструктурную keypair.

## Repeatable guest configuration

После появления `311-dev-services` повторяемая конфигурация выполняется через отдельный Ansible SSH credential:

```text
AI agent / пользователь
→ Git
→ Ansible на 311
→ SSH root
→ guest
```

Это отдельный слой от Proximo и не заменяет прямой AI SSH. AI может выполнять произвольные one-off/diagnostic действия по своему `ai_control_ed25519`, даже если соответствующего Ansible playbook ещё нет.

## Требования к новым scripts

Новая реализация должна быть:

- безопасной при повторном запуске;
- без secrets в Git/logs;
- с preflight и явными ошибками;
- без silent overwrite существующих critical VM/credentials;
- с health verification;
- согласованной с `root:22` guest contract;
- без зависимости initial SSH от optional `base` capability;
- без автоматической синхронизации SSH keys с pool `managed`.

## Live-test перед production

До вывода `320-ai-control` нужно проверить:

1. `301` создаётся host-side из текущего template `9000`;
2. QGA/Cloud-Init работают;
3. host-side root SSH по `pve_guest_ed25519` работает;
4. common platform создаёт отдельный `ai_control_ed25519`;
5. GitHub identity остаётся отдельной;
6. Proximo авторизуется как `ai-agent@pve!infra`;
7. AI создаёт/clone test guest сразу в `managed`;
8. AI public key попадает в root authorized_keys test guest;
9. AI входит в test guest как root своим ключом;
10. удаление AI public key отзывает AI SSH, не ломая deployer access;
11. перенос test guest в/из `managed` сам по себе не меняет SSH keyset;
12. Ansible на `311` работает отдельной identity;
13. `301` не может менять собственную VM через обычную PVE write-zone;
14. backup/recovery/credential handling соответствует policy.

До успешной проверки `320-ai-control` не удаляется.
