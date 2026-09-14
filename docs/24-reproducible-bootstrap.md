# Воспроизводимость bootstrap и template

## Статус решения

Политика **принята**.

Bootstrap/recovery не должен зависеть от состояния плавающей ветки `main`, `latest`-образа или installer, содержимое которого может измениться между двумя запусками.

Цель проекта — **функциональная воспроизводимость**:

```text
одинаковый Git revision
+ одинаковые pinned external inputs
+ одинаковая policy
→ эквивалентный PVE/template/runtime state
```

Byte-for-byte идентичность не требуется: machine-id, SSH host keys, timestamps и другие intentionally unique runtime values должны отличаться.

## 1. Git revision

Production/recovery bootstrap запускается только по immutable Git commit SHA.

Не использовать как recovery baseline:

```text
main
master
HEAD
latest
```

Текущий проверенный public bootstrap code baseline:

```text
zsergeyru/proxmox-bootstrap
ef0e3f21532c6e0fe19b79ea83f5c9b8d420d1f2
```

Public AI wrapper требует полный 40-hex `BOOTSTRAP_REF` и загружает version lock и все дочерние scripts из того же revision.

Private PVE deployer также должен сохранять конкретный commit SHA между PLAN и APPLY. Изменившийся `origin/main` не может автоматически изменить уже построенный PLAN.

## 2. Version lock

В текущем переходном public bootstrap внешний набор фиксируется в:

```text
zsergeyru/proxmox-bootstrap/bootstrap-versions.env
```

Принятый baseline:

```text
Template-Version: 5
Debian cloud build: 20260601-2496
Debian build-time APT snapshot: 20260914T000000Z
Proximo: 0.40.0
Hermes: 0.21.2
Hermes commit: 939e45c91d751fadd94dcd1b873ac3cb44846213
Docker CE/CLI: 29.7.2
containerd.io: 2.3.3
Docker Buildx: 0.36.1
Docker Compose plugin: 5.4.0
```

Версии меняются только отдельным Git commit, а не автоматически во время bootstrap.

После завершения миграции builder/deployer в private `zsergeyru/proxmox` equivalent version lock должен находиться рядом с private deploy tooling; public `init-pve.sh` остаётся только zero-day входом и передаёт управление конкретному private repo revision.

## 3. Debian image

Template v5 не использует `trixie/latest`.

Источник:

```text
cloud build: 20260601-2496
image: debian-13-genericcloud-amd64-20260601-2496.qcow2
```

Builder получает `SHA512SUMS` из того же versioned Debian build и выполняет strict SHA-512 verification перед импортом.

Обновление base image — отдельное изменение `DEBIAN_CLOUD_BUILD` с новой сборкой и тестовым Full Clone.

## 4. Build-time APT universe

Даже pinned qcow2 недостаточно для воспроизводимого template, если во время build выполняется `apt full-upgrade` из live repositories.

Поэтому Template v5 во время builder-stage использует timestamped Debian snapshot:

```text
20260914T000000Z
```

Через `snapshot.debian.org` фиксируются версии пакетов, доступные build-процессу.

Перед seal template обычные live Debian repositories восстанавливаются. Snapshot фиксирует **сборку template**, но не замораживает дальнейший lifecycle клонов.

## 5. Docker / Proximo / Hermes

Runtime bootstrap не устанавливает произвольные последние версии.

Docker:

```text
apt package=exact-version
→ verify dpkg version
→ apt-mark hold
```

Proximo:

```text
pip install --upgrade proximo-proxmox==<pinned version>
→ verify importlib.metadata version
```

Hermes:

```text
installer загружается из exact upstream commit
→ installer получает --commit <same SHA>
→ после установки git rev-parse HEAD обязан совпасть
```

Moving installer `https://hermes-agent.nousresearch.com/install.sh` не используется в reproducible path.

## 6. CI guard

CI public bootstrap проверяет как минимум:

- Bash syntax;
- embedded Cloud-Init YAML и Bash;
- формат immutable pins;
- отсутствие `proxmox-bootstrap/main` в исполняемых `.sh`;
- отсутствие Debian `trixie/latest` в исполняемых `.sh`;
- отсутствие moving Hermes installer;
- whitespace.

CI основного private repo отдельно проверяет manifests/schema/docs consistency.

## 7. Promotion нового baseline

Новый bootstrap/template baseline принимается в таком порядке:

```text
изменить pins/code в Git
→ CI
→ clean template build на PVE
→ Full Clone smoke test
→ для Docker-LXC при затронутых изменениях backup/restore test
→ зафиксировать проверенный immutable commit SHA
→ только после этого использовать его как recovery baseline
```

Не считать новый `main` recovery baseline только потому, что CI зелёный.

## 8. Recovery

Для восстановления нужно знать не только инструкции, но и revision.

Минимальный recovery record:

```text
bootstrap repo + commit SHA
private infrastructure repo + commit SHA
Template-Version
external version lock
backup revision/date
```

Secrets не входят в version lock и хранятся/резервируются по [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md) и [`22-storage-and-backup.md`](22-storage-and-backup.md).

## Главный принцип

> `main` показывает текущее развитие проекта; recovery запускается по конкретному проверенному commit SHA и конкретному набору внешних версий.
