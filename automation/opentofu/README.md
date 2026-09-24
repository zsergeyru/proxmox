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

LXC пока не управляются OpenTofu.

### Первый запуск 410

В полном входном состоянии сейчас присутствуют как минимум VM `109` и `410`.

`109` уже существует вне OpenTofu state и не должен автоматически приниматься под управление. Поэтому до отдельного явного импорта 109 обычный полный `tofu apply` запрещён.

Для первого запуска 410 используется специальное задание Semaphore:

~~~text
Deploy Guest 410
~~~

Оно не урезает `guests.json`, а строит план только для адреса:

~~~text
proxmox_virtual_environment_vm.guest["410"]
~~~

После создания плана задание дополнительно проверяет, что в нём нет изменений других ресурсов.

Так сохраняется единый OpenTofu state и одновременно исключается случайное создание или изменение 109 во время первого запуска 410.
