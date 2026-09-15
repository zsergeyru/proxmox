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

> Конкретный AI-агент устанавливается в `/opt/ai-control/agents/<agent>/`. Docker, Proximo, SSH identities и Git checkout являются общими компонентами `301` и не принадлежат Hermes.

## Целевая последовательность

Старые archived scripts не считаются канонической реализацией. Новый flow должен логически разделять:

```text
1. host-side deploy VM 301
   PVE → Full Clone Template-Version 6 → root SSH по pve_guest_ed25519

2. common platform внутри 301
   Docker + common tooling + Proximo + SSH identities

3. agent-specific install
   Hermes или другой agent → UI/integration/config
```

Это позволяет заменить агента или установить несколько агентов без пересоздания общей platform части.

## Этап 1 — VM

Первоначальное создание `301` выполняется host-side, потому что control plane не должен зависеть от собственного существования.

Ожидается:

- Full Clone `9000 → 301` из Template-Version 6;
- CPU/RAM/disk/network/Cloud-Init;
- `ciuser=root`;
- host-side `pve_guest_ed25519.pub` в root authorized_keys;
- `protection=0`, `onboot=1` согласно manifest;
- first boot + QGA/cloud-init/root-SSH health.

PVE identity AI (`ai-agent@pve!infra`) создаётся Stage 1 по общей ACL policy и затем безопасно передаётся Proximo runtime внутри `301`.

## Этап 2 — common AI platform

Common platform должна подготовить независимо от выбранного агента:

- Docker Engine;
- Docker Buildx;
- Docker Compose plugin;
- Git/OpenSSH/curl/jq/OpenSSL;
- Python/venv/pip;
- Proximo в `/opt/ai-control/mcp/proximo`;
- AI guest-management identity;
- отдельную GitHub identity.

SSH identities:

```text
/opt/ai-control/ssh/ai_control_ed25519(.pub)
/opt/ai-control/ssh/github_proxmox_repo_ed25519(.pub)
```

После common-platform этапа `/opt/ai-control/agents/` может оставаться пустым.

## Этап 3 — конкретный агент

Agent-specific software размещается строго под:

```text
/opt/ai-control/agents/<agent>/
```

Agent installer использует уже подготовленные common Proximo и SSH identities, а не генерирует их заново.

## Proximo

Канонический Proxmox MCP — **Proximo** (`proximo-proxmox`):

```text
/opt/ai-control/mcp/proximo/
```

PVE hard boundary задаётся `ai-agent@pve!infra` и ACL. Основная write-zone — `managed`.

`301` не получает штатных прав на host network, IAM/ACL, SDN infrastructure, storage definitions, repositories/certificates или reboot/shutdown PVE и не входит в собственную обычную PVE write-zone.

## SSH identities

AI guest-management identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Использование:

```text
private key
→ остаётся только в 301

public key
→ устанавливается в /root/.ssh/authorized_keys тех Debian VM/LXC,
  где разрешён direct AI SSH

AI agent
→ ssh root@guest
```

Generic user `ops` не используется.

AI public key не привязан автоматически к `managed`: pool регулирует PVE operations, а наличие key регулирует direct guest SSH. Если AI SSH к конкретной машине надо запретить, удаляется только AI public key.

GitHub Deploy Key:

```text
/opt/ai-control/ssh/github_proxmox_repo_ed25519
/opt/ai-control/ssh/github_proxmox_repo_ed25519.pub
```

используется только для private Git и не подменяет guest-management identity.

## Создание гостя самим AI

Для обычного нового guest:

```text
AI agent
→ Proximo
→ create/clone сразу в managed
→ передать ai_control_ed25519.pub в initial SSH keyset
→ для VM: Cloud-Init root sshkeys
→ для LXC: ssh-public-keys для root
→ start
→ verify root SSH
```

Если AI control располагает зарегистрированным public half host-side key, в initial keyset также добавляется `pve_guest_ed25519.pub`. Private host key в 301 не копируется.

## Git

GitHub identity создаётся отдельно от infrastructure SSH identity. После регистрации Deploy Key private repo клонируется в:

```text
/opt/ai-control/repos/proxmox
```

Private keys не хранятся в Git.

## Механизмы управления

```text
Proximo MCP        → PVE lifecycle/config/snapshots/backups в разрешённой зоне
AI direct SSH      → произвольные действия внутри guest OS
Ansible на 311     → repeatable provisioning внутри guest OS
```

Ansible/Semaphore не размещаются внутри `301`.

## Критерии готовности

До замены `320` проверить:

1. Full Clone `9000 → 301` из Template-Version 6;
2. host-side `root` SSH в 301 по `pve_guest_ed25519`;
3. common platform создаёт рабочие Docker/Compose/Proximo;
4. отдельные `ai_control_ed25519` и GitHub identity созданы;
5. agent installer не дублирует common identities;
6. Git Deploy Key зарегистрирован и private repo доступен;
7. Proximo работает с `ai-agent@pve!infra`;
8. Proximo создаёт test VM/LXC сразу в `managed`;
9. `ai_control_ed25519.pub` попадает в root authorized_keys test guest;
10. `301` входит в test guest по SSH как `root`;
11. удаление AI public key отзывает direct AI SSH без потери host-side доступа;
12. перенос guest в/из `managed` не редактирует SSH keys;
13. затем проверен `311-dev-services`/Ansible отдельным keypair.

Подробности: [`../../docs/51-ai-control-bootstrap.md`](../../docs/51-ai-control-bootstrap.md), [`../../docs/50-ai-control.md`](../../docs/50-ai-control.md), [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md).
