# Воспроизводимость bootstrap и template

## Статус

Политика общего bootstrap **снята с канонического статуса и будет спроектирована заново**.

Сейчас этот документ фиксирует только требования, которые остаются актуальны для действующего `create-template.sh`.

## Template

Активный builder находится в публичном:

```text
zsergeyru/proxmox-bootstrap/create-template.sh
```

Текущая реализация Template-Version 5 использует:

```text
Debian cloud build: 20260601-2496
build-time APT snapshot: 20260914T000000Z
SHA-512 verification исходного image
```

После build обычные Debian repositories восстанавливаются, поэтому будущие clones не остаются привязаны к build snapshot.

Такой подход пока сохраняется для template, потому что позволяет точно понимать, из какого base image и package universe собран `9000`.

## Что больше не является принятой общей policy

Предыдущий документ распространял pinned/recovery модель на:

```text
PVE bootstrap
Docker
Proximo
Hermes
AI Control
общий deploy workflow
```

Эта часть решения отменена вместе со старым bootstrap design.

Предыдущие pins и scripts сохранены в Git history и в public archive, но **не являются требованиями новой реализации**.

В частности, новая система позже может выбрать другой баланс между:

```text
latest stable для обычного deploy
и
pinned known-good versions для recovery/rollback
```

Это решение будет принято заново при проектировании нового bootstrap.

## Главный текущий принцип

До появления нового bootstrap design нельзя переносить старые assumptions о `BOOTSTRAP_REF`, AI version lock, Docker hold, Hermes commit pin и подобных механизмах в новую реализацию автоматически.

Для template source provenance и checksum остаются обязательными; для остальных компонентов policy пока TBD.
