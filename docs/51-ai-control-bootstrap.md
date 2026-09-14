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
- [`50-ai-control.md`](50-ai-control.md) — архитектура `301-ai-control`;
- ADR `311-dev-services` — Ansible/Semaphore и repeatable deploy;
- ADR `320-ai-control` — management/security model, пока не заменён отдельным новым ADR.

То есть мы не выводим архитектуру заново из требований. Мы заново пишем код, который её реализует.

## Целевая последовательность

Host-side создание VM должно быть частью общей PVE deploy-модели, а не зависеть от работающего AI:

```text
чистый PVE
→ zero-day PVE bootstrap
→ private zsergeyru/proxmox доступен на PVE
→ template 9000
→ deploy 301-ai-control по guest.yaml
→ подготовка общей AI platform внутри 301
→ установка выбранного AI agent
→ Git/SSH/MCP health checks
→ ввод 301 как control plane
```

`301` не должен быть необходим для собственного создания.

## Разделение этапов внутри 301

Сохраняется архитектурное разделение:

```text
создание VM 301
→ уровень Proxmox / generic guest deploy

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

## SSH identities

Сохраняются две разные задачи:

```text
infrastructure SSH identity
→ доступ к managed Debian guests как ops

GitHub Deploy Key
→ доступ к private zsergeyru/proxmox
```

Private keys не хранятся в Git. Способ генерации, доставки и проверки реализуется новыми scripts.

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

1. `301` создаётся из принятого PVE deploy flow;
2. QGA/Cloud-Init/SSH работают;
3. common platform устанавливается повторяемо;
4. Proximo видит только разрешённую PVE surface;
5. infrastructure SSH key работает на test managed guest;
6. private repo доступен через отдельную Git identity;
7. AI agent запускается и использует common capabilities;
8. Ansible на `311` может применять desired state;
9. `301` не может изменять собственную VM через обычную write-zone;
10. backup/recovery/credential handling соответствует принятой policy.

До успешной проверки `320-ai-control` не удаляется.
