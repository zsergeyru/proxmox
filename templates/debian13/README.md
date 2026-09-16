# Базовый template Debian 13 для Proxmox

## Статус

Текущий baseline:

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

Template v7 является текущей действующей guest-contract версией. Она сохраняет модель v6 с единым management user `root` и добавляет стабильную диагностику этапов builder-а: внутри guest публикуются короткие ASCII stage-коды, а PVE Configuration отображает их по-русски уже на host-side.

Создание template является частью PVE Configuration. Отдельного standalone `scripts/pve/create-template.sh` нет.

Host-side stages:

```text
scripts/pve/setup/lib/60-template-contract.sh
scripts/pve/setup/lib/61-template-source.sh
scripts/pve/setup/lib/62-template-build.sh
scripts/pve/setup/lib/63-template-smoke.sh
```

Canonical Cloud-Init renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Guest-side versioned assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

Политика сборки: [`build-policy.md`](build-policy.md).

## Что делает Template v7

```text
Debian 13 trixie/latest generic cloud image
→ SHA-512 verification
→ VMID 9000 builder
→ apt update/full-upgrade
→ base packages
→ early QGA + ASCII build-stage telemetry
→ regular linux-image-amd64
→ remove cloud-amd64 kernel
→ root password locked
→ root SSH key-only
→ verification reboot
→ framebuffer + VGA/noVNC tty1 verification
→ QGA + SSH policy verification
→ fail-closed machine-specific cleanup
→ standard Proxmox Cloud-Init
→ ciuser=root
→ qm template
→ protection=1
```

## Template pipeline

```text
60-template-contract.sh
→ определить состояние VMID 9000
→ проверить полный hardware + Cloud-Init contract
→ отличить unfinished builder от foreign VM

61-template-source.sh
→ проверить capacity и cloud.debian.org
→ скачать image + SHA512SUMS
→ проверить SHA-512
→ вызвать production renderer
→ получить temporary Cloud-Init snippet

62-template-build.sh
→ создать builder VM
→ дождаться QGA/Cloud-Init/bootstrap
→ читать ASCII stage-code через QGA и отображать локальную русскую подпись
→ verification reboot
→ проверить kernel/framebuffer/consoles
→ выполнить guest cleanup
→ повторно подтвердить cleanup через QGA
→ вернуть стандартный Proxmox Cloud-Init
→ qm template
→ protection=1
→ финальный contract check

63-template-smoke.sh
→ Full Clone 9000 → 9099
→ увеличить scsi0 до 20G до первого boot
→ передать PVE guest SSH public key через Cloud-Init
→ проверить QGA/Cloud-Init/root SSH/kernel/machine-id/SSH host keys/disk grow
→ reboot + повторная QGA/SSH/identity проверка
→ при успехе shutdown + destroy 9099
→ при ошибке оставить 9099 для диагностики
```

Один `configure-pve.sh` владеет общей orchestration lock, state, log и error handling для host configuration, template build и smoke-test.

## Источник Debian

Pipeline использует:

```text
https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
```

Перед импортом скачивается `SHA512SUMS` из того же каталога и выполняется строгая SHA-512 проверка.

Download policy включает retries, connection timeout и low-speed timeout. Temporary download удаляется trap-ом, а stale temp-файлы предыдущего interrupted run очищаются под общей orchestration lock.

Отдельный фиксированный Debian build и APT snapshot не используются. При новой сборке template получает актуальное состояние Debian 13 на момент build.

## Kernel и console

```text
linux-image-amd64
cloud kernel удалён
vga: std
noVNC/VGA → tty1 → autologin root
serial0 → ttyS0 → autologin root (fallback)
Fixed 8x16
```

После reboot pipeline требует:

- kernel `*-amd64` без `cloud`;
- существующий framebuffer `fb0` с валидным размером;
- active QEMU Guest Agent;
- active `getty@tty1` и `serial-getty@ttyS0`;
- root autologin на обеих локальных консолях;
- корректную effective SSH policy.

Console autologin не является сетевой password authentication. Право Proxmox `VM.Console` фактически даёт root-доступ внутрь гостя и выдаётся только доверенным PVE identities.

## Диагностика этапов builder

Во время builder-run `/var/lib/template-build/bootstrap-status` содержит только короткий ASCII-код текущего этапа:

```text
apt-metadata
qga-install
apt-upgrade
base-packages
console
kernel
locale-time
services
security
metadata
done
```

После запуска QEMU Guest Agent PVE Configuration читает этот код через QGA и уже на host-side выводит русскую подпись в строках `[ЭТАП VM]` и `[ОЖИДАНИЕ]`. Кириллица через QGA telemetry не передаётся, поэтому исключается mojibake вида `Ð...`. До появления QGA host показывает только ожидание Guest Agent.

Stage-файл является временным builder state и удаляется перед seal вместе с `/var/lib/template-build`.

## Full Clone smoke-test

После новой сборки `9000` smoke-test запускается автоматически. Для уже существующего template он запускается явно:

```bash
configure-pve.sh --smoke-test-template
```

или через Public Bootstrap:

```bash
curl -fsSL https://raw.githubusercontent.com/zsergeyru/proxmox-bootstrap/main/bootstrap-pve.sh | bash -s -- --smoke-test-template
```

Smoke VM имеет фиксированные параметры:

```text
VMID: 9099
Name: smoke-debian13-9099
Clone: Full Clone
Disk: scsi0 → 20G до первого boot
Network: DHCP
ciuser: root
SSH key: /etc/proxmox-deployer/ssh/pve_guest_ed25519.pub
```

Проверяются:

```text
Full Clone config
Cloud-Init user-data и injected SSH public key
QEMU Guest Agent
Cloud-Init status=done
regular Debian kernel *-amd64 без cloud
root password locked
root SSH key-only policy
реальный root SSH по pve_guest_ed25519
валидный новый machine-id
созданные SSH host keys
root filesystem >=18 GiB после resize 16G→20G
reboot с новым boot_id
тот же machine-id и SSH host key после reboot
повторный QGA и строгий SSH host-key check
```

Состояние теста хранится в:

```text
/var/lib/proxmox-deployer/state/template-smoke.json
```

Новая сборка сначала записывает `pending`. Только полностью успешный smoke-test переводит state в `passed`. Это защищает от ситуации, когда host был перезапущен между seal template и smoke-test: следующий обычный PVE Configuration увидит `pending` и продолжит проверку.

Safety policy VMID 9099:

```text
9099 свободен
→ создать smoke VM

9099 содержит project smoke marker
→ STOP: предыдущая smoke VM оставлена для диагностики

9099 занят обычной VM или LXC
→ STOP без изменения/удаления

smoke SUCCESS
→ shutdown
→ qm destroy --purge
→ подтвердить, что 9099 снова свободен

smoke ERROR / interrupt
→ 9099 НЕ удалять
```

## Доступ

```text
management user: root
root password: locked
PasswordAuthentication: no
KbdInteractiveAuthentication: no
PermitRootLogin: prohibit-password
PubkeyAuthentication: yes
```

Base template не содержит personal, deployer, AI или Ansible public keys. Каждый Full Clone получает необходимые public keys отдельно до первого start.

## Базовые параметры и host-visible contract

```text
ostype: l26
CPU: host, sockets=1, cores=1
RAM: 1024 MiB
Controller: virtio-scsi-single
System disk: scsi0, local-lvm, >=16 GiB
discard=on
iothread=1
ssd=1
Cloud-Init drive: ide2, local-lvm, media=cdrom
Network: VirtIO / vmbr0
Boot: scsi0 first
QEMU Guest Agent: enabled
VGA: std
serial0: socket
Template network: DHCP
ciuser: root
ciupgrade: 0
onboot: 0/default
cicustom: absent after build
protection: 1 after pipeline
```

PVE Configuration проверяет этот contract и у существующего 9000, и после новой сборки. Размер system disk может быть больше 16 GiB, но не меньше.

Единственное автоматически исправляемое отклонение у в остальном совместимого template — отсутствие `protection=1`; защита восстанавливается после configuration snapshot. Остальные несовпадения вызывают STOP.

## Guest cleanup contract

Перед seal `template-finalize.sh` выполняет cleanup и завершает работу с ошибкой, если `debian` user не удалось удалить.

До `qm template` должны быть подтверждены:

```text
user debian отсутствует
/etc/machine-id пуст
/var/lib/dbus/machine-id отсутствует
SSH host keys отсутствуют
/root/.ssh отсутствует
/var/lib/template-build отсутствует
template-bootstrap отсутствует
template-finalize отсутствует
```

Эти assertions выполняются и внутри guest finalize, и повторно host-side через QGA. Это делает root-only/machine-clean contract fail-closed. Smoke-test затем подтверждает обратную сторону lifecycle: в Full Clone появляются новый непустой machine-id и новые SSH host keys.

## Cloud-Init renderer

Production и CI используют один файл:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Renderer:

- валидирует base YAML;
- требует ровно по одному `write_files` entry для bootstrap/finalize;
- подставляет Template-Version, image name и SHA-512;
- вставляет guest scripts;
- запрещает unresolved markers;
- атомарно записывает итоговый Cloud-Init.

CI дополнительно запускает `cloud-init schema` на результате production renderer и подтверждает фиксированный набор ASCII stage-codes.

## Clone lifecycle

```text
Full Clone from 9000
→ CPU/RAM/disk/network
→ ciuser=root
→ установить SSH public keys
→ qm cloudinit update
→ start
→ verify QGA/SSH/health
```

Для host-side deployer используется `pve_guest_ed25519.pub`. AI и Ansible используют собственные независимые public keys. Linked Clone не является штатным вариантом.

## Failure policy

```text
9000 отсутствует
→ BUILD

валидный template v7
→ SKIP

валидный template без protection
→ восстановить protection после snapshot

unfinished builder
→ STOP, оставить VM/disks для диагностики

foreign VM/LXC или incompatible template
→ STOP без destructive overwrite

smoke pending
→ выполнить/возобновить smoke-test

предыдущая project smoke VM 9099 существует
→ STOP, оставить для диагностики
```

Если VMID свободен, но остался только temporary builder snippet, он считается orphaned artefact предыдущей попытки и безопасно пересоздаётся.

## `/etc/vm-template-info`

Template v7 записывает, в частности:

```text
Шаблон: tpl-debian13
Версия-шаблона: 7
ОС: Debian 13
Тип-ядра: amd64
Management-user: root
SSH: root key-only
Исходный-образ
SHA512-исходного-образа
Дата-сборки
```

## Проверка после изменения template assets или pipeline

CI проверяет:

```text
bash syntax
ShellCheck
production renderer
YAML parse
cloud-init schema
ASCII builder stage-code contract
template contract unit tests
smoke safety/state unit tests
absence of legacy create-template.sh
```

Реальный PVE smoke-test теперь является штатным этапом PVE Configuration. Для новой сборки он обязателен автоматически; для уже существующего `9000` используется `--smoke-test-template`.

## История

- **v2** — базовый build/Full Clone, QGA, SSH, timesync, TRIM, cleanup и disk growth.
- **v3** — тестировалась serial-only Web Console; признана менее удобной.
- **v4** — `ops + NOPASSWD sudo`, VGA/noVNC и regular amd64 kernel; прежний baseline.
- **v5** — эксперимент с pinned Debian build и APT snapshot; отменён и номер не переиспользуется.
- **v6** — единый management user `root`, пароль root locked, root SSH только по ключу, разные identities различаются ключами.
- **v7** — ASCII telemetry builder stage через QGA и host-side русские подписи без mojibake.
