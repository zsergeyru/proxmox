# Публичный bootstrap-репозиторий

## Назначение

Для безопасных скриптов, которые должны запускаться непосредственно на чистом Proxmox-хосте без установки Git и без GitHub-токена, используется отдельный публичный репозиторий:

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

Каноническая последовательность:

```text
чистый Proxmox VE
→ create-template.sh
→ VM 9000 tpl-debian13
→ bootstrap-ai-control.sh
→ VM 301 ai-control
→ Hermes + Dashboard + Proximo + SSH identities
→ оператор регистрирует показанный GitHub Deploy Key
→ Hermes клонирует приватный zsergeyru/proxmox
→ дальше инфраструктура восстанавливается/разворачивается из Git
```

Приватный Git **не нужен до тех пор, пока AI уже не развёрнут**. GitHub PAT в zero-day сценарии не используется.

## Почему отдельный репозиторий

GitHub не позволяет сделать отдельный файл публичным внутри приватного репозитория. Поэтому для файлов, которые PVE должен скачивать напрямую через HTTPS без авторизации, используется отдельный public repository.

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

## 301 AI Control bootstrap

После появления `9000 tpl-debian13` запускается второй публичный скрипт:

```text
zsergeyru/proxmox-bootstrap/bootstrap-ai-control.sh
```

Прямая загрузка:

```bash
curl -fsSL \
  https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-ai-control.sh \
  -o /root/bootstrap-ai-control.sh

bash -n /root/bootstrap-ai-control.sh
chmod +x /root/bootstrap-ai-control.sh
/root/bootstrap-ai-control.sh
```

По умолчанию он создаёт:

```text
VMID: 301
Name: ai-control
Template: Full Clone from 9000
CPU: 2 cores
RAM: 4096 MiB
Disk: 24 GiB
IPv4: 192.168.3.1/16
Gateway: 192.168.1.1
Dashboard: tcp/9119
```

Параметры можно переопределять environment variables самого bootstrap-скрипта.

### Почему bootstrap 301 не требует SSH

На момент создания `301` SSH identity control plane ещё не существует. Поэтому первичная настройка выполняется через QEMU Guest Agent:

```text
PVE root
→ qm guest exec
→ 301
→ установить Hermes/Proximo
→ создать ключи
→ настроить Dashboard
```

После этого прямой SSH становится штатным bootstrap/diagnostic/emergency каналом уже для следующих гостей.

### Что создаётся внутри 301

Две разные SSH identity:

```text
~/.ssh/ai_control_ed25519
~/.ssh/ai_control_ed25519.pub
→ вход ops в будущие managed Debian VM

~/.ssh/github_proxmox_ed25519
~/.ssh/github_proxmox_ed25519.pub
→ только private repo zsergeyru/proxmox
```

Private keys никогда не выводятся. GitHub public key показывается оператору в конце скрипта.

### GitHub после AI, а не до него

Оператор вручную добавляет показанный `.pub` в:

```text
zsergeyru/proxmox
→ Settings
→ Deploy keys
```

Если Hermes должен делать `commit/push`, включается `Allow write access`.

После этого Hermes может выполнить:

```bash
git clone git@github.com:zsergeyru/proxmox.git ~/proxmox
```

До регистрации Deploy Key не требуется ни PAT, ни personal SSH key пользователя.

### Proximo

Целевой `301` использует **Proximo** как канонический Proxmox MCP.

Bootstrap создаёт отдельные:

```text
Proxmox user:  proximo@pve
API token:     proximo@pve!ai-control
resource pool: managed
```

Token создаётся с privilege separation. Его secret показывается Proxmox только один раз и напрямую передаётся через QEMU Guest Agent в `301`; постоянной secret-копии на PVE bootstrap не создаёт.

В Hermes публикуется сокращённая MCP surface:

```text
PROXIMO_TOOLSETS=pve.guests
```

При этом hard boundary остаётся на уровне Proxmox ACL: AI control управляет managed guests, но не получает штатных прав на PVE host network, storage definitions, IAM/ACL, SDN, repositories/certificates и reboot/shutdown гипервизора.

### Hermes Web Dashboard

Используется встроенный Hermes Dashboard:

```text
http://192.168.3.1:9119/
```

Bootstrap создаёт случайный пароль `admin`, сохраняет только его scrypt hash и stable signing secret, а исходный пароль показывает один раз. Через Dashboard оператор затем задаёт model/provider credentials.

Это единственный обязательный секретный шаг, который новый сервер не может выполнить самостоятельно: провайдер модели должен быть авторизован человеком.

### Повторный запуск

`bootstrap-ai-control.sh` спроектирован как продолжение bootstrap, а не destructive rebuild:

- существующую `301` с правильным именем не удаляет и не клонирует заново;
- существующие SSH keys не меняет;
- Hermes повторно не переустанавливает;
- Dashboard password повторно не генерирует;
- если Proxmox token существует, а его secret-файл внутри `301` потерян, token пересоздаётся, потому что старый secret не восстановим.

Если VMID `301` занят другим объектом, скрипт прекращает работу.

## Правило изменения скриптов

При изменении любого публичного bootstrap-скрипта необходимо:

1. изменить его непосредственно в `zsergeyru/proxmox-bootstrap`;
2. убедиться, что файл не содержит секретов;
3. дождаться успешной проверки GitHub Actions;
4. обновить соответствующую документацию в приватном `zsergeyru/proxmox`;
5. выполнить реальный clean/live test для сценариев, которые CI не способен проверить на PVE.

Приватные executable-копии специально не хранятся.

## Проверка в GitHub Actions

CI публичного репозитория проверяет:

- `create-template.sh` через `bash -n`;
- `bootstrap-ai-control.sh` через `bash -n`;
- встроенный в template builder Cloud-Init как YAML;
- встроенный `/usr/local/sbin/template-bootstrap` через `bash -n`;
- встроенный `/usr/local/sbin/template-finalize` через `bash -n`;
- whitespace errors.

CI ловит ошибки структуры heredoc/YAML и синтаксиса вложенных guest-скриптов, но не заменяет реальный clean build на PVE.

Для `bootstrap-ai-control.sh` отдельно требуется первый live-test цепочки:

```text
Full Clone 9000 → 301
→ QGA bootstrap
→ Hermes Dashboard
→ Proximo doctor
→ Deploy Key registration
→ Git clone
→ Proximo Full Clone test guest + Cloud-Init SSH public key
→ SSH from 301 to test guest
```

## Security boundary

`proxmox-bootstrap` публичный. В него запрещено помещать пароли/PIN, PAT/API tokens, приватные SSH/VPN/TLS ключи, реальные `.env`, credentials сервисов и иные нежелательные для публикации данные.

Секреты, которые создаются во время bootstrap (`Proximo token`, private SSH keys, Dashboard auth secret), генерируются на месте и не находятся в исходном файле.

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
