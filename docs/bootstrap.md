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

На Proxmox-хосте при этом не требуется:

- устанавливать Git;
- клонировать весь инфраструктурный репозиторий;
- хранить GitHub PAT;
- давать хосту постоянный доступ к приватному GitHub-репозиторию.

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
4. при изменении поведения или параметров обновить соответствующую документацию в приватном `zsergeyru/proxmox`;
5. использовать этот же raw URL для запуска на PVE.

Приватная копия executable-файла специально не хранится.

## Security boundary

`proxmox-bootstrap` является публичным репозиторием. В него запрещено помещать:

- пароли и PIN;
- GitHub PAT/API tokens;
- приватные SSH-ключи;
- WireGuard/Amnezia/VPN private keys и preshared keys;
- `.env` с реальными секретами;
- приватные TLS-ключи и client certificates;
- credentials сервисов;
- иные данные, публикация которых нежелательна.

Допустимы только самодостаточные bootstrap-скрипты и документация, рассчитанные на публичное чтение.

## Текущий статус

Публично опубликован единственный builder-скрипт:

```text
create-template.sh
```

Текущая версия builder по умолчанию создаёт Debian 13 template `tpl-debian13`, VMID `9000`, `Template-Version: 3`.

### Проверенная v2

**2026-09-14: Template-Version 2 успешно проверена полной чистой сборкой и тестовым Full Clone на реальном PVE-хосте.** Сборка прошла весь цикл: загрузка и SHA-512-проверка Debian genericcloud image, импорт и resize диска, временный Cloud-Init, bootstrap, QEMU Guest Agent, успешное завершение Cloud-Init final stage, финальная очистка, shutdown, регенерация штатного Cloud-Init, `qm template` и `protection=1`.

Во время первого прогона v2 были выявлены и исправлены две ошибки порядка выполнения Cloud-Init:

- `locale: en_US.UTF-8` нельзя задавать до установки/генерации `locales`;
- пользователя `debian` нельзя удалять в `runcmd`, пока Cloud-Init ещё выполняет `ssh-authkey-fingerprints`.

После исправлений новая сборка v2 с нуля завершилась сообщением `Template created successfully.`. Тестовый клон подтвердил SSH по ключу, locked password, sudo, QEMU Agent, timesync, growpart/filesystem growth, очистку builder artifacts и уникальные machine-id/SSH host keys.

### Изменение v3

В v3 Proxmox console переведена на настоящую текстовую serial-консоль:

```text
serial0: socket
vga: serial0
xterm.js → ttyS0 → autologin ops
```

`vga: serial0` выбран специально: при `vga: std` штатная кнопка Web Console в Proxmox открывает noVNC, даже если serial0 настроен и xterm.js доступен отдельным пунктом. Для обычных headless Debian VM нужен именно прямой вход в xterm.js.

Отдельный VGA/noVNC-канал в base template не сохраняется. Если конкретному гостю понадобится графический display, он настраивается отдельно.

2026-09-14 guest-level часть v3 успешно прошла чистую сборку. После окончательного возврата `vga: serial0` требуется финально проверить поведение штатной Web Console на тестовом клоне.

### Protection при клонировании

Base template `9000` защищён:

```text
protection=1
```

Обычные Full Clone по умолчанию должны быть без защиты:

```text
protection=0
```

Так как protection может оказаться в конфигурации клона после clone, AI/deploy-сценарий обязан явно выставлять `protection=0`, если паспорт конкретной VM не требует обратного. Защиту нельзя снимать с самого template ради удобства клонирования.
