"use strict";

(() => {
  const view = document.getElementById("operation-view");
  if (!view) return;
  const id = view.dataset.operationId;
  if (!/^[0-9a-f]{32}$/.test(id || "")) return;

  const statuses = {
    queued: "Ожидает", running: "Выполняется",
    activating: "Активация", verifying: "Проверка",
    completed: "Успешно", failed: "Ошибка",
    "rolled-back": "Откат", "needs-recovery": "Нужна проверка"
  };
  const terminal = new Set(["completed", "failed", "rolled-back", "needs-recovery"]);
  const element = (name) => document.getElementById(name);
  const text = (name, value) => { element(name).textContent = value == null ? "—" : String(value); };

  const duration = (start, end) => {
    const seconds = Math.max(0, Math.floor((Date.parse(end) - Date.parse(start)) / 1000));
    if (!Number.isFinite(seconds)) return "—";
    if (seconds >= 3600) return Math.floor(seconds / 3600) + " ч " + Math.floor(seconds % 3600 / 60) + " мин";
    if (seconds >= 60) return Math.floor(seconds / 60) + " мин " + seconds % 60 + " с";
    return seconds + " с";
  };

  let busy = false;
  const update = async () => {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/operation?id=" + id, {
        credentials: "same-origin", cache: "no-store"
      });
      if (response.status === 401) {
        text("op-refresh", "Сеанс истёк. Войдите заново через главную страницу.");
        return;
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      const result = await response.json();
      if (!result.operation || result.operation.id !== id ||
          !Array.isArray(result.events) || !Array.isArray(result.logs)) {
        throw new Error("Некорректный ответ");
      }
      const op = result.operation;
      text("op-kind", op.kind);
      text("op-target", op.target);
      text("op-phase", statuses[op.phase] || op.phase || "Неизвестно");
      text("op-start", op.started_at || op.created_at);
      text("op-finish", op.finished_at);
      text("op-exit", op.exit_code);
      text("op-diagnostic", result.diagnostic || "");
      const stages = element("op-timeline");
      const fragment = document.createDocumentFragment();
      result.events.forEach((event, index) => {
        if (!event || !event.at || !statuses[event.phase]) return;
        const next = result.events[index + 1];
        const finish = next?.at || op.finished_at ||
          (terminal.has(op.phase) ? event.at : new Date().toISOString());
        const item = document.createElement("li");
        item.textContent = event.at + " — " + statuses[event.phase] +
          " (" + duration(event.at, finish) + ")";
        fragment.appendChild(item);
      });
      stages.replaceChildren(fragment);
      const log = element("op-log");
      const wasAtEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 45;
      log.textContent = result.logs.join("\n") || "Безопасных сообщений пока нет.";
      if (wasAtEnd) log.scrollTop = log.scrollHeight;
      text("op-refresh", terminal.has(op.phase) ?
        "Задание завершено. Результат сохранён; обновление каждые 4 секунды." :
        "Задание выполняется. Данные обновляются каждые 4 секунды.");
    } catch (_) {
      text("op-refresh", "Диагностика временно недоступна; повторная проверка через 4 секунды.");
    } finally {
      busy = false;
    }
  };
  update();
  setInterval(update, 4000);
})();
