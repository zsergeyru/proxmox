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
const document = {
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
  setInterval: () => {throw new Error("Manual page must not schedule polling");},
};

(async () => {
  vm.runInNewContext(source, context);
  assert.equal(requests, 0, "No automatic requests on page open");
  assert.equal(typeof buttons[0].listeners.click, "function");
  await buttons[0].listeners.click();
  assert.equal(requests, 1);
  assert.equal(get("backup-running").hidden, false);
  assert.equal(get("backup-failure").hidden, true);
  assert.equal(get("backup-service-state").textContent, "Запускается");
  assert.equal(get("backup-summary-text").textContent, "Вторая независимая копия отсутствует");
  assert.equal(cells.state.textContent, "Есть запись об успешной проверке");
  assert.equal(cells.created.children[0].textContent.startsWith("LOCAL:"), true);
  assert.equal(destCells.pve.textContent, "PVE сообщает: активно");
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, false);

  data.backup.timer.service = "failed";
  data.backup.timer.last_result = "exit-code";
  data.backup.timer.failure_hint = "restic не смог записать архив на SSD <private>";
  await buttons[1].listeners.click();
  assert.equal(requests, 2);
  assert.equal(get("backup-running").hidden, true);
  assert.equal(get("backup-failure").hidden, false);
  assert.equal(get("backup-failure-text").textContent, data.backup.timer.failure_hint);
  assert.equal(get("backup-failure-text").children.length, 0, "Unsafe journal HTML must never be parsed");
  code = 401;
  await buttons[0].listeners.click();
  assert.equal(requests, 3);
  assert.match(get("backup-refreshed").textContent, /Сеанс истёк/);
  assert.equal(get("backup-failure").hidden, false, "Failed refresh must not erase old status");
  console.log("[ОК] Manual backup refresh: no polling, safe DOM, timestamps, failure and expired session");
})().catch(error => {console.error(error); process.exitCode = 1;});
