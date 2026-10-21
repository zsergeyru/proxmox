"use strict";

/* Только пользовательское read-only обновление. Никакого фонового опроса. */
(() => {
  const buttons = [...document.querySelectorAll("[data-backup-refresh]")];
  if (!buttons.length) return;
  const el = (id) => document.getElementById(id);
  const updateText = (id, value) => {
    const node = el(id);
    if (node) node.textContent = value == null || value === "" ? "—" : String(value);
  };
  const states = {
    active: "Работает", inactive: "Выключен",
    failed: "Ошибка", unknown: "Не удалось проверить"
  };
  const services = {
    active: "Выполняется", activating: "Запускается",
    inactive: "Не выполняется", failed: "Ошибка", unknown: "Не удалось проверить"
  };
  const overall = {
    no_confirmed_backup: "Нет подтверждённой копии PVE/910",
    stale: "Последняя копия PVE/910 устарела",
    single_copy: "Вторая независимая копия отсутствует",
    two_recorded_copies: "Записаны две копии; текущая доступность носителей не проверена"
  };
  const recordStates = {
    verified: "Есть запись об успешной проверке", stale: "Копия устарела",
    missing: "Нет подтверждённой записи", invalid: "Запись повреждена"
  };
  const pveStates = {
    not_checked: "Нет связи с PVE",
    not_registered: "Не зарегистрировано в PVE",
    reported_active: "PVE сообщает: активно",
    reported_inactive: "PVE сообщает: неактивно",
    unknown: "Неизвестно"
  };
  const knownFailure = new Set(["exit-code", "timeout", "signal"]);
  const displayTime = (node, raw) => {
    if (!node) return;
    node.replaceChildren();
    if (!raw || typeof raw !== "string") {
      node.textContent = "—";
      return;
    }
    const stamp = document.createElement("time");
    stamp.setAttribute("datetime", raw);
    if (/^\d{4}-\d\d-\d\dT/.test(raw)) {
      stamp.setAttribute("data-local-time", "");
      stamp.textContent = window.InfraUI?.formatTime?.(raw) || raw;
    } else if (/^\d{4}-\d\d-\d\d$/.test(raw)) {
      const [year, month, day] = raw.split("-");
      stamp.textContent = day + "." + month + "." + year;
    } else {
      // systemd не всегда сообщает UTC offset; нельзя пересчитывать наугад.
      const matches = /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):\d\d ([A-Z][A-Z0-9+-]{1,9})$/.exec(raw);
      stamp.textContent = matches
        ? matches[3] + "." + matches[2] + "." + matches[1] + " · " + matches[4] + ":" + matches[5] + " " + matches[6]
        : raw;
    }
    node.appendChild(stamp);
    window.InfraUI?.refreshTimes?.(node);
  };
  const updateRecords = (records) => {
    for (const row of document.querySelectorAll("[data-backup-record]")) {
      const record = records?.[row.dataset.backupRecord] || {};
      const field = (key) => row.querySelector('[data-backup-field="' + key + '"]');
      const status = field("state");
      if (status) status.textContent = recordStates[record.state] || "Не проверено";
      displayTime(field("created"), record.created_utc);
      const destinations = field("destinations");
      if (destinations) destinations.textContent = Array.isArray(record.destinations)
        ? record.destinations.join(", ") || "—" : "—";
      const check = field("check");
      if (check) check.textContent =
        record.check === "restic_subset_1_percent" ? "Выборочная проверка данных restic: 1%" :
        record.check === "backup_checked" ? "Проверено при создании" : "Не подтверждено";
    }
  };
  const updateDestinations = (destinations) => {
    for (const row of document.querySelectorAll("[data-backup-destination]")) {
      const entry = destinations.find((item) => item?.id === row.dataset.backupDestination);
      if (!entry) continue;
      const configured = row.querySelector("[data-backup-configured]");
      const status = row.querySelector("[data-backup-pve-state]");
      if (configured) configured.textContent = entry.configured === true
        ? "Объявлен и подтверждён" : "Ожидает настройки";
      if (status) status.textContent = pveStates[entry.pve_state] || "Неизвестно";
    }
  };
  const refresh = async () => {
    buttons.forEach((button) => {button.disabled = true; button.setAttribute("aria-busy", "true");});
    updateText("backup-refreshed", "Проверяю состояние 910…");
    try {
      const response = await fetch("/api/infrastructure", {
        method: "GET", credentials: "same-origin", cache: "no-store"
      });
      if (response.status === 401) throw new Error("Сеанс истёк. Войди в панель снова.");
      if (!response.ok) throw new Error("Сервис временно недоступен. Попробуй позже.");
      const data = await response.json();
      if (!data || !data.backup || !data.backup.timer ||
          !data.backup.records || !Array.isArray(data.destinations)) {
        throw new Error("Не удалось прочитать ответ сервера.");
      }
      const backup = data.backup;
      const timer = backup.timer;
      const running = timer.service === "active" || timer.service === "activating";
      el("backup-running").hidden = !running;
      const failure = knownFailure.has(timer.last_result);
      el("backup-failure").hidden = !failure;
      updateText("backup-failure-text", timer.failure_hint ||
        "Системная служба завершилась с ошибкой; подробности в журнале.");
      const summary = el("backup-summary");
      summary.dataset.tone = backup.overall === "two_recorded_copies" ? "success" : "warning";
      updateText("backup-summary-text", overall[backup.overall] || "Неизвестное состояние копий");
      updateText("backup-timer-state", states[timer.state] || "Не удалось проверить");
      updateText("backup-service-state", services[timer.service] || "Не удалось проверить");
      updateText("backup-schedule", backup.schedule_declared);
      updateText("backup-last-result", timer.last_result);
      updateText("backup-ssd-state", data.ssd?.state);
      updateText("backup-restore-state", backup.restore_tested === true
        ? "Отмечено как испытанное" : "Не подтверждено");
      displayTime(el("backup-next-run"), timer.next);
      displayTime(el("backup-last-run"), timer.last);
      updateRecords(backup.records);
      updateDestinations(data.destinations);
      const moment = new Intl.DateTimeFormat("ru-RU", {hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(new Date());
      updateText("backup-refreshed", "Проверено в " + moment +
        (running ? " · резервирование выполняется; обнови ещё раз позже" :
          " · обновление только вручную"));
    } catch (error) {
      updateText("backup-refreshed", error instanceof Error ? error.message : "Не удалось обновить");
    } finally {
      buttons.forEach((button) => {button.disabled = false; button.removeAttribute("aria-busy");});
    }
  };
  buttons.forEach((button) => button.addEventListener("click", refresh));
})();
