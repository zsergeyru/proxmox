# Zero-day bootstrap `301-ai-control`

## Статус

Zero-day bootstrap разделён на **три канонических этапа** и реализуется публичными скриптами из `zsergeyru/proxmox-bootstrap`:

```text
1. create-ai-control-vm.sh
2. prepare-ai-control.sh
3. install-ai-agent.sh --agent <agent>
```

Это разделение специально сделано так, чтобы VM и общая AI-платформа не зависели от конкретного агента. Сегодня поддерживается Hermes; в дальнейшем можно добавить Agent Zero или другой агент, не меняя этапы 1 и 2.

Скрипты проверяются CI по Bash-синтаксису. Полная цепочка должна пройти live-test на реальном PVE до вывода `320-ai-control` из эксплуатации.

## Целевая цепочка

```text
чистый Proxmox VE
→ create-template.sh
→ 9000 tpl-debian13
→ create-ai-control-vm.sh              # PVE
→ 301 ai-control
→ prepare-ai-control.sh                # внутри 301
→ общая AI-платформа готова
→ install-ai-agent.sh --agent hermes   # внутри 301
→ Hermes/WebUI работает
→ оператор регистрирует GitHub Deploy Key
→ повторный install-ai-agent.sh --agent hermes
→ clone zsergeyru/proxmox
→ AI продолжает развёртывание инфраструктуры из Git
```

До установки AI приватный Git не нужен. GitHub PAT в zero-day пути не используется.

---

# Этап 1 — `create-ai-control-vm.sh`

Скрипт выполняется от `root` на PVE. Его зона ответственности — **только уровень виртуализации и host-side credential для Proximo**.

Он делает:

```text
проверка PVE/template/storage/network
→ Full Clone 9000 → 301
→ protection=0
→ CPU/RAM/disk/network/Cloud-Init
→ ciuser=ops
→ onboot=1
→ start
→ QEMU Guest Agent/cloud-init health
→ создать Proximo PVE identity/ACL
→ передать token/config/PVE CA внутрь 301 через QGA
```

Defaults:

```text
VMID:       301
Name:       ai-control
Template:   9000 tpl-debian13
CPU:        2 cores
RAM:        4096 MiB
Disk:       24 GiB
Bridge:     vmbr0
IPv4:       192.168.3.1/16
Gateway:    192.168.1.1
On boot:    yes
Protection: no
```

Он **не устанавливает** Docker, Proximo package, Hermes или другой AI-агент и не работает с private Git.

## Proximo management identity

Host-side bootstrap создаёт отдельную privilege-separated identity:

```text
user:  proximo@pve
token: proximo@pve!ai-control
pool:  managed
```

Hard boundary задаётся PVE ACL. `301` не должен получать возможность менять IAM/ACL, host network, storage definitions, SDN, certificates/repositories или reboot/shutdown самого PVE.

Token secret передаётся напрямую в `301` и хранится там в закрытом файле. Если secret утрачен, ротация выполняется только явно через `ROTATE_PROXIMO_TOKEN=1`.

---

# Этап 2 — `prepare-ai-control.sh`

Скрипт выполняется от `root` **внутри `301`** и подготавливает общую платформу, не устанавливая ни одного AI-агента.

Главный принцип:

> Всё, что нужно нескольким возможным агентам, относится к platform preparation, а не к Hermes.

## Что устанавливается

Общий runtime и инструменты:

- Git;
- OpenSSH client;
- Python 3 + venv/pip;
- curl, jq, OpenSSL, CA/TLS tooling;
- build dependencies;
- **Docker Engine**;
- **Docker Buildx**;
- **Docker Compose plugin**;
- общий Proximo MCP.

Docker устанавливается из официального Docker APT repository и является свойством `301-ai-control`, а не конкретного агента. Даже если Hermes использует собственный Python installer, Docker уже готов для другого агента или дальнейших сервисов control plane.

## Общая структура

После этапа 2 существует:

```text
/opt/ai-control/
├── agents/                    # пока может быть пуст
├── mcp/
│   └── proximo/
│       ├── venv/
│       └── run.sh
├── ssh/
│   ├── ai_control_ed25519
│   ├── ai_control_ed25519.pub
│   ├── github_proxmox_ed25519
│   └── github_proxmox_ed25519.pub
├── repos/                     # private repo ещё не обязан существовать
└── state/
    ├── platform-prepared
    └── proximo-doctor.json
```

## Proximo

Proximo — общая capability control plane и поэтому размещается не внутри агента:

```text
/opt/ai-control/mcp/proximo/
```

Текущий bootstrap фиксирует:

```text
proximo-proxmox==0.40.0
PROXIMO_TOOLSETS=pve.guests
```

На этапе подготовки выполняется `proximo doctor`; результат сохраняется в `/opt/ai-control/state/proximo-doctor.json`.

Регистрация Proximo в конкретном AI-клиенте выполняется уже этапом 3, потому что способ подключения MCP зависит от агента.

## Docker

Этап 2 обязан оставить рабочими:

```text
docker version
docker compose version
systemctl is-active docker
```

Пользователь `ops` добавляется в группу `docker`. Нужно помнить, что членство в группе `docker` фактически даёт высокий уровень доступа внутри VM; для `ops` это допустимо, поскольку он уже является административным пользователем `301` с `NOPASSWD sudo`.

## SSH identities

Этап 2 создаёт две независимые пары.

### Infrastructure identity

```text
/opt/ai-control/ssh/ai_control_ed25519
/opt/ai-control/ssh/ai_control_ed25519.pub
```

Private key остаётся только внутри `301`. Public key передаётся будущим managed Debian VM через Cloud-Init пользователю `ops` до первого запуска.

### GitHub Deploy Key

```text
/opt/ai-control/ssh/github_proxmox_ed25519
/opt/ai-control/ssh/github_proxmox_ed25519.pub
```

Этот ключ используется только для `git@github.com:zsergeyru/proxmox.git`.

Этап 2 выводит `.pub`, но **не клонирует private repo**. Это сохраняет принятое правило: Git не нужен до того момента, когда AI уже установлен.

---

# Этап 3 — `install-ai-agent.sh`

Третий скрипт отвечает только за **конкретного AI-агента**.

Вызов:

```bash
install-ai-agent.sh --agent hermes
```

Архитектура параметризована именем агента:

```text
/opt/ai-control/agents/<agent>/
```

Сейчас реализован adapter для:

```text
hermes
```

Если позже добавляется новый агент, его installer/configuration добавляются в этап 3. `create-ai-control-vm.sh` и `prepare-ai-control.sh` при этом не меняются.

## Hermes

Hermes размещается строго в:

```text
HERMES_HOME=/opt/ai-control/agents/hermes
code=/opt/ai-control/agents/hermes/hermes-agent
```

Этап 3:

- устанавливает Hermes официальным installer;
- подключает уже готовый общий `/opt/ai-control/mcp/proximo/run.sh` как MCP;
- настраивает Hermes Dashboard;
- создаёт/сохраняет Dashboard credentials;
- создаёт systemd service;
- проверяет, что Dashboard запущен;
- не переустанавливает Docker и Proximo.

Dashboard использует проектный порт:

```text
tcp/9119
```

Model/provider credential задаётся отдельно после первого запуска и не хранится в Git.

## Git после установки агента

После того как выбранный агент уже установлен, этап 3 проверяет GitHub Deploy Key.

Если ключ уже зарегистрирован в:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

то выполняется:

```text
git ls-remote
→ clone/pull
→ /opt/ai-control/repos/proxmox
```

Если ключ ещё не зарегистрирован, installer показывает тот же `.pub` и завершается без ошибки Git onboarding. После регистрации ключа запускается та же команда повторно; существующий агент, keys и credentials переиспользуются.

Для автономного `commit/push` Deploy Key получает `Allow write access`.

---

# Почему именно три этапа

Разделение обязанностей:

```text
create-ai-control-vm.sh
→ Proxmox infrastructure

prepare-ai-control.sh
→ reusable AI platform

install-ai-agent.sh --agent <name>
→ конкретный агент и его UI/integration
```

Это позволяет:

- пересоздать VM без знания конкретного агента;
- один раз подготовить Docker/Proximo/SSH/Git tooling;
- заменить Hermes другим агентом без изменения PVE bootstrap;
- установить несколько агентов параллельно в разных каталогах;
- не дублировать Proximo, Docker и SSH keys между агентами;
- обновлять агента отдельно от platform runtime.

## Несколько агентов

Допустимая будущая структура:

```text
/opt/ai-control/agents/
├── hermes/
├── agent-zero/
└── <future-agent>/
```

Все они могут использовать общие:

```text
/opt/ai-control/mcp/proximo/
/opt/ai-control/ssh/
/opt/ai-control/repos/proxmox/
Docker Engine
```

Конкретному агенту доступ к этим capabilities выдаётся его installer/configuration, а не копированием общих компонентов внутрь каталога агента.

---

# Повторный запуск

`create-ai-control-vm.sh`:

- не перезаписывает чужой VMID `301`;
- не делает clone поверх существующей правильной `301`;
- не уменьшает disk;
- не ротирует Proximo token без явного флага.

`prepare-ai-control.sh`:

- не удаляет существующие Docker data;
- не регенерирует SSH identities;
- переиспользует Proximo runtime;
- повторно проверяет `proximo doctor`;
- не устанавливает AI-агента.

`install-ai-agent.sh`:

- работает только с выбранным `/opt/ai-control/agents/<agent>`;
- не меняет common SSH identities;
- не переустанавливает common Proximo/Docker;
- переиспользует agent credentials/config;
- после регистрации Deploy Key завершает Git onboarding.

## Convenience wrapper

`bootstrap-ai-control.sh` остаётся только удобной обёрткой и последовательно вызывает три канонических этапа. По умолчанию:

```text
AI_AGENT=hermes
```

Для диагностики и recovery предпочтительно запускать три этапа отдельно.

---

# Live-test перед production

Нужно проверить:

1. `create-ai-control-vm.sh` создаёт настоящий Full Clone `9000 → 301`;
2. `prepare-ai-control.sh` устанавливает Docker Engine + Compose;
3. Proximo находится только в `/opt/ai-control/mcp/proximo`;
4. `proximo doctor` подтверждает ожидаемый `can/cannot`;
5. обе SSH identity существуют и private keys не покидают `301`;
6. `install-ai-agent.sh --agent hermes` устанавливает Hermes только в `/opt/ai-control/agents/hermes`;
7. Dashboard работает и защищён auth;
8. GitHub Deploy Key регистрируется и repo клонируется в `/opt/ai-control/repos/proxmox`;
9. Proximo создаёт test managed VM из `9000`;
10. `ai_control_ed25519.pub` передаётся test VM через Cloud-Init;
11. `301` входит по SSH в test VM как `ops`;
12. у Proximo отсутствуют host/IAM/network administrative rights.

До завершения live-test `320-ai-control` остаётся bootstrap/recovery узлом.

## Security boundary

Публичный `proxmox-bootstrap` не содержит secrets. Запрещено хранить там PAT/API tokens, private SSH/VPN/TLS keys, passwords, реальные `.env` или model-provider credentials.

Полный backup `301` является чувствительным, потому что содержит Proximo token, private SSH identities и credentials установленных AI-агентов.
