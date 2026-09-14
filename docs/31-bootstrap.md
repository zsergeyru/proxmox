# Публичный bootstrap-репозиторий

## Назначение

Публичный репозиторий:

```text
https://github.com/zsergeyru/proxmox-bootstrap
```

содержит канонические executable bootstrap-скрипты, которые можно скачать на чистый PVE или новую bootstrap-VM без доступа к приватному Git.

Приватный `zsergeyru/proxmox` остаётся source of truth для архитектуры, `guest.yaml`, `rootfs/`, политик и дальнейшего desired state. Скрипты между репозиториями не дублируются.

## Новая zero-day цепочка

Целевой путь нового физического сервера:

```text
чистый Proxmox VE
→ init-pve.sh
   ├── bootstrap packages
   ├── read-only GitHub Deploy Key для PVE
   ├── PVE roles/users/tokens/pool
   ├── create-template.sh
   └── deploy-guest
→ 9000 tpl-debian13
→ PVE умеет читать private zsergeyru/proxmox
→ deploy-guest <VMID> --apply
→ дальнейшее развёртывание не зависит от AI
```

После этого AI Control — уже обычный управляемый guest/platform:

```text
deploy/create 301
→ prepare-ai-control.sh внутри 301
→ Docker + Proximo + SSH/Git platform готова
→ install-ai-agent.sh --agent hermes
→ Hermes + Dashboard
```

Полная спецификация инициализации PVE: [`20-pve-initialization.md`](./20-pve-initialization.md).

Спецификация manifests: [`30-guest-manifest.md`](./30-guest-manifest.md).

Спецификация AI Control: [`51-ai-control-bootstrap.md`](./51-ai-control-bootstrap.md).

## Почему Git на PVE допустим

PVE получает **отдельный read-only GitHub Deploy Key** для единственного приватного repo `zsergeyru/proxmox`.

SSH Deploy Key работает с Git transport. Он не является HTTP/API token, поэтому обычный `curl` к private raw/Contents API не может использовать этот SSH key. Для HTTPS/API понадобился бы другой credential: GitHub App token или access token.

Поэтому целевая простая схема:

```text
PVE read-only Deploy Key
→ git clone/fetch over SSH
→ /var/lib/proxmox-deployer/repo
```

`git` на PVE используется как клиент source-of-truth и не является отдельным сервисом.

## `init-pve.sh` — планируемый этап 0

Скрипт должен жить в `zsergeyru/proxmox-bootstrap` и быть безопасно повторно запускаемым.

Задачи:

1. проверить, что запуск идёт от root на Proxmox VE;
2. установить минимальные bootstrap packages (`git`, `openssh-client`, `python3`, `python3-yaml`, `curl`, `jq`, CA tools);
3. создать отдельный read-only SSH Deploy Key PVE;
4. вывести `.pub` для ручной регистрации в `zsergeyru/proxmox` без `Allow write access`;
5. после регистрации клонировать/обновлять repo в `/var/lib/proxmox-deployer/repo`;
6. создать утверждённые resource pools, roles, users и API tokens проекта;
7. скачать и запустить канонический `create-template.sh`, если template `9000` ещё не существует/не соответствует policy;
8. установить PVE-side `deploy-guest`;
9. вывести итоговый health/status report.

Первый запуск может закончить все шаги, которые не требуют GitHub authorization, показать public key и остановиться. После регистрации ключа повторный запуск продолжает процесс без регенерации identity.

## `deploy-guest`

PVE-side deployer получает VMID:

```bash
deploy-guest 311
```

и по умолчанию только строит PLAN.

Применение:

```bash
deploy-guest 311 --apply
```

Источник данных:

```text
/var/lib/proxmox-deployer/repo/guests/311-*/guest.yaml
```

Он работает напрямую штатными PVE CLI/API tools:

```text
qm
pct
pvesh
pvesm
```

и не зависит от Hermes, 301 или Proximo.

## `create-template.sh`

Создаёт базовый Debian 13 template:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

Template содержит Cloud-Init, QEMU Guest Agent, пользователя `ops`, locked passwords, SSH public-key auth, regular `linux-image-amd64`, VGA/noVNC + serial fallback и очищенные machine-specific identifiers. После успешной проверки template получает `protection=1`.

`init-pve.sh` должен вызывать именно этот публичный канонический builder, а не дублировать его код.

## AI Control после базовой инициализации

Текущие AI bootstrap stages сохраняются как отдельные слои.

### `create-ai-control-vm.sh`

Выполняется от `root` на PVE и создаёт/настраивает `301`, включая host-side identity Proximo. После появления универсального `deploy-guest` этот специальный скрипт может со временем стать тонкой совместимой обёрткой либо быть заменён общим manifest-driven deploy, но пока остаётся действующим bootstrap-инструментом.

### `prepare-ai-control.sh`

Выполняется от `root` внутри `301` и готовит общую AI-платформу без установки конкретного агента.

Он устанавливает:

- Docker Engine;
- Docker Buildx;
- Docker Compose plugin;
- Git/OpenSSH/curl/jq/OpenSSL;
- Python 3/venv/pip и build tooling;
- общий Proximo MCP.

Создаёт:

```text
/opt/ai-control/
├── agents/        # пока может быть пуст
├── mcp/proximo/
├── ssh/
├── repos/
└── state/
```

Также создаёт `ai_control_ed25519(.pub)` и отдельный `github_proxmox_repo_ed25519(.pub)` для AI workflow.

### `install-ai-agent.sh`

Устанавливает только выбранного агента:

```bash
install-ai-agent.sh --agent hermes
```

Общее правило:

```text
/opt/ai-control/agents/<agent>/
```

Сейчас реализован installer для Hermes. Добавление следующего агента не должно требовать изменений PVE bootstrap или общей AI platform.

## Разные GitHub identities

Не смешивать роли:

```text
PVE Deploy Key
→ private key на PVE
→ read-only zsergeyru/proxmox
→ нужен init-pve/deploy-guest

301 Deploy Key
→ private key в 301
→ может иметь write access
→ нужен AI для pull/commit/push
```

Ключи разные и не копируются друг другу.

## CI и live-test

Публичный `proxmox-bootstrap` должен проверять синтаксис всех executable scripts. CI не заменяет реальный PVE test.

После реализации `init-pve.sh`/`deploy-guest` первый live-test должен пройти цепочку:

```text
чистый PVE
→ init-pve.sh
→ register read-only Deploy Key
→ повторный init-pve.sh
→ private repo checkout
→ 9000 template
→ deploy-guest <test-vmid>
→ PLAN
→ --apply
→ verify VM/LXC
```

После этого отдельно проверяется AI Control:

```text
301
→ prepare-ai-control.sh
→ install-ai-agent.sh --agent hermes
→ Proximo doctor
→ Dashboard
→ AI Git identity
```

## Security boundary

Публичный `proxmox-bootstrap` не должен содержать PAT/API tokens, passwords/PIN, private SSH/VPN/TLS keys, реальные `.env` с secrets или model-provider credentials.

PVE read-only private Deploy Key создаётся локально на PVE. API token secrets создаются на месте и не попадают в Git. Полный backup/диск PVE и `301` следует считать чувствительными с точки зрения содержащихся credentials.
