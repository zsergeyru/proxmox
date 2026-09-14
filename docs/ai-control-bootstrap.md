# Zero-day bootstrap `301-ai-control`

## Статус

Двухэтапный bootstrap **реализован** в публичном репозитории `zsergeyru/proxmox-bootstrap`:

```text
create-ai-control-vm.sh
install-ai-control.sh
```

Синтаксис скриптов проверяется CI. Полная цепочка ещё должна пройти первый live-test на реальном PVE перед выводом `320-ai-control` из эксплуатации.

Канонический executable-код хранится только в `zsergeyru/proxmox-bootstrap`; приватный `zsergeyru/proxmox` хранит архитектуру и desired state.

## Целевая цепочка

```text
чистый Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh на PVE
→ 301 ai-control
→ install-ai-control.sh внутри 301
→ Hermes + Dashboard + Proximo + SSH identities
→ оператор регистрирует GitHub Deploy Key
→ повторный install-ai-control.sh
→ clone zsergeyru/proxmox
→ Hermes продолжает развёртывание инфраструктуры из Git
```

До установки AI доступ к приватному Git не требуется. GitHub PAT в zero-day пути не используется.

## Этап 1 — `create-ai-control-vm.sh`

Скрипт выполняется от `root` на PVE. Его зона ответственности — гипервизор и bootstrap credential для Proximo. **Hermes на этом этапе не устанавливается.**

Параметры по умолчанию:

```text
VMID:       301
Name:       ai-control
Template:   9000 tpl-debian13
Clone:      Full Clone
CPU:        2 cores
RAM:        4096 MiB
Disk:       24 GiB
Bridge:     vmbr0
IPv4:       192.168.3.1/16
Gateway:    192.168.1.1
CI user:    ops
On boot:    yes
Protection: no
```

Последовательность:

```text
проверить template/storage
→ Full Clone 9000 → 301
→ protection=0
→ CPU/RAM/disk/network/Cloud-Init
→ start
→ дождаться QEMU Guest Agent и cloud-init
→ создать Proximo PVE identity/ACL
→ передать token secret и PVE CA внутрь 301 через QGA
→ завершить, не устанавливая AI software
```

### Proximo management identity

Создаётся отдельная privilege-separated identity:

```text
user:  proximo@pve
token: proximo@pve!ai-control
pool:  managed
```

Базовая политика:

- read/audit — доступен;
- template `9000` — read/clone, без изменения template;
- write — только в `managed` pool и на разрешённых storage;
- host network, IAM/ACL administration, SDN, storage definitions и reboot/shutdown PVE не являются штатной зоной AI.

Одинаковые ACL выдаются backing user и privilege-separated token, поскольку эффективные права token являются пересечением их разрешений.

`301` не помещается в собственную обычную self-managed write-зону.

### Материалы, передаваемые в 301

```text
/etc/ai-control/secrets/proximo-pve-token
/etc/ai-control/proximo/pve-root-ca.pem
/etc/ai-control/proximo/proximo.env
```

Token-файл содержит строку формата:

```text
proximo@pve!ai-control=SECRET
```

Он не выводится пользователю и не сохраняется в Git. TLS verification для Proximo остаётся включённой; для доверия используется PVE CA.

Если PVE token уже существует и secret-файл в `301` присутствует, token не меняется. Если token существует, а secret утрачен, скрипт останавливается. Ротация допускается только явно:

```bash
ROTATE_PROXIMO_TOKEN=1 ./create-ai-control-vm.sh
```

## Этап 2 — `install-ai-control.sh`

Второй скрипт выполняется от `root` **внутри `301`**. Он не создаёт PVE users/tokens/ACL и не расширяет полномочия control plane.

### Каноническая структура

```text
/opt/ai-control/
├── agents/
│   └── hermes/
│       └── hermes-agent/
├── mcp/
│   └── proximo/
│       ├── venv/
│       └── run.sh
├── ssh/
│   ├── ai_control_ed25519
│   ├── ai_control_ed25519.pub
│   ├── github_proxmox_ed25519
│   └── github_proxmox_ed25519.pub
└── repos/
    └── proxmox/
```

Главное правило проекта:

> Любой конкретный AI-агент устанавливается в `/opt/ai-control/agents/<agent>/`. Общие MCP размещаются отдельно в `/opt/ai-control/mcp/`.

### Hermes

Hermes устанавливается официальным installer с явными путями:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
```

Таким образом код и persistent-конфигурация Hermes не оказываются в обычном `~/.hermes` и не смешиваются с MCP или Git checkout.

Штатный Hermes Dashboard запускается через systemd на:

```text
tcp/9119
```

Для доступа создаются локальные Basic Auth credentials. Они хранятся в `/etc/ai-control/secrets/`, а runtime `.env` Hermes — в каталоге самого агента. Model/provider credential оператор задаёт отдельно после первого запуска; в Git он не хранится.

### Proximo

Proximo устанавливается отдельно от агента:

```text
/opt/ai-control/mcp/proximo
```

Текущий bootstrap фиксирует пакет:

```text
proximo-proxmox==0.40.0
```

Запуск происходит через wrapper, который читает host-provisioned `/etc/ai-control/proximo/proximo.env`.

Дополнительное ограничение tool surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

Перед регистрацией MCP у Hermes выполняется:

```bash
proximo doctor
```

Результат сохраняется для диагностики. Основная граница безопасности всё равно задаётся PVE token/ACL, а не только набором MCP tools.

## SSH identities

Второй этап создаёт две независимые пары.

### Infrastructure identity

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся только внутри `301`. Public key передаётся будущим managed Debian VM пользователю `ops` через Cloud-Init **до первого запуска**. Это даёт Hermes прямой SSH-канал для bootstrap/diagnostics/emergency.

### GitHub Deploy Key

```text
/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Этот ключ используется только для `zsergeyru/proxmox`. Installer выводит `.pub`, после чего оператор вручную добавляет его:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
→ Add deploy key
→ Allow write access (если Hermes должен commit/push)
```

После регистрации тот же `install-ai-control.sh` можно запустить повторно. Ключ не регенерируется; installer проверяет `git ls-remote` и клонирует:

```text
/opt/ai-control/repos/proxmox
```

## Повторный запуск

`create-ai-control-vm.sh`:

- не перезаписывает чужую VM с VMID `301`;
- не делает clone поверх существующей правильной `301`;
- не уменьшает диск;
- не ротирует Proximo token без `ROTATE_PROXIMO_TOKEN=1`.

`install-ai-control.sh`:

- не перегенерирует существующие SSH keys;
- повторно использует Dashboard credentials;
- не удаляет существующий Git checkout;
- если GitHub Deploy Key уже зарегистрирован, завершает Git onboarding автоматически.

## Совместимая обёртка

Старое имя сохранено как удобная обёртка:

```text
bootstrap-ai-control.sh
```

Она последовательно запускает оба канонических этапа. Для отладки и восстановления предпочтительно запускать два этапа отдельно — так граница между PVE provisioning и guest software очевидна.

## Первый live-test

Перед тем как считать `301` production control plane, проверить:

1. настоящий Full Clone `9000 → 301`;
2. QEMU Agent и cloud-init;
3. Hermes именно в `/opt/ai-control/agents/hermes/`;
4. Dashboard и авторизацию;
5. `proximo doctor` и фактический `can/cannot`;
6. регистрацию GitHub Deploy Key и clone в `/opt/ai-control/repos/proxmox`;
7. создание тестовой managed VM через Proximo;
8. передачу `/opt/ai-control/ssh/ai_control_ed25519.pub` через Cloud-Init;
9. SSH `301 → test VM` как `ops`;
10. отсутствие у Proximo прав на PVE host/IAM/network administration.

До этого `320-ai-control` сохраняется как bootstrap/recovery узел.

## Security boundary

Публичный `proxmox-bootstrap` не содержит secrets. Не помещать туда PAT/API tokens, private SSH keys, пароли, реальные `.env`, VPN/TLS private keys или model-provider credentials.

Полный backup `301` является чувствительным: в нём находятся Proximo token, private SSH identities и credentials AI control plane.
