# AI Control bootstrap

## Статус

Архитектурные требования bootstrap `301-ai-control` сохраняются. **С нуля переписывается только реализация scripts.**

Предыдущие public scripts:

```text
create-ai-control-vm.sh
prepare-ai-control.sh
install-ai-agent.sh
bootstrap-ai-control.sh
```

перенесены в:

```text
zsergeyru/proxmox-bootstrap/archive/2026-09-14/
```

Они больше не являются рабочими entrypoints и не должны использоваться для нового развёртывания.

## Архитектурная основа

Новая реализация должна соответствовать уже принятым документам:

- [`20-pve-initialization.md`](20-pve-initialization.md) — zero-day PVE и host-side deploy;
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) — файловая модель PVE bootstrap/deployer;
- [`25-pve-access-control.md`](25-pve-access-control.md) — канонические PVE identities, роли и граница `managed`;
- [`33-guest-bootstrap-and-provisioning.md`](33-guest-bootstrap-and-provisioning.md) — guest bootstrap и передача управления Ansible;
- [`50-ai-control.md`](50-ai-control.md) — архитектура `301-ai-control`;
- ADR `311-dev-services` — Ansible/Semaphore и repeatable deploy;
- ADR `320-ai-control` — management/security model, пока не заменён отдельным новым ADR.

`25-pve-access-control.md` является источником истины по тому, кто и какими PVE credentials создаёт и управляет обычными VM/LXC.

## Целевая последовательность bootstrap самого 301

`301` является специальным control-plane объектом и не должен быть необходим для собственного создания.

Поэтому его первоначальное развёртывание остаётся host-side:

```text
чистый PVE
→ zero-day PVE bootstrap Stage 0 + Stage 1
→ private zsergeyru/proxmox доступен на PVE
→ template 9000
→ host-side deploy 301-ai-control по guest.yaml
→ guest bootstrap/common platform внутри 301
→ установка выбранного AI agent
→ Git/SSH/MCP health checks
→ ввод 301 как control plane
```

Здесь использование host-side deploy не означает, что после запуска 301 все новые гости обязаны создаваться через `deploy-guest`.

После ввода control plane обычный AI lifecycle выглядит иначе:

```text
301 AI agent
→ Proximo
→ ai-agent@pve!infra
→ create/clone обычного guest сразу в managed
→ configure/start/verify
```

То есть `deploy-guest` остаётся человеческим/direct-host инструментом и bootstrap-механизмом для специальных случаев вроде первоначального создания самого `301`, а обычные разрешённые managed guests AI может создавать и обслуживать напрямую через Proximo согласно `25-pve-access-control.md`.

## Разделение этапов внутри 301

Сохраняется архитектурное разделение:

```text
создание VM 301
→ уровень Proxmox / generic host-side guest deploy

подготовка общей AI platform
→ Docker/runtime, Git/OpenSSH, Python/tooling, общий Proximo, identities

установка AI agent
→ Hermes или другой agent-specific runtime/configuration
```

Это логические уровни. Новая реализация не обязана повторять старые имена или количество shell-файлов.

## Common platform

Общие capabilities не принадлежат конкретному AI agent:

```text
/opt/ai-control/
├── agents/
├── mcp/
│   └── proximo/
├── ssh/
├── repos/
└── state/
```

Docker/runtime, Proximo, Git checkout и SSH identities не должны дублироваться в каждом `agents/<name>/`.

## Proximo

Proximo остаётся общим MCP для Proxmox guest-level operations.

PVE credential создаётся и хранится по принятой PVE bootstrap/security policy. Runtime copy передаётся в `301` безопасным способом новой реализацией.

Hard boundary задаётся PVE ACL. `301` не получает host-level administration и не входит в собственную обычную self-managed write-zone.

После ввода 301 Proximo является штатным путём AI для create/clone/configure/start/stop/snapshot/backup/delete обычных разрешённых VM/LXC в `managed`.

Новый обычный guest должен создаваться сразу в `managed`. AI не должен получать возможность произвольно принимать существующие protected/self-managed VM вне этого pool.

## SSH identities

Сохраняются разные задачи и разные credentials:

```text
infrastructure SSH identity
→ доступ к managed Debian guests как ops

GitHub Deploy Key
→ доступ к private zsergeyru/proxmox
```

Private keys не хранятся в Git. Эти credentials не используются взаимозаменяемо. Способ генерации, доставки и проверки реализуется новыми scripts.

## AI agent

Agent-specific software размещается отдельно:

```text
/opt/ai-control/agents/<agent>/
```

Архитектура допускает Hermes и другие агенты без перестройки общей platform части.

Конкретная команда installer, версия и способ установки могут меняться при новой реализации; это implementation detail, если не меняет архитектурные границы.

## Repeatable guest configuration

После появления `311-dev-services` повторяемая конфигурация гостевых ОС выполняется через Ansible по SSH.

```text
AI agent / пользователь
→ Git
→ Ansible на 311
→ SSH
→ guest
```

Это отдельный слой от PVE lifecycle. AI может создать обычный guest через Proximo, после чего штатный guest bootstrap/provisioning доводит ОС и приложения до Git desired state.

Прямой SSH из `301` сохраняется для bootstrap, диагностики, разовых и аварийных действий.

## Требования к новым scripts

Новая реализация должна быть:

- безопасной при повторном запуске;
- понятной по зонам ответственности;
- без secrets в Git/logs;
- с preflight и явными ошибками;
- без молчаливого overwrite существующих критичных VM/credentials;
- с health verification после каждого существенного этапа;
- с возможностью отдельно диагностировать host deploy, platform preparation и agent install;
- согласованной с `guest.yaml`, PVE ACL и filesystem policy.

Bootstrap самого `301` не должен требовать работающего AI control plane. После ввода 301 обычное создание managed guests, наоборот, не должно искусственно маршрутизироваться через host-side `deploy-guest`.

## Что не переносим автоматически из старого кода

Архивный код можно использовать только как справочный материал. Не обязаны сохраняться:

- старые shell-функции и структура файлов;
- старые installer URL;
- старые version pins;
- конкретные временные workaround;
- порядок команд, выбранный исключительно из-за ограничений старого script.

Если новое implementation-решение не меняет архитектуру, его можно упростить.

## Live-test перед production

До вывода `320-ai-control` нужно проверить полный новый путь на реальном PVE:

1. `301` создаётся host-side из принятого PVE deploy flow без зависимости от собственного control plane;
2. QGA/Cloud-Init/SSH работают;
3. common platform устанавливается повторяемо;
4. Proximo авторизуется как `ai-agent@pve!infra` и видит только разрешённую PVE surface;
5. AI через Proximo создаёт/clone тестовый обычный guest сразу в `managed`;
6. infrastructure SSH key работает на test managed guest;
7. private repo доступен через отдельную Git identity;
8. AI agent запускается и использует common capabilities;
9. Ansible на `311` может применять desired state к test guest;
10. `301` не может изменять собственную VM через обычную write-zone;
11. protected/self-managed объекты вне `managed` не становятся автоматически доступны AI write-access;
12. backup/recovery/credential handling соответствует принятой policy.

До успешной проверки `320-ai-control` не удаляется.
