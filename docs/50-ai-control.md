# AI Control — архитектурные принципы

## Статус

Архитектура `301-ai-control` остаётся принятой. Исполняемая реализация bootstrap/install/deploy scripts переписывается под текущий guest contract.

Каноническая модель PVE access/deploy задаётся [`25-pve-access-control.md`](25-pve-access-control.md), management SSH — [`../templates/debian13/users-and-keys.md`](../templates/debian13/users-and-keys.md).

## Разделение ответственности

```text
Proximo MCP + ai-agent@pve!infra
→ PVE lifecycle обычных VM/LXC в зоне managed
→ create/clone/configure/start/stop/snapshot/backup/delete и diagnostics

host-side deploy-guest + deployer@pve!host-deploy
→ человек / локальная host-side automation
→ deterministic PLAN/APPLY из guest manifests

Ansible на 311-dev-services
→ повторяемая конфигурация ОС и приложений внутри гостей через SSH

прямой SSH из 301
→ произвольная диагностика, bootstrap, one-off и аварийные действия внутри guest OS
```

`deploy-guest` не является обязательным security gateway для AI agent. AI и человек используют разные PVE identities.

Ansible/Semaphore не размещаются в `301`; они относятся к `311-dev-services`.

## 301-ai-control

`301-ai-control` — целевая VM центрального AI-контура. `320-ai-control` остаётся bootstrap/legacy control node до успешного ввода `301` и отдельного решения о выводе `320`.

```text
/opt/ai-control/
├── agents/
│   ├── hermes/
│   └── <future-agent>/
├── mcp/
│   └── proximo/
├── ssh/
├── repos/
│   └── proxmox/
└── state/
```

Docker/runtime, Proximo, SSH identities и Git checkout являются common platform и не дублируются внутри каждого агента.

## Proximo и `managed`

Канонический Proxmox MCP — Proximo (`proximo-proxmox`).

Hard boundary PVE задаётся identity/token/ACL. AI Control не получает штатных прав на host network, IAM/ACL, SDN infrastructure, storage definitions, certificates/repositories или reboot/shutdown самого PVE.

Основная PVE write-zone `ai-agent@pve!infra`:

```text
managed
```

Нормальный flow:

```text
AI agent
→ Git desired state
→ Proximo
→ create/clone сразу с pool=managed
→ CPU/RAM/disk/network и guest-level options
→ start/verify
→ дальнейшее PVE guest-level управление
```

AI может самостоятельно создавать, клонировать, конфигурировать и удалять обычные разрешённые VM/LXC через Proximo.

Template `9000` остаётся отдельным clone source и не входит в обычную write-zone AI.

## Важная граница: PVE access != SSH access

Pool `managed` определяет **права AI на объект Proxmox**, но не является списком SSH authorized keys внутри Linux.

```text
PVE:
guest в managed
→ ai-agent@pve!infra получает предусмотренные guest-level PVE permissions

guest вне managed
→ эти PVE permissions не распространяются
```

Отдельно:

```text
Guest OS:
есть ai_control_ed25519.pub в /root/.ssh/authorized_keys
→ AI может SSH root внутрь ОС

ключ удалён
→ direct AI SSH запрещён
```

Перемещение VM/LXC в `managed` или из него само по себе **не должно изменять `authorized_keys`**. Это предотвращает необходимость синхронизировать две независимые системы доступа.

Поэтому guest вне `managed` может сознательно оставаться доступным AI по SSH для диагностики/конфигурации ОС, при этом AI не получает права менять его PVE hardware/lifecycle через Proximo.

## SSH

Единый management user управляемой Debian-инфраструктуры:

```text
root
```

Root password locked. SSH password authentication disabled. Root login разрешён только по public key.

AI Control использует отдельную infrastructure identity:

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся в AI control plane. Public key устанавливается тем Linux guests, которым разрешён direct AI SSH.

Host-side deployer использует другой keypair `pve_guest_ed25519`; Ansible/311 — свою provisioning identity. Все они могут авторизоваться как `root`.

GitHub credential и guest-management SSH identity — разные credentials.

## Создание гостя host-side

```text
оператор
→ deploy-guest <VMID> [--apply]
→ deployer@pve!host-deploy
→ Full Clone from 9000 v6 либо создание LXC из Debian 13 family selector
→ установить host-side public key для root
→ при наличии зарегистрированного AI public key включить его в initial keyset, если direct AI SSH нужен
→ start
→ verify root SSH
→ optional bootstrap
```

Initial root SSH не зависит от `base` capability.

## Создание гостя AI

```text
AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone сразу в managed
→ передать AI public key через Cloud-Init/LXC create options
→ при доступном host-side public credential добавить также deployer public key
→ start
→ verify
```

AI не обязан вызывать host-side `deploy-guest`.

`301` — специальный случай: initial creation самого control plane выполняется host-side и не зависит от уже работающего 301.

## Repeatable deploy

После развёртывания `311-dev-services`:

```text
AI agent / пользователь
→ конфигурация в Git
→ Ansible на 311
→ SSH root по отдельному provisioning key
→ нужный guest
→ health/status/log verification
```

Это не ограничивает AI только Ansible playbooks: прямой SSH из `301` по `ai_control_ed25519` сохраняется для произвольных действий внутри ОС.

## Git

Приватный `zsergeyru/proxmox` остаётся source of truth.

GitHub access AI Control выполняется отдельной Deploy Key identity. Public/private части Git credential не используются как guest SSH credential.

## Что переписывается

Старые public scripts из archive не являются действующими entrypoints. Новая реализация должна соответствовать:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`25-pve-access-control.md`](25-pve-access-control.md);
- [`30-guest-manifest.md`](30-guest-manifest.md);
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md);
- [`51-ai-control-bootstrap.md`](51-ai-control-bootstrap.md);
- [`../templates/debian13/users-and-keys.md`](../templates/debian13/users-and-keys.md).

## Security boundary

1. PVE privilege-separated token + ACL определяют PVE operations AI.
2. `ai-agent@pve!infra` штатно управляет обычными гостями в `managed` через Proximo.
3. Direct guest SSH является отдельным credential boundary и определяется наличием AI public key.
4. AI SSH key не привязан автоматически к pool membership.
5. AI может create/clone/delete обычные managed guests напрямую.
6. MCP surface ограничивается необходимыми guest-level operations.
7. Private SSH keys и provider credentials не хранятся в Git.
8. Root password authentication отключена.
9. Snapshots/backups используются перед рискованными изменениями.
10. `301`, production HA, bootstrap `320` и template `9000` не входят автоматически в PVE write-zone `managed`.

## Готовность к замене 320

До вывода `320-ai-control` проверить:

1. host-side создание самого `301` без зависимости от работающего 301;
2. root key-only SSH;
3. отдельные Git и AI guest-management SSH identities;
4. Proximo и авторизацию `ai-agent@pve!infra`;
5. создание/clone тестового guest сразу в `managed`;
6. прямой root SSH AI в тестовый guest по `ai_control_ed25519`;
7. удаление AI public key действительно отзывает только AI SSH;
8. PVE membership `managed` не модифицирует SSH keys;
9. взаимодействие с Ansible на `311`;
10. backup/recovery и credential handling.
