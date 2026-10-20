"use strict";

/* Только отображение: даты, фильтры и удобство чтения.
   Нет отправки команд, паролей и запросов к системному посреднику. */
(() => {
  const timeNames = {local: "Моё время", "Europe/Moscow": "Москва", UTC: "UTC"};
  const zoneSelect = document.getElementById("timezone-select");
  const label = document.getElementById("timezone-label");
  const browserZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const settingsKey = "infra-web-timezone";
  let chosen = "local";
  try {
    const saved = window.localStorage.getItem(settingsKey);
    if (Object.prototype.hasOwnProperty.call(timeNames, saved)) chosen = saved;
  } catch (_) {
    /* Private browsing can disable browser storage. */
  }
  const getZone = () => chosen === "local" ? browserZone : chosen;
  const prettyTime = (input, long = false) => {
    if (typeof input !== "string") return null;
    // Только ISO с известным часовым смещением. Неполную дату не сдвигать.
    if (!/^\d{4}-\d\d-\d\dT\d\d:\d\d(?::\d\d)?(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$/.test(input)) return null;
    const date = new Date(input);
    if (Number.isNaN(date.getTime())) return null;
    return new Intl.DateTimeFormat("ru-RU", {
      timeZone: getZone(), day: "2-digit", month: "2-digit",
      year: "numeric", hour: "2-digit", minute: "2-digit",
      ...(long ? {second: "2-digit"} : {}),
      hourCycle: "h23"
    }).format(date).replace(",", " ·");
  };
  const formatDuration = (milliseconds) => {
    if (!Number.isFinite(milliseconds) || milliseconds < 0) return "—";
    const seconds = Math.floor(milliseconds / 1000);
    if (seconds >= 3600) return Math.floor(seconds / 3600) + " ч " + Math.floor(seconds % 3600 / 60) + " мин";
    if (seconds >= 60) return Math.floor(seconds / 60) + " мин " + seconds % 60 + " сек";
    return seconds + " сек";
  };

  const applyTime = (root = document) => {
    root.querySelectorAll("time[data-local-time]").forEach((time) => {
      const input = time.getAttribute("datetime");
      const shown = prettyTime(input);
      if (!shown) return;
      time.textContent = shown;
      time.title = "Исходное время: " + input + " · зона: " + getZone();
    });
    root.querySelectorAll("[data-duration-start]").forEach((target) => {
      const a = Date.parse(target.dataset.durationStart);
      const b = target.dataset.durationEnd ? Date.parse(target.dataset.durationEnd) : Date.now();
      target.textContent = formatDuration(b - a);
    });
    if (label) label.textContent = "Показано: " + (chosen === "local" ? browserZone : timeNames[chosen]);
  };
  if (zoneSelect) {
    zoneSelect.value = chosen;
    zoneSelect.addEventListener("change", () => {
      chosen = zoneSelect.value;
      try { window.localStorage.setItem(settingsKey, chosen); } catch (_) { /* no storage */ }
      applyTime();
      document.dispatchEvent(new Event("infra-timezone-changed"));
    });
  }

  const filters = document.querySelectorAll("[data-filter-list]");
  filters.forEach((list) => {
    const key = list.dataset.filterList;
    const search = document.querySelector('[data-search-for="' + key + '"]');
    const select = document.querySelector('[data-state-for="' + key + '"]');
    const counter = document.querySelector('[data-count-for="' + key + '"]');
    const empty = document.querySelector('[data-empty-for="' + key + '"]');
    const items = [...list.querySelectorAll("[data-filter-item]")];
    const filter = () => {
      const query = (search?.value || "").toLocaleLowerCase("ru").trim();
      const state = select?.value || "all";
      let count = 0;
      items.forEach((item) => {
        const text = (item.dataset.search || item.textContent || "").toLocaleLowerCase("ru");
        const show = (!query || text.includes(query))
          && (state === "all" || item.dataset.state === state);
        item.hidden = !show;
        if (show) count += 1;
      });
      if (counter) counter.textContent = count + " из " + items.length;
      if (empty) empty.hidden = count !== 0;
    };
    if (search) search.addEventListener("input", filter);
    if (select) select.addEventListener("change", filter);
    filter();
  });
  applyTime();
  // Длительность выполняющейся операции увеличивается без сетевых запросов.
  if (document.querySelector("[data-duration-start]")) {
    setInterval(() => applyTime(), 10000);
  }
  window.InfraUI = {formatTime: prettyTime, formatDuration, refreshTimes: applyTime};
})();
