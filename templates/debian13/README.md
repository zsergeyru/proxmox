# Debian 13 template `9000`

**Type:** Template passport / Entry point  
**Status:** Active  
**Source of truth:** No — полный технический contract находится в [`build-policy.md`](build-policy.md).

## Текущий baseline

```text
VMID: 9000
Name: tpl-debian13
OS: Debian 13 (Trixie)
Template-Version: 7
Clone policy: Full Clone
Protection: 1
Management user: root
SSH: public key only
```

Template `9000` — универсальная база для управляемых Debian VM. Он не содержит application-specific software, service directories, personal keys или других credentials конкретного потребителя.

## Что гарантирует template

Коротко:

```text
Debian 13 generic cloud image
→ SHA-512 verification
→ current Debian packages на момент build
→ regular linux-image-amd64
→ QEMU Guest Agent
→ Cloud-Init
→ root password locked
→ root SSH key-only
→ VGA/noVNC + serial console
→ machine-specific cleanup
→ Full Clone ready
→ protection=1
```

После clone конкретный guest получает CPU/RAM/disk/network и необходимые **public** SSH keys до первого start.

Точные параметры hardware, Cloud-Init, kernel/console, cleanup, smoke-test, failure policy и provenance определены только в [`build-policy.md`](build-policy.md).

## Файлы template

```text
templates/debian13/
├── README.md                  # этот паспорт
├── build-policy.md            # канонический технический contract
├── cloud-init.yaml            # base guest Cloud-Init source
├── template-bootstrap.sh      # guest-side build/bootstrap
└── template-finalize.sh       # cleanup и seal preparation
```

Host-side pipeline:

```text
scripts/pve/setup/lib/60-template-contract.sh
scripts/pve/setup/lib/61-template-source.sh
scripts/pve/setup/lib/62-template-build.sh
scripts/pve/setup/lib/63-template-smoke.sh
```

Canonical renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Создание template является частью PVE Configuration; отдельного standalone `create-template.sh` нет.

## Build и smoke-test

Новая сборка `9000` выполняется штатным PVE Configuration pipeline. После новой сборки Full Clone smoke-test обязателен автоматически.

Для явной проверки уже существующего template:

```bash
configure-pve.sh --smoke-test-template
```

или через Public Bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

Smoke VM использует VMID `9099`. При успешной проверке она удаляется; при ошибке остаётся для диагностики. Подробная safety/state policy находится в [`build-policy.md`](build-policy.md).

## Credentials

Template не содержит management credentials:

```text
root password locked
/root/.ssh absent before seal
management authorized_keys empty
private keys absent
SSH host keys removed before seal
```

Каждый clone получает свой набор public keys отдельно. Общая модель PVE/AI/Ansible/personal SSH identities описана в [`../../docs/23-security.md`](../../docs/23-security.md), а initial injection VM/LXC — в [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md).

## Filesystem/application layout

Template не создаёт заранее каталоги будущих сервисов и не задаёт структуру конкретных workloads.

Общая policy размещения `/opt`, `/etc`, `/var/lib`, `/srv`, logs/cache/runtime находится в [`../../docs/34-linux-filesystem-layout.md`](../../docs/34-linux-filesystem-layout.md).

## Где искать детали

| Вопрос | Канонический документ |
|---|---|
| Как строится и проверяется `9000` | [`build-policy.md`](build-policy.md) |
| SSH identities и secrets | [`../../docs/23-security.md`](../../docs/23-security.md) |
| Initial SSH VM/LXC и provisioning handoff | [`../../docs/33-guest-bootstrap-and-provisioning.md`](../../docs/33-guest-bootstrap-and-provisioning.md) |
| Файловая структура сервисов | [`../../docs/34-linux-filesystem-layout.md`](../../docs/34-linux-filesystem-layout.md) |
| PVE roles/ACL для template clone | [`../../docs/25-pve-access-control.md`](../../docs/25-pve-access-control.md) |

Главный принцип: **`README.md` отвечает на вопрос «что такое template 9000 и куда смотреть дальше», а `build-policy.md` является единственным подробным contract его сборки и runtime-проверки.**
