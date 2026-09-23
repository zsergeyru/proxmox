# OpenTofu

Этот каталог содержит целевой контур управления обычными VM/LXC Proxmox.

## Граница ответственности

OpenTofu не читает `guest.yaml` напрямую и не реализует правила наследования проекта.

Поток данных:

```text
infrastructure/guests/defaults.yaml
+ infrastructure/guests/<guest>/guest.yaml
→ scripts/guests/resolver.py
→ scripts/guests/render-opentofu-input.py
→ /var/lib/infra-manager/opentofu/guests.json
→ OpenTofu
```

Таким образом, единственным владельцем правил defaults/profile/guest и адресации, включая режим DHCP, остаётся `scripts/guests/resolver.py`.

OpenTofu получает уже готовое значение `network.ipv4`: статический адрес с префиксом либо `dhcp`. Он не должен повторно вычислять адрес по VMID.

## Состояние

OpenTofu использует локальный backend:

```text
/var/lib/infra-manager/opentofu/state/proxmox.tfstate
```

Этот файл не хранится в Git и обязательно входит в резервное копирование `910`.

## Провайдер

Используется `bpg/proxmox` без SSH-доступа к PVE.

Провайдер получает:

- `pve_endpoint` — HTTPS API PVE;
- `pve_api_token` — ограниченный token `root@pam!infra-manager` с `privsep=1`;
- доверие к PVE CA — из системного CA bundle Runner.

`insecure = false` является обязательной частью контракта.

## Текущий этап

Пока здесь зафиксированы:

- версия OpenTofu;
- версия провайдера;
- backend state;
- входной JSON итогового состояния.

Ресурсы VM/LXC будут добавляться после успешного реального теста `infra-manager-pve-lifecycle-test --apply` на PVE.

До этого `tofu apply` для обычных гостей не должен использоваться.
