#!/usr/bin/env node
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(
  path.join(__dirname, "../../infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web/backup-refresh.js"),
  "utf8",
);
const elements = new Map();
function item() {
  return {
    textContent: "", dataset: {}, hidden: false, disabled: false,
    children: [], attributes: {},
    setAttribute(key, value) { this.attributes[key] = value; },
    removeAttribute(key) { delete this.attributes[key]; },
    replaceChildren(...children) { this.children = children; this.textContent = ""; },
    appendChild(child) { this.children.push(child); },
  };
}
const get = (key) => {
  if (!elements.has(key)) elements.set(key, item());
  return elements.get(key);
};
const buttons = [item(), item()];
for (const button of buttons) {
  button.listeners = {};
  button.addEventListener = (event, action) => {button.listeners[event] = action;};
}
const row = item();
row.dataset.backupRecord = "core";
const cells = {
  state: item(), created: item(), destinations: item(), check: item()
};
row.querySelector = (query) => {
  const found = /data-backup-field="([^"]+)"/.exec(query);
  return found ? cells[found[1]] : null;
};
const dest = item();
dest.dataset.backupDestination = "fast-ssd";
const destCells = {configured: item(), pve: item()};
dest.querySelector = (selector) => selector.includes("configured") ? destCells.configured : destCells.pve;
let requests = 0;
let code = 200;
const data = {
  backup: {
    overall: "single_copy",
    restore_tested: false,
    schedule_declared: "03,09,15,21:30",
    timer: {
      state: "active", service: "activating", last_result: "success",
      next: "Tue 2026-10-21 09:30:00 MSK",
      last: "Tue 2026-10-21 03:30:00 MSK",
    },
    records: {
      core: {state: "verified", created_utc: "2026-10-21T06:00:00+00:00",
             destinations: ["fast-ssd"], check: "restic_subset_1_percent"}
    }
  },
  ssd: {state: "configured"},
  destinations: [{id: "fast-ssd", configured: true, pve_state: "reported_active"}],
};
const listeners = {};
let nextTimerId = 0;
const scheduled = new Map();
const fireTimer = async () => {
  assert.equal(scheduled.size, 1, "Only one in-flight poll timer");
  const [id, callback] = [...scheduled.entries()][0];
  scheduled.delete(id);
  return await callback();
};
const document = {
  visibilityState: "visible",
  addEventListener(name, callback) {listeners[name] = callback;},
  querySelectorAll(selector) {
    if (selector === "[data-backup-refresh]") return buttons;
    if (selector === "[data-backup-record]") return [row];
    if (selector === "[data-backup-destination]") return [dest];
    return [];
  },
  getElementById: get,
  createElement: () => item(),
};
const context = {
  document,
  window: {InfraUI: {formatTime: (v) => "LOCAL:" + v, refreshTimes: () => {}}},
  async fetch(url, args) {
    assert.equal(url, "/api/infrastructure");
    assert.equal(args.method, "GET");
    assert.equal(args.credentials, "same-origin");
    requests += 1;
    return {
      ok: code === 200, status: code,
      json: async () => JSON.parse(JSON.stringify(data)),
    };
  },
  Date, Intl, Set, Error, Number, Array,
  setTimeout(callback, milliseconds) {
    assert.equal(milliseconds, 4000, "Auto-refresh must be 4 seconds");
    const id = ++nextTimerId;
    scheduled.set(id, callback);
    return id;
  },
  clearTimeout(id) {scheduled.delete(id);},
  setInterval: () => {throw new Error("Use bounded one-shot polling, not setInterval");},
};

(async () => {
  get("backup-live-poll").dataset.watch = "false"; // Открыли панель в простое.
  vm.runInNewContext(source, context);
  assert.equal(requests, 0, "Idle page must not poll");
  assert.equal(scheduled.size, 0, "Idle page must not have a timer");
  assert.equal(typeof buttons[0].listeners.click, "function");

  // Клик в момент активности автоматически включает опрос.
  await buttons[0].listeners.click();
  assert.equal(requests, 1);
  assert.equal(scheduled.size, 1);
  assert.equal(get("backup-running").hidden, false);
  assert.equal(get("backup-failure").hidden, true);
  assert.equal(get("backup-service-state").textContent, "Запускается");
  assert.equal(get("backup-summary-text").textContent, "Вторая независимая копия отсутствует");
  assert.equal(cells.state.textContent, "Есть запись об успешной проверке");
  assert.equal(cells.created.children[0].textContent.startsWith("LOCAL:"), true);
  assert.equal(destCells.pve.textContent, "PVE сообщает: активно");
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, false);

  await fireTimer();
  assert.equal(requests, 2, "Active service: one request every 4s");
  assert.equal(scheduled.size, 1, "Active service: polling continues");

  // Скрытая вкладка не должна посылать запросы.
  document.visibilityState = "hidden";
  listeners.visibilitychange();
  assert.equal(scheduled.size, 0);
  document.visibilityState = "visible";
  await listeners.visibilitychange();
  // Обработчик не обязан возвращать promise, дождёмся микрозадач.
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests, 3);
  assert.equal(scheduled.size, 1);

  data.backup.timer.service = "failed";
  data.backup.timer.last_result = "exit-code";
  data.backup.timer.failure_hint = "restic не смог записать архив на SSD <private>";
  await fireTimer();
  assert.equal(requests, 4);
  assert.equal(scheduled.size, 0, "Completed/failed service must stop polling");
  assert.equal(get("backup-running").hidden, true);
  assert.equal(get("backup-failure").hidden, false);
  assert.equal(get("backup-failure-text").textContent, data.backup.timer.failure_hint);
  assert.equal(get("backup-failure-text").children.length, 0, "Unsafe journal HTML must never be parsed");
  assert.equal(get("backup-live-poll").dataset.watch, "false");
  assert.equal(buttons[1].disabled, false);

  // Истёкший сеанс не должен запускать бесконечный опрос.
  code = 401;
  await buttons[0].listeners.click();
  assert.equal(requests, 5);
  assert.equal(scheduled.size, 0);
  assert.match(get("backup-refreshed").textContent, /Сеанс истёк/);
  assert.equal(get("backup-failure").hidden, false, "Failed refresh must not erase old status");

  // После POST /infrastructure/run наблюдение запускается автоматически,
  // даже если systemd ещё не успел выставить active/activating.
  code = 200;
  data.backup.timer.service = "activating";
  get("backup-live-poll").dataset.watch = "true";
  const beforeRequests = requests;
  vm.runInNewContext(source, context);
  assert.equal(requests, beforeRequests);
  assert.equal(scheduled.size, 1, "Redirect after backup start must arm polling");
  await fireTimer();
  assert.equal(requests, beforeRequests + 1);
  assert.equal(scheduled.size, 1);
  data.backup.timer.service = "inactive";
  data.backup.timer.last_result = "success";
  await fireTimer();
  assert.equal(scheduled.size, 0, "Successful completion stops polling");
  console.log("[ОК] Backup live refresh: 4s while active, pause hidden, no overlapping polls, completion and expired session");
})().catch(error => {console.error(error); process.exitCode = 1;});
