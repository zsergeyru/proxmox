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

## Почему отдельный репозиторий

GitHub не позволяет сделать отдельный файл публичным внутри приватного репозитория. Поэтому для файлов, которые PVE должен скачивать напрямую через HTTPS без авторизации, используется отдельный public repository.

На Proxmox-хосте при этом не требуется устанавливать Git, клонировать весь инфраструктурный репозиторий или хранить GitHub PAT.

## Debian 13 template builder

Единственный исходный файл builder-скрипта:

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

## Правило изменения скрипта

При изменении template builder необходимо:

1. изменить `create-template.sh` непосредственно в `zsergeyru/proxmox-bootstrap`;
2. убедиться, что файл не содержит секретов;
3. дождаться успешной проверки GitHub Actions;
4. обновить соответствующую документацию в приватном `zsergeyru/proxmox`;
5. использовать тот же raw URL для запуска на PVE.

Приватная executable-копия специально не хранится.

## Проверка в GitHub Actions

CI публичного репозитория проверяет:

- внешний `create-template.sh` через `bash -n`;
- встроенный Cloud-Init как YAML;
- встроенный `/usr/local/sbin/template-bootstrap` через `bash -n`;
- встроенный `/usr/local/sbin/template-finalize` через `bash -n`;
- whitespace errors.

CI ловит ошибки структуры heredoc/YAML и синтаксиса вложенных guest-скриптов, но не заменяет реальный clean build на PVE.

## Security boundary

`proxmox-bootstrap` публичный. В него запрещено помещать пароли/PIN, PAT/API tokens, приватные SSH/VPN/TLS ключи, реальные `.env`, credentials сервисов и иные нежелательные для публикации данные.

## Текущий статус

Публично опубликован один builder-скрипт:

```text
create-template.sh
```

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

Таким образом, проверяется не только наличие пакетов на диске, но и фактическая загрузка VM с нужным ядром и console stack.

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
