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

Публичный `proxmox-bootstrap` — только канал распространения безопасных bootstrap-скриптов. Он не заменяет основной инфраструктурный репозиторий и не должен содержать приватные конфигурации.

## Почему отдельный репозиторий

GitHub не позволяет сделать отдельный файл публичным внутри приватного репозитория. Поэтому для файлов, которые PVE должен скачивать напрямую через HTTPS без авторизации, используется отдельный public repository.

На Proxmox-хосте при этом не требуется:

- устанавливать Git;
- клонировать весь инфраструктурный репозиторий;
- хранить GitHub PAT;
- давать хосту постоянный доступ к приватному GitHub-репозиторию.

## Debian 13 template builder

Приватная рабочая копия:

```text
templates/debian13/create-template.sh
```

Публичная копия:

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

## Правило синхронизации

`templates/debian13/create-template.sh` в приватном `proxmox` является рабочей инфраструктурной копией. Публичный `proxmox-bootstrap/create-template.sh` должен содержать ту же утверждённую версию executable-кода.

При изменении template builder необходимо:

1. изменить и проверить скрипт в `zsergeyru/proxmox`;
2. обновить публичную копию в `zsergeyru/proxmox-bootstrap`;
3. убедиться, что публичный файл не содержит секретов;
4. только после этого использовать raw URL для сборки template на PVE.

Если копии расходятся, запуск с PVE следует считать заблокированным до синхронизации.

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

Публично опубликован:

```text
create-template.sh
```

Он создаёт Debian 13 template `tpl-debian13`, VMID `9000`, `Template-Version: 2`.
