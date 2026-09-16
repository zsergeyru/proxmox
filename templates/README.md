# Templates

`templates/` содержит reusable VM/LXC templates и guest-side build assets. Здесь должны находиться только файлы, которые непосредственно описывают или строят шаблон.

Общие policy для SSH identities, Linux filesystem layout, provisioning и access control находятся в `docs/`, а не дублируются внутри template directories.

## Debian 13 / VMID 9000

Каталог [`debian13/`](debian13/) содержит текущий универсальный Debian VM template.

Основные файлы:

- [`debian13/README.md`](debian13/README.md) — короткий паспорт и точка входа;
- [`debian13/build-policy.md`](debian13/build-policy.md) — **канонический технический contract** template `9000`;
- [`debian13/cloud-init.yaml`](debian13/cloud-init.yaml) — guest Cloud-Init source;
- [`debian13/template-bootstrap.sh`](debian13/template-bootstrap.sh) — guest-side build/bootstrap;
- [`debian13/template-finalize.sh`](debian13/template-finalize.sh) — cleanup/seal preparation.

Текущая Template-Version и точные hardware/Cloud-Init/kernel/console/smoke параметры читаются из `debian13/README.md` и `debian13/build-policy.md`; этот индекс их не дублирует.

## Связанные общие policy

- [`../docs/23-security.md`](../docs/23-security.md) — management SSH, независимые identities и private-key lifecycle;
- [`../docs/25-pve-access-control.md`](../docs/25-pve-access-control.md) — PVE roles/ACL и clone access;
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — initial access VM/LXC и provisioning handoff;
- [`../docs/34-linux-filesystem-layout.md`](../docs/34-linux-filesystem-layout.md) — application/config/data/log/cache/runtime layout внутри Linux guests.

## Правила

- Template не содержит passwords, tokens, private keys или credentials конкретного consumer.
- Template остаётся универсальным и не создаёт заранее service-specific каталоги или workloads.
- Build assets и contract меняются согласованно с tests и versioning policy.
- Если правило относится ко всем Linux guests, оно должно жить в `docs/`, а не в каталоге конкретного template.
