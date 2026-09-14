# Политика сборки `tpl-debian13`

```text
Template-Version: 5
```

## 1. Роль PVE host

PVE остаётся hypervisor/deployer, а не application/build workstation.

Разрешён минимальный runtime, нужный host-side deployment, включая:

```text
git
openssh-client
python3/python3-yaml
curl/jq/CA tools
```

`git` нужен для read-only private source-of-truth checkout и является принятым исключением. На PVE не устанавливаются Docker, Hermes, application services и произвольные guest build toolchains.

Template строится штатными `qm/pvesm` и временной builder-VM.

## 2. Воспроизводимый source image

Не использовать:

```text
trixie/latest
```

Текущий baseline:

```text
Debian cloud build: 20260601-2496
image: debian-13-genericcloud-amd64-20260601-2496.qcow2
```

Builder получает `SHA512SUMS` из того же versioned cloud build и требует strict SHA-512 verification до `qm importdisk`.

Использованные filename/hash/build id записываются в `/etc/vm-template-info`.

## 3. Воспроизводимый APT build

Live Debian repositories нельзя использовать для `apt full-upgrade` во время reproducible build, иначе одинаковый script в разные дни создаёт разные template.

Текущий build snapshot:

```text
20260914T000000Z
```

Builder временно использует timestamped `snapshot.debian.org` для Debian и Debian Security, выполняет update/full-upgrade/package install и только после завершения build возвращает обычные live repositories.

Следствие:

```text
snapshot → фиксирует состав template build
live repos → используются клонами для дальнейших контролируемых обновлений
```

`ciupgrade=0`: Cloud-Init не выполняет автоматический package upgrade при первом boot клона.

## 4. Kernel и VGA console

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
tty1 autologin ops
serial0: socket
ttyS0 autologin ops
Fixed 8x16
```

После bootstrap обязателен verification reboot. Builder продолжает только если:

- kernel `*-amd64` и не `*cloud*`;
- `/sys/class/graphics/fb0/virtual_size` существует и валиден;
- QGA active;
- tty1/ttyS0 getty active.

## 5. Users / SSH / sudo

```text
ops password: locked
root password: locked
ops sudo: NOPASSWD
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Builder проверяет `sshd -t` и effective `sshd -T`.

Персональных SSH keys в base template нет.

## 6. Cloud-Init lifecycle

Builder-only custom Cloud-Init используется только для сборки.

Перед `qm template`:

```text
final cleanup
→ remove cicustom
→ ciuser=ops
→ ipconfig0=ip=dhcp
→ ciupgrade=0
→ qm cloudinit update
→ проверить отсутствие builder-only user-data
```

## 7. Full Clone policy

Рабочие Debian VM создаются как Full Clone.

До первого start задаются:

```text
protection
name/hostname
CPU/RAM/disk
ciuser=ops
SSH public key
network/DNS
qm cloudinit update
```

Template `9000` остаётся `protection=1`. Protection клона задаётся по `guest.yaml`.

## 8. Base packages

Базовый набор включает инструменты администрирования/диагностики и QGA, включая:

```text
qemu-guest-agent openssh-server sudo locales cloud-guest-utils systemd-timesyncd
linux-image-amd64 console-setup console-setup-linux
git mc nano curl wget jq ca-certificates openssl
htop ncdu lsof tree tmux bash-completion
tar rsync zstd unzip acl
dnsutils iproute2 iputils-ping net-tools
cron logrotate
```

Docker/Compose и service-specific software не входят в VM template.

## 9. Disk / TRIM

```text
VirtIO SCSI Single
iothread=1
discard=on
ssd=1
base disk=16 GiB
```

`cloud-guest-utils` обеспечивает growpart; включён `fstrim.timer`; перед seal выполняется `fstrim -av`.

## 10. Cleanup

Удаляются machine-specific/runtime данные:

- Cloud-Init state/logs/seed;
- machine-id;
- SSH host keys;
- DHCP/network state;
- systemd random seed;
- APT lists/cache;
- journal/build logs;
- temp/history;
- `/home/ops/.ssh`;
- builder scripts.

Сохраняются SSH hardening, sudo, console, timesync и fstrim policy.

## 11. Provenance

`/etc/vm-template-info` содержит как минимум:

```text
Template-Version
Source-Image
Source-Image-SHA512
Debian-Cloud-Build
Build-Apt-Snapshot
Build-Date
Kernel-Flavor
Console modes
```

## 12. Failure policy

Существующий VMID 9000 не перезаписывается.

При build error временная VM/disks не уничтожаются автоматически: состояние сохраняется для диагностики.

## 13. Проверка

CI проверяет shell/YAML/pin policy, но acceptance новой Template-Version требует реального clean build + Full Clone smoke test.

Подробная reproducibility policy: [`../../docs/24-reproducible-bootstrap.md`](../../docs/24-reproducible-bootstrap.md).
