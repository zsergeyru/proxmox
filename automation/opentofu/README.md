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

`main.tf` содержит один универсальный ресурс VM.

Он строит набор VM из полного итогового состояния:

~~~text
guests.json
↓
гости с type = vm
↓
proxmox_virtual_environment_vm.guest[VMID]
~~~

Для каждой VM из данных используются её собственные:

- VMID и имя;
- PVE-узел;
- шаблон;
- CPU и память;
- диск и хранилище;
- сеть;
- Cloud-Init;
- защита и автоматический запуск.

Открытый ключ Ansible передаётся через Cloud-Init. QEMU Guest Agent включён. Автоматическое уничтожение или замена управляемой VM запрещены через `prevent_destroy`.

OpenTofu управляет как VM, так и непривилегированными LXC. Высокоуровневая возможность профиля `container-host` преобразуется для LXC в необходимые возможности вложенной контейнерной среды.

### Разделение основного и bootstrap state

Полное итоговое состояние может содержать VMID 910, однако обычная рабочая область infra-manager исключает управляющий VMID 910 из основного state.

Временный 990 использует обратный фильтр:

~~~text
include VMID 910
→ отдельный bootstrap-state
~~~

Так один и тот же OpenTofu-код создаёт все типы гостей, но объект 910 никогда не попадает в state, которым управляет сам 910.

