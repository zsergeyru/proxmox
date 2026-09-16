# Ansible

`ansible/` предназначен для повторяемого provisioning и configuration management Linux VM/LXC после того, как guest уже создан и доступен по management SSH.

Граница ответственности описана в [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md): PVE/deployer создаёт guest и обеспечивает management readiness, а Ansible отвечает за повторяемую настройку ОС и приложений.

## Целевая структура

По мере появления реального содержимого используются:

```text
ansible/
├── inventory/    # hosts/groups и inventory variables
├── playbooks/    # entrypoint playbooks для provisioning/operations
└── roles/        # переиспользуемые роли
```

Пустые каталоги и README-заглушки только ради сохранения структуры в Git не нужны. Каталог создаётся тогда, когда в нём появляется реальный inventory, playbook или role.

## Основные правила

- Target Linux guests управляются по SSH отдельной Ansible/provisioning identity.
- Ansible identity не совпадает с host-side `pve_guest_ed25519` и AI Control identity.
- На target guest не требуется устанавливать полный Ansible controller; достаточно SSH и prerequisites используемых modules.
- Application/service configuration должна быть идемпотентной и повторяемой.
- Secrets, passwords, tokens и private keys не хранятся в Git.
- Runtime/persistent application data не следует превращать в содержимое Ansible repository; для него действует отдельный backup/recovery contract.
- Структура файлов внутри Linux guests следует [`../docs/34-linux-filesystem-layout.md`](../docs/34-linux-filesystem-layout.md).

## Связанные документы

- [`../docs/23-security.md`](../docs/23-security.md) — SSH identities и secret lifecycle.
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — deployer ↔ Ansible responsibility boundary.
- [`../docs/34-linux-filesystem-layout.md`](../docs/34-linux-filesystem-layout.md) — filesystem/data placement policy.
- [`../guests/README.md`](../guests/README.md) — guest desired state и per-guest files.
