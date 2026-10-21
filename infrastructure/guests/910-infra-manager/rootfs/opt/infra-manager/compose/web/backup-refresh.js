"use strict";

/* Только чтение: обновление каждые четыре секунды без перезапуска копирования.
   Не допускаем параллельных запросов и останавливаем опрос в фоновой вкладке. */
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
    inactive: "Ожидает запуска", failed: "Ошибка", unknown: "Не удалось проверить"
  };
  const recordStates = {
    verified: "Есть запись об успешной проверке", partial: "Подтверждён один носитель", stale: "Копия устарела",
    missing: "Нет подтверждённой записи", invalid: "Запись повреждена"
  };
  const pveStates = {
    not_checked: "Нет связи с PVE", not_registered: "Не зарегистрировано в PVE",
    reported_active: "PVE сообщает: активно",
    reported_inactive: "PVE сообщает: неактивно", unknown: "Неизвестно"
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
      // systemd может сообщать время без подтверждённого UTC-offset.
      const parts = /^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):\d\d ([A-Z][A-Z0-9+-]{1,9})$/.exec(raw);
      stamp.textContent = parts
        ? parts[3] + "." + parts[2] + "." + parts[1] + " · " + parts[4] + ":" + parts[5] + " " + parts[6]
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
        record.check === "restic_subset_1_percent" ? "Выборочная проверка Restic: 1%" :
        record.check === "backup_checked" ? "Проверено при создании" : "Не подтверждено";
    }
  };
  const updateCards = (records) => {
    for (const card of document.querySelectorAll("[data-backup-copy]")) {
      const record = records?.[card.dataset.backupCopy] || {};
      const state = record.state;
      card.dataset.tone = state === "verified" ? "success" : "warning";
      const label = card.querySelector("[data-backup-copy-state]");
      if (label) label.textContent = ({
        verified: "Копия создана", partial: "Есть только часть копий", stale: "Копия устарела",
        missing: "Копии пока нет", invalid: "Нет подтверждения"
      })[state] || "Не проверено";
      displayTime(card.querySelector("[data-backup-copy-date]"), record.created_utc);
      const places = card.querySelector("[data-backup-copy-places]");
      if (places) places.textContent = Array.isArray(record.destinations)
        ? record.destinations.join(", ") || "—" : "—";
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
  const updateOverview = (data) => {
    const backup = data.backup;
    const timer = backup.timer;
    const records = backup.records;
    const core = records.core || {};
    const vm100 = records.vm100 || {};
    const full = records.full || {};
    const running = timer.service === "active" || timer.service === "activating";
    const failed = knownFailure.has(timer.last_result);
    const volumes = Array.isArray(backup.volume_jobs) ? backup.volume_jobs : [];
    const protectedVolumes = volumes.filter((job) =>
      ["verified", "partial"].includes(records[job.record_key]?.state)).length;
    const complete = [core, vm100, full].every((x) => x.state === "verified");
    const heading = running ? "Резервирование выполняется" :
      failed ? "Последний запуск завершился с ошибкой" :
      complete ? "Копии PVE/910 и Home Assistant созданы" :
      core.state === "verified" ? "Копия PVE/910 создана; Home Assistant ещё не подтверждён" :
      core.state === "stale" ? "Резервную копию PVE/910 нужно обновить" :
      "Подтверждённых резервных копий пока нет";
    const summary = el("backup-summary");
    if (summary) summary.dataset.tone = running ? "info" : failed ? "danger" :
      complete ? "success" : "warning";
    updateText("backup-summary-text", heading +
      (volumes.length ? " · Тома: " + protectedVolumes + "/" + volumes.length +
       " имеют актуальную копию" : ""));
    displayTime(el("backup-last-success"), core.created_utc);
    const coreTargets = Array.isArray(core.destinations) ? core.destinations : [];
    const nasConfigured = data.destinations.some((x) => x.id === "nas-backup" && x.configured === true);
    updateText("backup-media-summary",
      (coreTargets.includes("fast-ssd") ? "SSD: копия PVE/910 записана." : "SSD: копия не подтверждена.") +
      " " + (coreTargets.includes("nas-backup") ? "NAS: копия PVE/910 записана." :
        nasConfigured ? "NAS: копия не подтверждена." : "NAS: пока не настроен."));
    updateCards(records);
    updateRecords(records);
    updateDestinations(data.destinations);
    const runningBanner = el("backup-running");
    if (runningBanner) runningBanner.hidden = !running;
    const failureBanner = el("backup-failure");
    if (failureBanner) failureBanner.hidden = !failed;
    updateText("backup-failure-text", timer.failure_hint ||
      "Подробности доступны в журнале службы 910.");
    updateText("backup-timer-state", states[timer.state] || "Не удалось проверить");
    updateText("backup-service-state", services[timer.service] || "Не удалось проверить");
    updateText("backup-schedule", backup.schedule_declared);
    const plan = el("backup-plan-state");
    if (plan) {
      plan.textContent = running ? "Сейчас выполняется" :
        timer.state === "active" ? "Включено · ожидает запуска" :
        timer.state === "inactive" ? "Таймер выключен" : "Нужно проверить таймер";
      plan.dataset.tone = timer.state === "active" ? "success" : "warning";
    }
    updateText("backup-last-result", timer.last_result);
    updateText("backup-ssd-state", data.ssd?.state);
    updateText("backup-restore-state", backup.restore_tested === true ? "Испытано" : "Ещё не проверено");
    displayTime(el("backup-next-run"), timer.next);
    displayTime(el("backup-last-run"), timer.last);
  };
  let pending = false;
  let sessionExpired = false;
  const refresh = async (manual = false) => {
    if (pending || (!manual && (sessionExpired || document.visibilityState === "hidden"))) return;
    pending = true;
    buttons.forEach((button) => {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
    });
    try {
      const response = await fetch("/api/infrastructure", {
        method: "GET", credentials: "same-origin", cache: "no-store"
      });
      if (response.status === 401) {
        sessionExpired = true;
        throw new Error("Сеанс истёк. Войди в панель снова.");
      }
      if (!response.ok) throw new Error("Сервис временно недоступен.");
      const data = await response.json();
      if (!data || !data.backup || !data.backup.timer ||
          !data.backup.records || !Array.isArray(data.destinations)) {
        throw new Error("Не удалось прочитать ответ сервера.");
      }
      sessionExpired = false;
      updateOverview(data);
      const moment = new Intl.DateTimeFormat("ru-RU", {
        hour: "2-digit", minute: "2-digit", second: "2-digit"
      }).format(new Date());
      updateText("backup-refreshed", "Проверено в " + moment + " · автообновление каждые 4 сек");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Не удалось обновить";
      updateText("backup-refreshed", message + " Показаны предыдущие данные.");
    } finally {
      pending = false;
      buttons.forEach((button) => {
        button.disabled = false;
        button.removeAttribute("aria-busy");
      });
    }
  };
  buttons.forEach((button) => button.addEventListener("click", () => refresh(true)));
  // Один таймер на страницу, без параллельных запросов, в фоновой вкладке не опрашиваем.
  setInterval(() => { void refresh(false); }, 4000);
  void refresh(false);
})();
