"""Разбор общих аргументов инфраструктурных заданий Semaphore."""

from __future__ import annotations

from .common import InfraManagerError


def extract_survey_vmid(argv: list[str]) -> tuple[list[str], int | None]:
    """Извлечь GUEST_VMID, который Semaphore передаёт как survey-переменную."""

    remaining: list[str] = []
    values: list[str] = []
    for item in argv:
        if item.startswith("GUEST_VMID="):
            values.append(item.split("=", 1)[1])
        else:
            remaining.append(item)

    if len(values) > 1:
        raise InfraManagerError("GUEST_VMID передан более одного раза")
    if not values:
        return remaining, None
    if not values[0].isdigit() or int(values[0]) <= 0:
        raise InfraManagerError("GUEST_VMID должен быть положительным VMID")
    return remaining, int(values[0])
