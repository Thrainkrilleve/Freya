"use strict";
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");
const html = fs.readFileSync("src/freya/gateway/static/index.html", "utf8");
const script = fs.readFileSync("src/freya/gateway/static/media.js", "utf8");
class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this.events = {}; this.value = ""; this.files = []; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  addEventListener(name, callback) { this.events[name] = callback; }
  click() { if (this.events.click) this.events.click(); }
  pause() {}
  load() {}
  removeAttribute() {}
}
const elements = new Map([...html.matchAll(/id="([^"]+)"/g)].map((m) => [m[1], new Element()]));
const handlers = {};
const sent = [];
const context = {
  document: {getElementById: (id) => { assert(elements.has(id), "Missing control " + id); return elements.get(id); }, createElement: (tag) => new Element(tag), addEventListener() {}, hidden: false},
  window: {addEventListener: (name, callback) => { handlers[name] = callback; }, confirm: () => true},
  send: (message) => { sent.push(message); return true; },
  location: {origin: "http://127.0.0.1:8770"},
  URL, TextEncoder, setInterval() {}, Date, Map, Number, WebSocket: {OPEN: 1}, socket: null,
};
vm.runInNewContext(script, context, {filename: "media.js"});
const control = (name) => elements.get("media-" + name);
const reply = (answer) => handlers["freya-media"]({detail: {...answer, request: sent.at(-1).request}});
handlers["freya-view"]({detail: "media"});
reply({items: [{id: "a".repeat(32), title: "<img onerror=alert(1)>", provider: "local"}], availability: {roots: []}, settings: {}, jobs: []});
assert.equal(control("library").children[0].children[0].textContent, "<img onerror=alert(1)> · local");
control("library").children[0].children[1].click();
reply({item: {id: "a".repeat(32), title: "Example", provider: "local", tags: []}, notes: [], summaries: [], transcript: [], skips: []});
control("play").click();
reply({media_id: "a".repeat(32), player: {kind: "file", media: "video", url: "/media/stream/" + "z".repeat(32)}, skips: []});
assert.equal(control("player").children[0].tagName, "video");
control("jellyfin-library").value = "b".repeat(32);
control("jellyfin-search").click();
assert.equal(sent.at(-1).library, "b".repeat(32));
reply({page: {items: [{id: "c".repeat(32), name: "Jellyfin movie"}], total: 1}});
assert.equal(control("jellyfin-results").children[0].children[0].textContent, "Jellyfin movie");
control("web-query").value = "circuits";
control("discover").click();
reply({discovery: {results: [{title: "Circuit video", channel: "Example", url: "https://youtu.be/abcdefghijk"}]}});
control("discovery").children[0].children[1].click();
assert.equal(sent.at(-1).action, "bookmark");
assert.equal(sent.at(-1).url, "https://youtu.be/abcdefghijk");
control("scan").click();
reply({candidates: [{candidate: "c0", name: "download.mp4"}]});
control("candidates").children[0].children[1].click();
assert.equal(sent.at(-1).candidates[0], "c0");
handlers["freya-view"]({detail: "chat"});
assert.equal(control("player").children.length, 0);
console.log("Media UI interaction checks passed");
