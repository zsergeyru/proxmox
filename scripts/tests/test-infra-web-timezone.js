"use strict";
// End-to-end по функции форматирования: настоящее Intl, без браузерного DOM.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.resolve(__dirname, "../../");
const script = fs.readFileSync(path.join(
  root, "infrastructure/guests/910-infra-manager/rootfs/opt/infra-manager/compose/web/ui.js"
), "utf8");
let onTimezoneChange;
const dropdown = {
  value: "local",
  addEventListener: (name, fn) => {
    if (name === "change") onTimezoneChange = fn;
  }
};
const timezoneLabel = {textContent: ""};
const doc = {
  getElementById: (id) => id === "timezone-select" ? dropdown
    : id === "timezone-label" ? timezoneLabel : null,
  querySelectorAll: () => [],
  querySelector: () => null,
  dispatchEvent: () => {}
};
const store = new Map();
const win = {localStorage: {
  getItem: (key) => store.get(key) || null,
  setItem: (key, value) => store.set(key, value)
}};
vm.runInNewContext(script, {document: doc, window: win, Intl, Date, Event});
const stamp = "2026-10-20T18:05:15+00:00";
assert.equal(typeof win.InfraUI.formatTime, "function");
assert.ok(typeof onTimezoneChange === "function");

dropdown.value = "Europe/Moscow";
onTimezoneChange();
assert.match(win.InfraUI.formatTime(stamp), /21:05/);
assert.match(timezoneLabel.textContent, /Москва/);
assert.equal(store.get("infra-web-timezone"), "Europe/Moscow");

dropdown.value = "UTC";
onTimezoneChange();
assert.match(win.InfraUI.formatTime(stamp), /18:05/);
assert.match(timezoneLabel.textContent, /UTC/);
assert.equal(win.InfraUI.formatTime("2026-10-20"), null,
  "Нельзя смещать дату без часов/временной зоны");
assert.equal(win.InfraUI.formatTime("Tue 2026-10-20 21:30:00 MSK"), null,
  "Нельзя угадывать смещение текстового systemd-времени");
assert.equal(win.InfraUI.formatDuration(90000), "1 мин 30 сек");
console.log("[ОК] infra-web: browser timezone UTC/Moscow, time formatting and durations");
