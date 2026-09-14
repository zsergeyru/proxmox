# Версии и воспроизводимость bootstrap/template

## Статус

Архитектура bootstrap/deploy сохраняется. Пересмотрено только прежнее усложнённое решение по жёсткой фиксации версий и recovery lock.

С нуля переписываются scripts; архитектурные требования определяются другими документами проекта.

## Template

Активный builder:

```text
zsergeyru/proxmox/scripts/pve/create-template.sh
```

Текущая модель Template-Version 4 намеренно простая:

```text
официальный Debian 13 trixie/latest
→ SHA-512 verification
→ обычные Debian repositories
→ apt update/full-upgrade
→ сборка template
```

Не используются:

```text
pinned Debian cloud build
snapshot.debian.org
отдельный template version lock
recovery mode для builder
```

При этом фактически использованный image и его SHA-512 записываются в `/etc/vm-template-info`, поэтому происхождение конкретного созданного template остаётся видимым.

## Новые bootstrap/deploy scripts

Для будущих scripts не действует прежнее требование обязательно фиксировать каждую внешнюю версию.

Базовый принцип для домашней инфраструктуры:

```text
обычная установка
→ актуальные stable версии
→ проверка результата
```

Если для конкретного компонента позже потребуется pin/recovery baseline, это добавляется точечно и обоснованно, а не как обязательный общий механизм для всей инфраструктуры.

Это implementation/versioning policy и она не отменяет архитектуру из:

- [`20-pve-initialization.md`](20-pve-initialization.md);
- [`21-pve-filesystem-layout.md`](21-pve-filesystem-layout.md);
- [`31-bootstrap.md`](31-bootstrap.md);
- AI/guest ADR.

## Что отменено из предыдущего эксперимента

Не являются обязательными требованиями новой реализации:

```text
BOOTSTRAP_MODE=normal/recovery
общий bootstrap-versions.env
immutable BOOTSTRAP_REF для каждого запуска
Docker exact-version + apt-mark hold
Hermes exact commit pin
Proximo exact package pin
Debian APT snapshot
```

Эти механизмы сохранены в Git history/archive и могут быть использованы точечно, если появится реальная необходимость.

## Что остаётся обязательным

Даже при простой модели новые scripts должны:

- проверять скачиваемые artefacts штатными checksum/signature механизмами, если они доступны;
- не хранить secrets в Git;
- явно проверять результат установки;
- не делать молчаливый destructive overwrite;
- оставлять достаточно информации для диагностики того, что фактически установлено;
- соблюдать принятую архитектуру и security boundaries.

## Главный принцип

> Не усложнять управление версиями без практической необходимости: использовать актуальные stable-компоненты, проверять результат и добавлять pin/recovery механизмы только там, где они действительно нужны.
