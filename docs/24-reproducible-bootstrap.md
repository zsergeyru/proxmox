# Воспроизводимость bootstrap и template

## Статус

Политика общего bootstrap снята с канонического статуса и будет спроектирована заново.

Для действующего template также отказались от избыточной схемы с pinned Debian build, отдельным version lock и `snapshot.debian.org`.

## Template

Активный builder находится в публичном:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Текущая версия:

```text
Template-Version: 4
```

Builder использует:

```text
Debian 13 Trixie current cloud image из trixie/latest
SHA-512 verification из того же upstream каталога
обычные Debian repositories
apt update/full-upgrade на момент сборки
```

То есть две сборки в разные даты могут получить разные версии Debian packages. Для домашней инфраструктуры это сейчас принято как нормальный и более простой вариант.

## Что фиксируется

В `/etc/vm-template-info` сохраняются фактические данные конкретной сборки:

```text
Template-Version
Source-Image
Source-Image-SHA512
Build-Date
Kernel-Flavor
Console modes
```

Это даёт достаточную трассируемость без отдельной системы version lock.

## Что больше не используется

Для template больше не являются требованиями:

```text
фиксированный Debian cloud build ID
Debian APT snapshot timestamp
bootstrap-versions.env
recovery lock для template
```

Экспериментальная Template v5 с такой схемой сохранена в Git history, но не является действующей архитектурой.

## Остальной bootstrap

Предыдущие решения по PVE bootstrap, Docker, Proximo, Hermes, AI Control, recovery и deploy workflow также не считаются принятыми и будут спроектированы заново.

Нельзя автоматически переносить прежние assumptions о `BOOTSTRAP_REF`, package pins, Docker hold, Hermes commit pin и подобных механизмах в новую реализацию.

## Главный текущий принцип

Для template важны простота, проверка скачанного образа и понятный smoke test после изменений. Полная повторяемость package universe сейчас не является целью.
