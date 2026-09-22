# OpenTofu

Этот каталог содержит целевой контур управления обычными VM/LXC Proxmox.

## Граница ответственности

OpenTofu не читает `guest.yaml` напрямую и не реализует правила наследования проекта.

Поток данных:

```text
guests/defaults.yaml
+ guests/<guest>/guest.yaml
→ scripts/guest_config.py
→ scripts/infra-deployer/render-opentofu-input.py
→ /var/lib/infra-deployer/opentofu/guests.json
→ OpenTofu
```

Таким образом, единственным владельцем правил defaults/profile/guest и вычисляемой адресации остаётся `scripts/guest_config.py`.

## Состояние

OpenTofu использует локальный backend:

```text
/var/lib/infra-deployer/opentofu/state/proxmox.tfstate
```

Этот файл не хранится в Git и обязательно входит в резервное копирование `910`.

## Провайдер

Используется `bpg/proxmox` без SSH-доступа к PVE.

Провайдер получает:

- `pve_endpoint` — HTTPS API PVE;
- `pve_api_token` — отдельный токен `infra-deployer@pve!automation`;
- доверие к PVE CA — из системного CA bundle Runner.

`insecure = false` является обязательной частью контракта.

## Текущий этап

Пока здесь зафиксированы:

- версия OpenTofu;
- версия провайдера;
- backend state;
- входной JSON итогового состояния.

Ресурсы VM/LXC будут добавляться после успешного реального теста `infra-deployer-pve-lifecycle-test --apply` на PVE.

До этого `tofu apply` для обычных гостей не должен использоваться.
