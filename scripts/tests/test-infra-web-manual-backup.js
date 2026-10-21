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
const buttons = [item()];
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
const card = (key) => {
  const node = item();
  node.dataset.backupCopy = key;
  node.fields = {state: item(), date: item(), places: item()};
  node.querySelector = (selector) => {
    if (selector === "[data-backup-copy-state]") return node.fields.state;
    if (selector === "[data-backup-copy-date]") return node.fields.date;
    if (selector === "[data-backup-copy-places]") return node.fields.places;
    return null;
  };
  return node;
};
const cards = [card("core"), card("vm100")];
let requests = 0;
let code = 200;
let hold = false;
let pendingResponse = null;
let poll;
let intervalMs;
const data = {
  backup: {
    overall: "single_copy", restore_tested: false,
    schedule_declared: "03,09,15,21:30",
    timer: {
      state: "active", service: "inactive", last_result: "success",
      next: "Tue 2026-10-21 09:30:00 UTC",
      last: "Tue 2026-10-21 03:30:00 UTC",
    },
    records: {
      core: {state: "verified", created_utc: "2026-10-21T08:28:15+00:00",
             destinations: ["fast-ssd"], check: "restic_subset_1_percent"},
      full: {state: "verified", created_utc: "2026-10-21T08:28:15+00:00",
             destinations: ["fast-ssd"], check: "backup_checked"},
      vm100: {state: "verified", created_utc: "2026-10-21",
              destinations: ["fast-ssd"], check: "backup_checked"}
    }
  },
  ssd: {state: "configured"},
  destinations: [
    {id: "fast-ssd", configured: true, pve_state: "reported_active"},
    {id: "nas-backup", configured: false, pve_state: "not_registered"}
  ],
};
const document = {
  visibilityState: "visible",
  querySelectorAll(selector) {
    if (selector === "[data-backup-refresh]") return buttons;
    if (selector === "[data-backup-record]") return [row];
    if (selector === "[data-backup-destination]") return [dest];
    if (selector === "[data-backup-copy]") return cards;
    return [];
  },
  getElementById: get,
  createElement: () => item(),
};
const respond = () => ({
  ok: code === 200, status: code,
  json: async () => JSON.parse(JSON.stringify(data)),
});
const context = {
  document,
  window: {InfraUI: {formatTime: (v) => "LOCAL:" + v, refreshTimes: () => {}}},
  fetch(url, args) {
    assert.equal(url, "/api/infrastructure");
    assert.equal(args.method, "GET");
    assert.equal(args.credentials, "same-origin");
    assert.equal(args.cache, "no-store");
    requests += 1;
    return hold ? new Promise((resolve) => {pendingResponse = () => resolve(respond());})
      : Promise.resolve(respond());
  },
  Date, Intl, Set, Error, Number, Array,
  setInterval(fn, ms) {
    assert.equal(poll, undefined, "Exactly one polling timer");
    poll = fn; intervalMs = ms; return 1;
  },
};
const settle = () => new Promise((resolve) => setImmediate(resolve));

(async () => {
  vm.runInNewContext(source, context);
  assert.equal(intervalMs, 4000, "The browser polls every four seconds");
  await settle();
  assert.equal(requests, 1, "Initial state loads automatically");
  assert.equal(get("backup-running").hidden, true);
  assert.equal(get("backup-failure").hidden, true);
  assert.equal(get("backup-summary-text").textContent,
    "Копии PVE/910 и Home Assistant созданы");
  assert.equal(get("backup-summary").dataset.tone, "success");
  assert.match(get("backup-media-summary").textContent, /NAS: пока не настроен/);
  assert.equal(get("backup-plan-state").textContent, "Включено · ожидает запуска");
  assert.equal(get("backup-service-state").textContent, "Ожидает запуска");
  assert.equal(cards[0].fields.state.textContent, "Копия создана");
  assert.equal(cards[1].fields.state.textContent, "Копия создана");
  assert.match(cards[0].fields.date.children[0].textContent, /LOCAL:/);
  assert.equal(cards[1].fields.date.children[0].textContent, "21.10.2026");
  assert.equal(destCells.pve.textContent, "PVE сообщает: активно");
  assert.equal(buttons[0].disabled, false);

  poll();
  await settle();
  assert.equal(requests, 2, "Automatic refresh without click");
  document.visibilityState = "hidden";
  poll();
  await settle();
  assert.equal(requests, 2, "Background tab stops polling");
  document.visibilityState = "visible";

  hold = true;
  poll();
  assert.equal(requests, 3);
  poll();
  await buttons[0].listeners.click();
  assert.equal(requests, 3, "In-flight request must not duplicate");
  hold = false;
  pendingResponse();
  await settle();
  assert.equal(buttons[0].disabled, false);

  data.backup.timer.service = "failed";
  data.backup.timer.last_result = "exit-code";
  data.backup.timer.failure_hint = "restic не смог записать архив на SSD <private>";
  await buttons[0].listeners.click();
  assert.equal(requests, 4);
  assert.equal(get("backup-summary-text").textContent, "Последний запуск завершился с ошибкой");
  assert.equal(get("backup-failure").hidden, false);
  assert.equal(get("backup-failure-text").textContent, data.backup.timer.failure_hint);
  assert.equal(get("backup-failure-text").children.length, 0, "Never parse unsafe HTML");
  assert.equal(cards[0].fields.state.textContent, "Копия создана",
    "An old verified backup must remain visible after later failure");

  data.backup.timer.service = "inactive";
  data.backup.timer.last_result = "success";
  data.backup.records.vm100.state = "missing";
  await buttons[0].listeners.click();
  assert.match(get("backup-summary-text").textContent, /Home Assistant ещё не подтверждён/);
  assert.equal(cards[1].fields.state.textContent, "Копии пока нет");

  code = 401;
  poll();
  await settle();
  assert.match(get("backup-refreshed").textContent, /Сеанс истёк/);
  const count = requests;
  poll();
  await settle();
  assert.equal(requests, count, "No repeated unauthorized background polling");
  assert.equal(get("backup-summary-text").textContent,
    "Копия PVE/910 создана; Home Assistant ещё не подтверждён",
    "Failed refresh leaves existing status but indicates it is stale");
  assert.match(get("backup-refreshed").textContent, /предыдущие данные/);
  console.log("[ОК] Backup UI: status, 4-second polling, hidden-tab pause, no overlap, failure, 401");
})().catch(error => {console.error(error); process.exitCode = 1;});
