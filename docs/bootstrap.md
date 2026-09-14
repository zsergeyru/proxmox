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

Скрипты не дублируются между репозиториями. Это исключает ситуацию, когда существуют две расходящиеся версии одного builder-скрипта.

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

Он создаёт Debian 13 template `tpl-debian13`, VMID `9000`, `Template-Version: 2`.

**2026-09-14: Template-Version 2 успешно проверена полной чистой сборкой на реальном PVE-хосте.** Сборка прошла весь цикл: загрузка и SHA-512-проверка Debian genericcloud image, импорт и resize диска, временный Cloud-Init, bootstrap, QEMU Guest Agent, успешное завершение Cloud-Init final stage, финальная очистка, shutdown, регенерация штатного Cloud-Init, `qm template` и `protection=1`.

Во время первого прогона v2 были выявлены и исправлены две ошибки порядка выполнения Cloud-Init:

- `locale: en_US.UTF-8` нельзя задавать до установки/генерации `locales`; locale теперь настраивается внутри `template-bootstrap` после установки пакета;
- пользователя `debian` нельзя удалять в `runcmd`, пока Cloud-Init ещё выполняет `ssh-authkey-fingerprints`; его удаление перенесено в `template-finalize`, после успешного завершения Cloud-Init.

После исправлений выполнена новая сборка с нуля; она завершилась сообщением `Template created successfully.`. Это считается базовой проверенной реализацией версии 2.
