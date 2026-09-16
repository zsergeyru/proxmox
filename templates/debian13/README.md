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

Template v7 является текущей действующей guest-contract версией. Она сохраняет модель v6 с единым management user `root` и добавляет постоянную диагностическую команду `guest-status`, а внутренние этапы builder-а передаются через QGA как ASCII-коды и отображаются PVE Configuration по-русски уже на host-side.

Создание template является частью PVE Configuration. Отдельного standalone `scripts/pve/create-template.sh` нет.

Host-side stages:

```text
scripts/pve/setup/lib/60-template-contract.sh
scripts/pve/setup/lib/61-template-source.sh
scripts/pve/setup/lib/62-template-build.sh
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
→ persistent guest-status diagnostics
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
→ проверить kernel/framebuffer/consoles/guest-status
→ выполнить guest cleanup
→ повторно подтвердить cleanup через QGA
→ сохранить guest-status как часть итогового guest contract
→ вернуть стандартный Proxmox Cloud-Init
→ qm template
→ protection=1
→ финальный contract check
```

Один `configure-pve.sh` владеет общей orchestration lock, state, log и error handling для host configuration и template build.

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
- executable `/usr/local/sbin/guest-status`;
- корректную effective SSH policy.

Console autologin не является сетевой password authentication. Право Proxmox `VM.Console` фактически даёт root-доступ внутрь гостя и выдаётся только доверенным PVE identities.

## Диагностика внутри VM

Команда:

```bash
guest-status
```

показывает без дополнительных зависимостей:

```text
Build stage code
Build stage
QGA package
QGA service
QGA enabled
QGA virtio port
Cloud-Init
IP addresses
Uptime
```

Её вывод намеренно ASCII-only: утилита должна оставаться читаемой в ранней консоли ещё до полной настройки локалей и console font. Во время builder-run stage-файл `/var/lib/template-build/bootstrap-status` также содержит только ASCII-код. Это исключает mojibake при передаче stage через `qm guest exec`/QGA. После seal builder state удаляется, но `guest-status` остаётся полезной для обычных Full Clone VM.

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
guest-status существует и executable
```

Эти assertions выполняются и внутри guest finalize, и повторно host-side через QGA. `guest-status` не является builder-only artifact и сохраняется в итоговом template v7.

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

CI дополнительно запускает `cloud-init schema` на результате production renderer, извлекает встроенный `guest-status`, проверяет его `bash -n`/ShellCheck и подтверждает фиксированный набор ASCII stage-codes.

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
embedded guest-status syntax/ShellCheck
ASCII builder stage-code contract
template contract unit tests
absence of legacy create-template.sh
```

После существенного изменения guest/template assets по принятой policy требуется реальный integration test:

1. clean build VMID 9000;
2. Full Clone;
3. noVNC/tty1 и serial fallback;
4. regular kernel + framebuffer;
5. QGA;
6. `guest-status` в builder и Full Clone;
7. locked root password / SSH key-only;
8. root SSH с injected public key;
9. unique machine-id и SSH host keys;
10. filesystem growth после resize;
11. отсутствие builder artifacts и baked-in authorized_keys.

## История

- **v2** — базовый build/Full Clone, QGA, SSH, timesync, TRIM, cleanup и disk growth.
- **v3** — тестировалась serial-only Web Console; признана менее удобной.
- **v4** — `ops + NOPASSWD sudo`, VGA/noVNC и regular amd64 kernel; прежний baseline.
- **v5** — эксперимент с pinned Debian build и APT snapshot; отменён и номер не переиспользуется.
- **v6** — единый management user `root`, пароль root locked, root SSH только по ключу, разные identities различаются ключами.
- **v7** — постоянная `guest-status`, ASCII telemetry builder stage через QGA и host-side русские подписи без mojibake.
