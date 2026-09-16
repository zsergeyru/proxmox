# Спецификация сборки `tpl-debian13`

**Тип:** Спецификация  
**Статус:** Действующий  
**Основной источник:** Да — для полного контракта сборки и проверки шаблона `9000`, его параметров, видимых со стороны PVE, очистки и проверки реального Full Clone.

```text
Template-Version: 7
```

## 1. Общий принцип

Шаблон должен быть простым и понятным. Не используются отдельные файлы фиксации версий Debian, жёстко закреплённая версия облачного образа или `snapshot.debian.org`.

Сборка берёт актуальный облачный образ Debian 13 Trixie и стабильные пакеты на момент запуска.

Шаблон v7 использует единого административного пользователя `root`. Отдельный универсальный `ops` не создаётся.

Управление сборкой со стороны PVE является частью `scripts/pve/setup/configure-pve.sh`; отдельного `create-template.sh` нет.

## 2. Ответственность этапов сборки

```text
60-template-contract.sh
→ определение состояния + полный контракт, видимый со стороны PVE

61-template-source.sh
→ проверка места и источника + образ/контрольная сумма + генерация Cloud-Init

62-template-build.sh
→ жизненный цикл VM-сборщика + работа с QGA + отображение этапов + проверки + очистка + финализация

63-template-smoke.sh
→ проверка жизненного цикла Full Clone + проверка работающей VM + безопасная очистка
```

Основной генератор:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Файлы, выполняемые внутри гостевой системы:

```text
templates/debian13/cloud-init.yaml
templates/debian13/template-bootstrap.sh
templates/debian13/template-finalize.sh
```

`configure-pve.sh` остаётся единственным управляющим скриптом и отвечает за общую блокировку, файлы состояния, журналы и обработку ошибок.

## 3. Исходный образ

Используется:

```text
https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2
```

Контрольные суммы:

```text
https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS
```

Этап получения источника обязан проверить SHA-512 до `qm importdisk`.

Правила загрузки:

```text
повторять попытку при временных ошибках
ограничивать время установления соединения
останавливать слишком медленную загрузку
использовать уникальный временный файл
удалять временный файл при EXIT/INT/TERM
удалять старый временный файл того же назначения только под общей блокировкой
```

## 4. APT внутри гостевой системы

```text
apt-get update
apt-get -y full-upgrade
apt-get install ...
```

Снимок APT-репозитория и жёсткая фиксация версий пакетов не применяются.

`ciupgrade=0`: Cloud-Init не выполняет автоматическое обновление пакетов при первом запуске клона.

## 5. Ядро и консоль

Устанавливаются:

```text
linux-image-amd64
console-setup
console-setup-linux
```

`linux-image-*cloud-amd64` удаляется.

Настройки консоли:

```text
vga: std
tty1 autologin root
serial0: socket
ttyS0 autologin root
Fixed 8x16
```

После первичной настройки выполняется проверочная перезагрузка. Сборка продолжается только если:

- используется ядро `*-amd64`, а не `*cloud*`;
- framebuffer существует и корректен;
- QGA активен;
- службы `getty` для `tty1` и `ttyS0` активны;
- обе настройки консоли используют `--autologin root`.

## 6. Root / SSH

```text
root password: locked
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PubkeyAuthentication yes
```

Имена параметров OpenSSH оставлены без перевода, поскольку это точные настройки.

Скрипт внутри VM-сборщика проверяет `sshd -t`, фактический вывод `sshd -T` для `root` и заблокированное состояние пароля.

В базовом шаблоне не должно быть административных открытых ключей. Перед финализацией `/root/.ssh` удаляется.

## 7. Генератор Cloud-Init

Во время сборки и в CI должен использоваться один генератор:

```text
scripts/pve/setup/render-template-cloud-init.py
```

Генератор должен:

- прочитать и проверить `cloud-init.yaml`;
- обнаружить ровно по одной записи `write_files` для скриптов первоначальной настройки и финализации;
- вставить скрипты гостевой системы;
- подставить `Template-Version`, имя образа и SHA-512;
- запретить оставшиеся незаполненные подстановки;
- атомарно записать итоговый документ.

CI дополнительно проверяет результат через `cloud-init schema`.

## 8. Отображение этапов сборки

`template-bootstrap` публикует текущий этап в:

```text
/var/lib/template-build/bootstrap-status
```

В этом файле разрешены только ASCII-коды этапов:

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

Коды оставлены без перевода, поскольку являются машинным интерфейсом между гостевой системой и PVE.

Русские подписи этапов формируются на стороне PVE в `62-template-build.sh`. Кириллица не передаётся через канал состояния QGA, поскольку вывод `guest-exec` проходит через несколько уровней преобразования байтов и строк и не должен использоваться как транспорт локализованного текста.

До запуска QEMU Guest Agent PVE может показывать только ожидание QGA. После появления агента PVE Configuration читает `bootstrap-status` и выводит текущий этап строками `[ЭТАП VM]`/`[ОЖИДАНИЕ]`.

## 9. Жизненный цикл Cloud-Init

Специальная конфигурация Cloud-Init для VM-сборщика существует только во время сборки.

Перед `qm template`:

```text
очистить гостевую систему
→ проверить результаты очистки через QGA со стороны PVE
→ shutdown
→ удалить cicustom
→ ciuser=root
→ ipconfig0=ip=dhcp
→ ciupgrade=0
→ qm cloudinit update
→ проверить отсутствие user-data, предназначенного только для сборщика
→ удалить временный snippet
```

Описание VM содержит:

```text
template-version=7
```

## 10. Параметры шаблона, видимые со стороны PVE

Существующий и только что созданный `9000` должны соответствовать:

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

Этот блок намеренно остаётся на языке точных параметров Proxmox.

Отсутствующий `protection=1` у в остальном совместимого шаблона можно восстановить после снимка конфигурации. Любое другое несоответствие приводит к остановке.

## 11. Правила Full Clone

Рабочие Debian VM создаются как Full Clone.

До первого запуска задаются CPU, RAM, диск, сеть, `ciuser=root`, необходимые открытые SSH-ключи и параметры Cloud-Init.

Шаблон `9000` остаётся `protection=1`.

Старый или несовместимый шаблон автоматически не заменяется.

## 12. Проверка Full Clone

Штатная проверочная VM:

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

Имена параметров в блоке оставлены без перевода, поскольку соответствуют фактическим настройкам и командам Proxmox.

Правила запуска проверки:

```text
новый шаблон создан текущим запуском
→ проверка обязательна автоматически

существующий шаблон
→ проверка только по --smoke-test-template

/template-smoke.json status=pending
→ проверка обязательна при следующем обычном запуске
```

Перед `qm template` и финализацией нового сборщика PVE Configuration записывает состояние `pending`. Поэтому потеря питания между финализацией и проверкой не позволяет следующему запуску молча считать шаблон полностью проверенным.

Проверки работающей VM `9099`:

```text
Full Clone действительно является обычной VM, а не шаблоном
в description есть проектная защитная отметка
Cloud-Init содержит открытый SSH-ключ PVE
QGA active
Cloud-Init done
kernel *-amd64 и не cloud
пароль root заблокирован
SSH root разрешён только по ключу
реальный SSH root по ключу PVE работает
/etc/machine-id непустой и корректный
созданы новые SSH host keys
корневая файловая система >=18 GiB после увеличения диска до 20G
reboot меняет boot_id
machine-id и SSH host key сохраняются после reboot
QGA и SSH повторно доступны
StrictHostKeyChecking=yes проходит с ключом сервера, принятым до reboot
```

Успешная очистка:

```text
shutdown
→ дождаться stopped
→ qm destroy 9099 --purge 1
→ убедиться, что VMID свободен
→ template-smoke.json status=passed
```

Поведение при ошибке:

```text
ошибка или прерывание после создания 9099
→ НЕ удалять 9099
→ вывести подсказку для диагностики

9099 с проектной отметкой проверки уже существует
→ остановиться и оставить объект для диагностики

9099 занят обычной VM или LXC
→ остановиться без изменения или удаления
```

## 13. Базовые пакеты

```text
qemu-guest-agent openssh-server sudo locales cloud-guest-utils systemd-timesyncd
linux-image-amd64 console-setup console-setup-linux
git mc nano curl wget jq ca-certificates openssl
htop ncdu lsof tree tmux bash-completion
tar rsync zstd unzip acl
dnsutils iproute2 iputils-ping net-tools
cron logrotate
```

Docker/Compose и программы конкретных сервисов в базовый шаблон не входят.

## 14. Диск и TRIM

```text
VirtIO SCSI Single
iothread=1
discard=on
ssd=1
base disk >=16 GiB
```

Включён `fstrim.timer`; перед финализацией выполняется `fstrim -av`.

Проверочная VM отдельно увеличивает полный клон до `20G` до первого запуска и требует корневую файловую систему не меньше `18 GiB`, тем самым проверяя штатное увеличение раздела и файловой системы.

## 15. Очистка с остановкой при несоответствии

`template-finalize.sh` обязан завершиться ошибкой, если универсальный пользователь `debian` существует и его не удалось удалить.

После очистки обязательны проверки:

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

Эти значения описывают фактические файлы и условия проверки и поэтому оставлены в техническом виде.

Проверки выполняются внутри гостевой системы и повторно со стороны PVE через QGA до финализации. Затем проверочная VM подтверждает, что Full Clone создаёт новый `machine-id` и новые SSH host keys.

## 16. Сведения о происхождении сборки

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

Названия полей файла не переводятся, поскольку являются его машинным форматом.

Жёстко закреплённый идентификатор облачной сборки и снимок APT-репозитория не являются частью политики.

Сведения о проверке Full Clone хранятся отдельно на PVE:

```text
/var/lib/proxmox-deployer/state/template-smoke.json
```

В состоянии `passed` фиксируются VMID и версия шаблона, ревизия исходного кода, `machine-id` проверочного клона, ядро, размер корневой файловой системы и fingerprint SSH host key.

## 17. Поведение при ошибке

Существующий VMID `9000` не перезаписывается.

При ошибке сборки VM и диски автоматически не уничтожаются. Следующий запуск распознаёт `builder-debian13` и останавливается для диагностики.

Если VMID свободен, но остался временный фрагмент конфигурации сборщика без самой VM-сборщика, такой фрагмент считается оставшимся артефактом незавершённой сборки и пересоздаётся.

PVE Configuration не переводит несовместимый старый шаблон на v7 автоматически.

Проверочная VM удаляется только после полного успеха. Любое неоднозначное состояние VMID `9099` приводит к остановке без разрушительной очистки.

## 18. Проверки CI и реального PVE

CI проверяет:

- проверку репозитория;
- компиляцию Python;
- `bash -n`;
- ShellCheck;
- основной генератор Cloud-Init;
- разбор YAML;
- `cloud-init schema`;
- отсутствие встроенного помощника `guest-status`;
- точный набор ASCII-кодов этапов;
- модульные проверки спецификации шаблона;
- модульные проверки безопасности и состояния проверочной VM;
- отрицательный тест некорректного каталога гостевой системы;
- отсутствие устаревшего `scripts/pve/create-template.sh`;
- ошибки пробелов и форматирования.

CI не заменяет настоящий PVE. Реальная проверка Full Clone является частью PVE Configuration: автоматически после новой сборки либо явно через `--smoke-test-template` для уже существующего шаблона.
