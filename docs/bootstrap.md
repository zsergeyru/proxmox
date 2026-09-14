# Публичный bootstrap-репозиторий

## Назначение

Для безопасных скриптов, которые должны запускаться непосредственно на чистом Proxmox-хосте или внутри новой bootstrap-VM без доступа к приватному Git, используется отдельный публичный репозиторий:

```text
https://github.com/zsergeyru/proxmox-bootstrap
```

Основной репозиторий инфраструктуры остаётся приватным:

```text
https://github.com/zsergeyru/proxmox
```

Распределение ответственности:

```text
zsergeyru/proxmox
→ документация инфраструктуры, архитектура, описания VM/LXC и политики

zsergeyru/proxmox-bootstrap
→ единственные канонические executable bootstrap-скрипты
```

Скрипты не дублируются между репозиториями.

## Zero-day recovery chain

На совершенно новом компьютере исходной точкой считаются только установленный Proxmox VE и доступ в интернет к публичному bootstrap-репозиторию.

Целевая последовательность:

```text
чистый Proxmox VE
→ create-template.sh
→ VM 9000 tpl-debian13
→ create-ai-control-vm.sh
→ VM 301 ai-control
→ install-ai-control.sh внутри 301
→ Hermes + WebUI + Proximo + SSH identities
→ оператор регистрирует показанный GitHub Deploy Key
→ Hermes клонирует приватный zsergeyru/proxmox
→ дальше инфраструктура восстанавливается/разворачивается из Git
```

Приватный Git **не нужен до тех пор, пока AI уже не развёрнут**. GitHub PAT в zero-day сценарии не используется.

Подробная спецификация двух скриптов находится в [`ai-control-bootstrap.md`](./ai-control-bootstrap.md).

## Почему отдельный публичный репозиторий

GitHub не позволяет сделать отдельный файл публичным внутри приватного репозитория. Поэтому для файлов, которые PVE или новая VM должны скачать напрямую через HTTPS без авторизации, используется отдельный public repository.

На Proxmox-хосте при этом не требуется устанавливать Git, клонировать весь инфраструктурный репозиторий или хранить GitHub PAT.

## Debian 13 template builder

Канонический builder-скрипт:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Прямая загрузка на PVE:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/create-template.sh \
  -o /root/create-template.sh

bash -n /root/create-template.sh
chmod +x /root/create-template.sh
/root/create-template.sh
```

GitHub token в этом сценарии не нужен.

## Планируемые скрипты 301 AI Control

На текущем этапе два следующих скрипта **спроектированы, но ещё не считаются реализованными/live-validated**:

```text
create-ai-control-vm.sh
install-ai-control.sh
```

После реализации их канонические экземпляры должны находиться только в `zsergeyru/proxmox-bootstrap`.

### `create-ai-control-vm.sh`

Запускается на PVE от `root` и отвечает за слой виртуализации:

```text
9000 tpl-debian13
→ Full Clone 301 ai-control
→ CPU/RAM/disk/network/Cloud-Init
→ protection=0
→ onboot=1
→ first start
→ QEMU Guest Agent/cloud-init health
```

Кроме самой VM этот скрипт создаёт только необходимую host-side bootstrap identity для Proximo, потому что PVE user/token/ACL нельзя безопасно поручать самому AI-гостю.

Целевая identity:

```text
user:  proximo@pve
token: proximo@pve!ai-control
privilege separation: enabled
pool: managed
```

Token secret передаётся непосредственно в `301` через QEMU Guest Agent и не должен оставаться постоянным plaintext-файлом на PVE.

Скрипт **не устанавливает Hermes/Proximo packages и не клонирует Git**.

### `install-ai-control.sh`

Запускается уже внутри `301` и отвечает за программный AI control plane.

Обязательная структура:

```text
/opt/ai-control/
├── agents/
│   └── hermes/
├── mcp/
│   └── proximo/
├── repos/
│   └── proxmox/
└── state/
```

Ключевое правило: Hermes устанавливается именно в:

```text
/opt/ai-control/agents/hermes
```

а общий Proximo — отдельно:

```text
/opt/ai-control/mcp/proximo
```

Installer:

- ставит runtime dependencies;
- устанавливает Hermes в `agents/hermes`;
- настраивает штатный Hermes WebUI, целевой порт `9119`;
- устанавливает Proximo в `mcp/proximo`;
- связывает Proximo с уже подготовленным PVE token;
- включает TLS verification и при необходимости использует pinned PVE certificate/CA bundle;
- запускает `proximo doctor`;
- создаёт две независимые SSH identity;
- показывает GitHub Deploy Key public key;
- после ручной регистрации ключа проверяет GitHub и клонирует private repo.

## SSH identities внутри 301

### Infrastructure key

```text
/home/ops/.ssh/ai_control_ed25519
/home/ops/.ssh/ai_control_ed25519.pub
```

Private key остаётся внутри `301`. Public key Hermes передаёт новым managed Debian VM через Cloud-Init до первого запуска.

### GitHub Deploy Key

```text
/home/ops/.ssh/github_proxmox_ed25519
/home/ops/.ssh/github_proxmox_ed25519.pub
```

Этот ключ используется только для `zsergeyru/proxmox`.

После первого запуска installer показывает `.pub`. Оператор добавляет его в:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Для `commit/push` включается `Allow write access`.

GitHub PAT не нужен.

После регистрации ключа целевой checkout находится в:

```text
/opt/ai-control/repos/proxmox
```

## Proximo

Целевой `301` использует **Proximo** как канонический Proxmox MCP.

Hard boundary задаётся отдельным privilege-separated PVE token и ACL. Дополнительно Hermes по умолчанию получает сокращённую MCP surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

Обычный AI workflow не получает прав на PVE host network, storage definitions, IAM/ACL, SDN, repositories/certificates и reboot/shutdown гипервизора.

После установки обязательно выполняется:

```bash
proximo doctor
```

и проверяется фактическая граница `can/cannot`.

## Повторный запуск

Оба будущих скрипта должны проектироваться как безопасно повторно запускаемые.

`create-ai-control-vm.sh`:

- не удаляет существующую правильную `301`;
- не делает новый clone поверх неё;
- не уменьшает диск;
- не ротирует Proximo token без явного recovery-флага;
- прекращает работу, если VMID `301` занят другим объектом.

`install-ai-control.sh`:

- не перегенерирует SSH keys;
- не сбрасывает provider credentials;
- не удаляет Git checkout;
- не меняет версию Hermes без явного upgrade-режима;
- повторяет health checks и доделывает безопасно пропущенные шаги.

## Правило изменения скриптов

При изменении любого публичного bootstrap-скрипта необходимо:

1. изменить его непосредственно в `zsergeyru/proxmox-bootstrap`;
2. убедиться, что файл не содержит секретов;
3. дождаться успешной проверки GitHub Actions;
4. обновить соответствующую документацию в приватном `zsergeyru/proxmox`;
5. выполнить реальный clean/live test для сценариев, которые CI не способен проверить на PVE.

Приватные executable-копии специально не хранятся.

## План проверки CI и live-test

После реализации CI публичного репозитория должен как минимум проверять:

- `create-template.sh` через `bash -n`;
- `create-ai-control-vm.sh` через `bash -n`;
- `install-ai-control.sh` через `bash -n`;
- встроенный в template builder Cloud-Init как YAML;
- встроенные template guest scripts через `bash -n`;
- whitespace errors.

CI не заменяет real PVE test.

Первый полный live-test zero-day цепочки:

```text
create-template.sh
→ Full Clone 9000 → 301 через create-ai-control-vm.sh
→ QGA/cloud-init health
→ install-ai-control.sh
→ Hermes WebUI
→ Proximo doctor
→ Deploy Key registration
→ Git clone в /opt/ai-control/repos/proxmox
→ Proximo Full Clone test guest + Cloud-Init infrastructure SSH public key
→ SSH from 301 to test guest
```

## Security boundary

`proxmox-bootstrap` публичный. В него запрещено помещать пароли/PIN, PAT/API tokens, приватные SSH/VPN/TLS ключи, реальные `.env`, credentials сервисов и иные нежелательные для публикации данные.

Секреты zero-day bootstrap (`Proximo token`, private SSH keys, model/provider credentials и WebUI secrets) создаются/передаются на месте и не находятся в исходных скриптах.

Полный backup `301` считается чувствительным, потому что содержит control-plane credentials.

## Текущий статус template

Текущая версия по умолчанию создаёт:

```text
VMID: 9000
Name: tpl-debian13
Template-Version: 4
```

### v2

2026-09-14 Template-Version 2 успешно проверена полной чистой сборкой и тестовым Full Clone на реальном PVE-хосте. Были подтверждены SSH по ключу, locked password, sudo, QEMU Agent, timesync, growpart/filesystem growth, очистка builder artifacts и уникальные machine-id/SSH host keys.

### v3

В v3 основная Proxmox console была переведена на:

```text
vga: serial0
xterm.js → ttyS0 → autologin ops
```

Технически схема работала, но тестирование показало, что serial console менее удобна как основная Web Console: это поток, а не framebuffer, старое содержимое экрана не сохраняется.

### v4

v4 возвращает:

```text
vga: std
Proxmox Console → noVNC/VGA → tty1 → autologin ops
```

и оставляет резервный канал:

```text
serial0: socket
xterm.js / qm terminal → ttyS0 → autologin ops
```

Во время теста выяснилась причина крупного шрифта в noVNC: исходный Debian genericcloud image загружался с `cloud-amd64` kernel без framebuffer. У тестовой VM:

```text
cloud-amd64 kernel → /sys/class/graphics/fb0 отсутствует
regular amd64 kernel → fb0 = 1280,800
```

Поэтому v4 автоматически:

1. устанавливает `linux-image-amd64`;
2. устанавливает `console-setup` и `console-setup-linux`;
3. настраивает `Fixed 8x16`;
4. удаляет `linux-image-*cloud-amd64`;
5. перезагружает builder;
6. проверяет, что реально загружено обычное ядро, `fb0` существует и сообщает валидное непустое разрешение, QEMU Guest Agent отвечает, `tty1` и `ttyS0` активны;
7. проверяет locked-пароли `ops`/`root` и эффективную SSH policy;
8. только после этого выполняет final cleanup и создаёт template.

Конкретное разрешение framebuffer не зашито как требование. На текущем PVE экспериментально получено `1280,800`; builder выводит фактическое значение в итоговом отчёте.

После создания template дополнительно проверяются `template=1`, `protection=1`, `agent=1`, `vga=std`, `serial0=socket`, `ciuser=ops`, `ciupgrade=0`, DHCP и отсутствие builder-only `cicustom`.

### Protection при клонировании

Base template `9000` защищён:

```text
protection=1
```

Обычные Full Clone по умолчанию должны иметь:

```text
protection=0
```

Deploy/AI workflow обязан явно выставлять `protection=0`, если паспорт конкретной VM не требует обратного.

### Full Clone vs Linked Clone

Для рабочих VM принят Full Clone. Проверять тип клона только по словам автоматизации нельзя.

Надёжные признаки Full Clone:

```text
Proxmox task log:
create full clone of drive scsi0 (...)
```

и на LVM-thin:

```bash
lvs -o lv_name,origin
```

У основного диска настоящего Full Clone поле `Origin` пустое. Если там `base-9000-disk-0`, создан Linked Clone.
