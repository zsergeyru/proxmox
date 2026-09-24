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

Первая рабочая реализация OpenTofu управляет только VM `410 ai-control`.

Это намеренное ограничение первого запуска:

- `410` создаётся полным клонированием шаблона `9000`;
- ресурсы, диск, сеть и Cloud-Init берутся из итогового состояния;
- открытый ключ Ansible передаётся через Cloud-Init;
- QEMU Guest Agent включён;
- автоматическое уничтожение или замена `410` запрещены через `prevent_destroy`;
- автоматическая остановка ради изменения, требующего перезапуска, запрещена.

VM `109` также использует профиль `debian-vm`, но пока не включена в OpenTofu-ресурс. Это защищает существующую систему от случайного первого применения.

LXC пока также не управляются OpenTofu.

Первое применение `410` выполняется только через явное задание Semaphore:

~~~text
Deploy Guest 410
~~~

После успешного реального запуска 410 VM-ресурс можно обобщить для остальных управляемых VM отдельным изменением.
