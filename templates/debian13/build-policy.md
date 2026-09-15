# Политика сборки `tpl-debian13`

```text
Template-Version: 6
```

## 1. Общий принцип

Template должен быть простым и понятным. Для текущего проекта не используем отдельные lock-файлы версий Debian, pinned cloud build или `snapshot.debian.org`.

Сборка каждый раз берёт актуальный Debian 13 Trixie cloud image и актуальные stable packages на момент запуска.

Template v6 использует единый management user `root`. Отдельный generic `ops` не создаётся.

## 2. Source image

Используется:

```text
https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
```

Checksum:

```text
https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
```

Builder обязан проверить SHA-512 до `qm importdisk`.

Это защищает от повреждённой загрузки, но не пытается сделать две сборки в разные даты идентичными.

## 3. APT

Внутри builder VM используются обычные Debian repositories из cloud image:

```text
apt-get update
apt-get -y full-upgrade
apt-get install ...
```

APT snapshot и version pinning не применяются.

`ciupgrade=0`: Cloud-Init не делает автоматический package upgrade при первом boot клона.

## 4. Kernel и console

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

После bootstrap выполняется verification reboot. Builder продолжает только если:

- kernel `*-amd64` и не `*cloud*`;
- `/sys/class/graphics/fb0/virtual_size` существует и валиден;
- QGA active;
- tty1/ttyS0 getty active;
- обе console override действительно используют `--autologin root`.

## 5. Root / SSH

```text
root password: locked
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Таким образом пароль root не используется, но SSH root по public key разрешён.

Builder проверяет `sshd -t`, effective `sshd -T` для `root` и locked password state.

В base template не должно быть ни одного management public key. Перед seal удаляется `/root/.ssh`.

Разные субъекты доступа используют независимые keypairs:

```text
PVE / deploy-guest
→ pve_guest_ed25519

AI control
→ ai_control_ed25519

Ansible / 311
→ отдельная provisioning identity
```

Все они могут входить как `root`; разграничение и отзыв доступа выполняются по ключам.

## 6. Cloud-Init lifecycle

Builder-only custom Cloud-Init используется только для сборки.

Перед `qm template`:

```text
final cleanup
→ remove cicustom
→ ciuser=root
→ ipconfig0=ip=dhcp
→ ciupgrade=0
→ qm cloudinit update
→ проверить отсутствие builder-only user-data
```

Description шаблона должен содержать:

```text
template-version=6
```

Stage 1 использует этот marker вместе с `ciuser=root` для проверки совместимости существующего VMID 9000.

## 7. Full Clone policy

Рабочие Debian VM создаются как Full Clone.

До первого start задаются CPU/RAM/disk/network, `ciuser=root`, необходимые SSH public keys и Cloud-Init параметры.

Template `9000` остаётся `protection=1`.

Старый template другой версии не перезаписывается автоматически. Его замена должна быть отдельной осознанной операцией.

## 8. Base packages

```text
qemu-guest-agent openssh-server sudo locales cloud-guest-utils systemd-timesyncd
linux-image-amd64 console-setup console-setup-linux
git mc nano curl wget jq ca-certificates openssl
htop ncdu lsof tree tmux bash-completion
tar rsync zstd unzip acl
dnsutils iproute2 iputils-ping net-tools
cron logrotate
```

Наличие `sudo` как utility package не означает наличие отдельного обязательного admin user. Management automation работает как `root`.

Docker/Compose и service-specific software в base template не входят.

## 9. Disk / TRIM

```text
VirtIO SCSI Single
iothread=1
discard=on
ssd=1
base disk=16 GiB
```

Включён `fstrim.timer`; перед seal выполняется `fstrim -av`.

## 10. Cleanup

Удаляются machine-specific/runtime данные:

- Cloud-Init state/logs/seed;
- machine-id;
- SSH host keys;
- `/root/.ssh`;
- DHCP/network state;
- systemd random seed;
- APT lists/cache;
- journal/build logs;
- temp/history;
- builder scripts.

## 11. Provenance

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

## 12. Failure policy

Существующий VMID 9000 не перезаписывается.

При build error временная VM/disks не уничтожаются автоматически: состояние сохраняется для диагностики.

Stage 1 также не мигрирует старый template на v6 автоматически: несовместимая версия вызывает STOP до любых попыток использовать её как clone source.

## 13. Проверка

CI проверяет shell/YAML структуру. После существенного изменения builder требуется реальный clean build + Full Clone smoke test, включая вход `root` по injected SSH key.
