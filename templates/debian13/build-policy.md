# Политика сборки `tpl-debian13`

```text
Template-Version: 7
```

## 1. Общий принцип

Template должен быть простым и понятным. Не используются отдельные lock-файлы версий Debian, pinned cloud build или `snapshot.debian.org`.

Сборка берёт актуальный Debian 13 Trixie cloud image и stable packages на момент запуска.

Template v7 использует единый management user `root`. Отдельный generic `ops` не создаётся.

Host-side orchestration является частью `scripts/pve/setup/configure-pve.sh`; standalone `create-template.sh` отсутствует.

## 2. Pipeline ownership

```text
60-template-contract.sh
→ state detection + полный host-visible contract

61-template-source.sh
→ capacity/source + image/checksum + production Cloud-Init render

62-template-build.sh
→ VM builder lifecycle + QGA normalization + stage rendering + verification + cleanup + seal

63-template-smoke.sh
→ Full Clone smoke lifecycle + runtime verification + safe cleanup
```

Canonical renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Guest-side assets:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

`configure-pve.sh` остаётся единственным orchestrator и владеет общими lock/state/log/error semantics.

## 3. Source image

Используется:

```text
https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
```

Checksum:

```text
https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
```

Source stage обязан проверить SHA-512 до `qm importdisk`.

Download policy:

```text
retry transient errors
connect timeout
low-speed timeout
unique temporary file
cleanup temp on EXIT/INT/TERM
cleanup stale same-target temp under shared orchestration lock
```

## 4. APT внутри guest

```text
apt-get update
apt-get -y full-upgrade
apt-get install ...
```

APT snapshot и version pinning не применяются.

`ciupgrade=0`: Cloud-Init не делает automatic package upgrade при первом boot клона.

## 5. Kernel и console

Устанавливаются:

```text
linux-image-amd64
console-setup
console-setup-linux
```

`linux-image-*cloud-amd64` удаляется.

Console policy:

```text
vga: std
tty1 autologin root
serial0: socket
ttyS0 autologin root
Fixed 8x16
```

После bootstrap выполняется verification reboot. Pipeline продолжает только если:

- kernel `*-amd64` и не `*cloud*`;
- framebuffer существует и валиден;
- QGA active;
- tty1/ttyS0 getty active;
- обе console override используют `--autologin root`.

## 6. Root / SSH

```text
root password: locked
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Guest bootstrap проверяет `sshd -t`, effective `sshd -T` для `root` и locked password state.

В base template не должно быть management public keys. Перед seal `/root/.ssh` удаляется.

## 7. Production Cloud-Init renderer

Runtime и CI обязаны использовать один renderer:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Renderer должен:

- прочитать и проверить `cloud-init.yaml`;
- обнаружить ровно по одному bootstrap/finalize `write_files` entry;
- вставить guest scripts;
- подставить Template-Version, image name и SHA-512;
- запретить unresolved placeholders;
- атомарно записать итоговый документ.

CI дополнительно проверяет результат через `cloud-init schema`.

## 8. Build telemetry

`template-bootstrap` публикует текущий этап в:

```text
/var/lib/template-build/bootstrap-status
```

В этом файле разрешены только ASCII stage-codes:

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

Русские подписи этапов формируются host-side в `62-template-build.sh`. Кириллица не передаётся через QGA status channel, потому что вывод `guest-exec` проходит через несколько уровней byte/string serialization и не должен использоваться как транспорт localized telemetry.

До запуска QEMU Guest Agent host может показывать только ожидание QGA. После появления агента PVE Configuration читает `bootstrap-status` и выводит текущий этап в строках `[ЭТАП VM]`/`[ОЖИДАНИЕ]`.

## 9. Cloud-Init lifecycle

Builder-only custom Cloud-Init существует только во время build.

Перед `qm template`:

```text
guest cleanup
→ host-side cleanup assertions через QGA
→ shutdown
→ remove cicustom
→ ciuser=root
→ ipconfig0=ip=dhcp
→ ciupgrade=0
→ qm cloudinit update
→ проверить отсутствие builder-only user-data
→ удалить temporary snippet
```

Description содержит:

```text
template-version=7
```

## 10. Host-visible template contract

Существующий и только что созданный 9000 должны соответствовать:

```text
name = tpl-debian13
template = 1
ostype = l26
cpu = host
sockets = 1
cores = 1
memory = 1024
scsihw = virtio-scsi-single
scsi0 storage = local-lvm
scsi0 discard=on
iothread=1
ssd=1
scsi0 size >= 16G
ide2 = local-lvm Cloud-Init CD-ROM
net0 = VirtIO, bridge=vmbr0
boot order starts with scsi0
onboot = 0/default
agent = 1
vga = std
serial0 = socket
ciuser = root
ciupgrade = 0
ipconfig0 = ip=dhcp
cicustom absent
description contains template-version=7
protection = 1 after pipeline
```

Отсутствующий `protection=1` у в остальном совместимого template можно восстановить после configuration snapshot. Другой mismatch вызывает STOP.

## 11. Full Clone policy

Рабочие Debian VM создаются как Full Clone.

До первого start задаются CPU/RAM/disk/network, `ciuser=root`, необходимые SSH public keys и Cloud-Init параметры.

Template `9000` остаётся `protection=1`.

Старый/incompatible template автоматически не заменяется.

## 12. Full Clone smoke-test

Штатный smoke VM:

```text
VMID = 9099
name = smoke-debian13-9099
source = template 9000
clone = --full 1
storage = local-lvm
scsi0 = 20G до первого boot
ciuser = root
ipconfig0 = ip=dhcp
SSH public key = pve_guest_ed25519.pub
```

Trigger policy:

```text
новый template создан текущим run
→ smoke-test обязателен автоматически

существующий template
→ smoke-test только по --smoke-test-template

/template-smoke.json status=pending
→ smoke-test обязателен при следующем обычном run
```

Перед `qm template`/seal нового builder-а PVE Configuration записывает pending smoke state. Поэтому power loss между seal и smoke не позволяет следующему run молча считать pipeline полностью проверенным.

Runtime checks smoke VM:

```text
Full Clone действительно обычная VM, не template
project safety marker в description
Cloud-Init содержит PVE guest SSH public key
QGA active
Cloud-Init done
kernel *-amd64 и не cloud
root password locked
root SSH policy key-only
реальный root SSH по PVE guest key
валидный непустой /etc/machine-id
новые SSH host keys
root filesystem >=18 GiB после resize до 20G
reboot меняет boot_id
machine-id и SSH host key сохраняются после reboot
QGA и SSH повторно доступны
StrictHostKeyChecking=yes проходит с host key, принятым до reboot
```

Success cleanup:

```text
shutdown
→ дождаться stopped
→ qm destroy 9099 --purge 1
→ убедиться, что VMID свободен
→ template-smoke.json status=passed
```

Failure policy:

```text
ошибка или interruption после создания 9099
→ НЕ удалять 9099
→ вывести diagnostic hint

9099 с project smoke marker уже существует
→ STOP как residue предыдущего неуспешного smoke

9099 занят обычной VM или LXC
→ STOP без изменения/удаления
```

## 13. Base packages

```text
qemu-guest-agent openssh-server sudo locales cloud-guest-utils systemd-timesyncd
linux-image-amd64 console-setup console-setup-linux
git mc nano curl wget jq ca-certificates openssl
htop ncdu lsof tree tmux bash-completion
tar rsync zstd unzip acl
dnsutils iproute2 iputils-ping net-tools
cron logrotate
```

Docker/Compose и service-specific software в base template не входят.

## 14. Disk / TRIM

```text
VirtIO SCSI Single
iothread=1
discard=on
ssd=1
base disk >=16 GiB
```

Включён `fstrim.timer`; перед seal выполняется `fstrim -av`.

Smoke-test отдельно расширяет Full Clone до `20G` до первого boot и требует root filesystem не меньше `18 GiB`, тем самым проверяя штатный growpart/resize filesystem lifecycle.

## 15. Fail-closed cleanup

`template-finalize.sh` обязан завершиться ошибкой, если generic `debian` user существует и его не удалось удалить.

После cleanup обязательны assertions:

```text
debian absent
/etc/machine-id empty
/var/lib/dbus/machine-id absent
SSH host keys absent
/root/.ssh absent
/var/lib/template-build absent
template-bootstrap absent
template-finalize absent
```

Assertions выполняются внутри guest и повторно host-side через QGA до seal. Smoke-test затем подтверждает, что Full Clone генерирует новый machine-id и SSH host keys.

## 16. Provenance

`/etc/vm-template-info` содержит:

```text
Template-Version
Management-user
SSH policy
Source-Image
Source-Image-SHA512
Build-Date
Kernel-Flavor
Console modes
```

Pinned cloud build ID и APT snapshot не являются частью policy.

Smoke provenance хранится отдельно host-side:

```text
/var/lib/proxmox-deployer/state/template-smoke.json
```

В `passed` state фиксируются template VMID/version, source revision, machine-id smoke clone, kernel, root filesystem bytes и SSH host-key fingerprint.

## 17. Failure policy

Существующий VMID 9000 не перезаписывается.

При build error VM/disks не уничтожаются автоматически. Следующий run распознаёт `builder-debian13` и останавливается для диагностики.

Если VMID свободен, но остался builder snippet без builder VM, snippet считается orphaned build artefact и пересоздаётся.

PVE Configuration не мигрирует incompatible template на v7 автоматически.

Smoke VM удаляется только после полного SUCCESS. Любое неоднозначное состояние VMID 9099 приводит к STOP без destructive cleanup.

## 18. Проверка

CI проверяет:

- repository validator;
- Python compilation;
- `bash -n`;
- ShellCheck;
- production renderer;
- YAML parsing;
- `cloud-init schema`;
- отсутствие embedded `guest-status` helper;
- exact ASCII stage-code set;
- template contract unit tests;
- template smoke safety/state unit tests;
- malformed guest directory negative test;
- отсутствие legacy `scripts/pve/create-template.sh`;
- whitespace.

CI не подменяет real PVE runtime. Реальный Full Clone smoke-test является частью PVE Configuration: автоматически после нового build либо явно через `--smoke-test-template` для уже существующего template.
