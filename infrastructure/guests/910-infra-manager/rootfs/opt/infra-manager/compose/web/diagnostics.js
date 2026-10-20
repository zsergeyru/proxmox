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
  const operations = {deploy: "Развёртывание", status: "Проверка",
    sync: "Синхронизация", repair: "Исправление"};
  const terminal = new Set(["completed", "failed", "rolled-back", "needs-recovery"]);
  const element = (name) => document.getElementById(name);
  const text = (name, value) => {
    element(name).textContent = value == null || value === "" ? "—" : String(value);
  };
  const formatDuration = (start, finish) => {
    const duration = Date.parse(finish) - Date.parse(start);
    return window.InfraUI?.formatDuration?.(duration) || "—";
  };

  function localStamp(name, input) {
    const host = element(name);
    host.replaceChildren();
    if (typeof input !== "string" || !input) {
      host.textContent = "—";
      return;
    }
    const stamp = document.createElement("time");
    stamp.dateTime = input;
    stamp.setAttribute("data-local-time", "");
    stamp.textContent = window.InfraUI?.formatTime?.(input) || input;
    host.appendChild(stamp);
    window.InfraUI?.refreshTimes?.(host);
  }

  const search = element("op-log-search");
  const log = element("op-log");
  let allLogs = [];
  const showLogs = () => {
    const query = (search?.value || "").toLocaleLowerCase("ru").trim();
    const visible = query
      ? allLogs.filter((line) => line.toLocaleLowerCase("ru").includes(query))
      : allLogs;
    const atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    log.textContent = visible.join("\n") || (query ?
      "Совпадений нет. Попробуй другое слово." : "Безопасных сообщений пока нет.");
    if (atEnd) log.scrollTop = log.scrollHeight;
  };
  search?.addEventListener("input", showLogs);

  let busy = false;
  const update = async () => {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch("/api/operation?id=" + id, {
        credentials: "same-origin", cache: "no-store"
      });
      if (response.status === 401) {
        text("op-refresh", "Сеанс истёк. Перейди на главную для входа.");
        return;
      }
      if (!response.ok) throw new Error("HTTP " + response.status);
      const result = await response.json();
      if (!result.operation || result.operation.id !== id ||
          !Array.isArray(result.events) || !Array.isArray(result.logs)) {
        throw new Error("Некорректный ответ");
      }
      const op = result.operation;
      text("op-kind", operations[op.kind] || op.kind);
      text("op-target", op.target);
      const status = element("op-phase");
      status.textContent = statuses[op.phase] || op.phase || "Неизвестно";
      status.className = "chip";
      status.dataset.tone =
        op.phase === "completed" ? "success" :
        ["failed", "needs-recovery"].includes(op.phase) ? "danger" :
        ["running", "activating", "verifying"].includes(op.phase) ? "info" : "warning";
      localStamp("op-start", op.started_at || op.created_at);
      localStamp("op-finish", op.finished_at);
      text("op-exit", op.exit_code);
      text("op-diagnostic", result.diagnostic || "Нет сообщений о неисправностях.");

      const duration = element("op-duration");
      if (op.started_at || op.created_at) {
        duration.dataset.durationStart = op.started_at || op.created_at;
        if (op.finished_at) duration.dataset.durationEnd = op.finished_at;
        else delete duration.dataset.durationEnd;
        duration.textContent = formatDuration(op.started_at || op.created_at,
          op.finished_at || new Date().toISOString());
      } else duration.textContent = "—";

      const stages = element("op-timeline");
      const fragment = document.createDocumentFragment();
      result.events.forEach((event, index) => {
        if (!event || !event.at || !statuses[event.phase]) return;
        const next = result.events[index + 1];
        const finish = next?.at || op.finished_at ||
          (terminal.has(op.phase) ? event.at : new Date().toISOString());
        const item = document.createElement("li");
        const label = document.createElement("div");
        label.className = "timeline-name";
        label.textContent = statuses[event.phase];
        const detail = document.createElement("div");
        detail.className = "timeline-detail";
        const time = document.createElement("time");
        time.setAttribute("datetime", event.at);
        time.setAttribute("data-local-time", "");
        time.textContent = window.InfraUI?.formatTime?.(event.at) || event.at;
        detail.append(time, document.createTextNode(" · " + formatDuration(event.at, finish)));
        item.append(label, detail);
        fragment.appendChild(item);
      });
      stages.replaceChildren(fragment);
      window.InfraUI?.refreshTimes?.(stages);
      allLogs = result.logs.filter((entry) => typeof entry === "string");
      showLogs();
      text("op-refresh", terminal.has(op.phase)
        ? "Задание завершено. Результат сохранён; данные проверяются каждые 4 секунды."
        : "Задание выполняется. Данные обновляются каждые 4 секунды.");
    } catch (_) {
      text("op-refresh", "Данные пока недоступны. Повторная попытка через 4 секунды.");
    } finally {
      busy = false;
    }
  };

  document.addEventListener("infra-timezone-changed", () => update());
  element("op-copy")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(id);
      text("op-refresh", "ID задания скопирован.");
    } catch (_) {
      text("op-refresh", "Не удалось скопировать ID. Он указан выше.");
    }
  });
  element("op-copy-log")?.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(log.textContent || "");
      text("op-refresh", "Показанные строки журнала скопированы.");
    } catch (_) {
      text("op-refresh", "Браузер не разрешил копирование журнала.");
    }
  });
  update();
  setInterval(update, 4000);
})();
