# Templates

Шаблоны VM/LXC и повторяемые заготовки. Template не должен содержать реальные secrets, tokens, passwords, private keys или credentials конкретного consumer.

## Debian 13 / VMID 9000

- [`debian13/README.md`](./debian13/README.md) — короткий паспорт `tpl-debian13`, текущий baseline и карта документации.
- [`debian13/build-policy.md`](./debian13/build-policy.md) — **канонический подробный contract** сборки, hardware/Cloud-Init, kernel/console, cleanup, Full Clone smoke-test и failure policy.
- [`debian13/cloud-init.yaml`](./debian13/cloud-init.yaml) — base Cloud-Init source.
- [`debian13/template-bootstrap.sh`](./debian13/template-bootstrap.sh) — guest-side build/bootstrap.
- [`debian13/template-finalize.sh`](./debian13/template-finalize.sh) — guest-side cleanup перед seal.

Общие правила, которые не являются свойствами самого template, вынесены в проектную документацию:

- [`../docs/23-security.md`](../docs/23-security.md) — management SSH, PVE/AI/Ansible identities и private-key lifecycle;
- [`../docs/33-guest-bootstrap-and-provisioning.md`](../docs/33-guest-bootstrap-and-provisioning.md) — initial access VM/LXC и handoff к provisioning;
- [`../docs/34-linux-filesystem-layout.md`](../docs/34-linux-filesystem-layout.md) — размещение application/config/persistent data/logs/cache/runtime внутри Linux guests.

Главное правило: каталог `templates/debian13/` содержит только то, что непосредственно описывает или строит template `9000`.
