const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

// Regression guard: the extension must never call websites to fill forms.
// It may only talk to the user's local Flask backend.

const ROOT = path.join(__dirname, "..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");

const SOURCES = ["content.js", "background.js", "popup.js", "verbatim.js"];
const LOCAL_URL = /^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?(\/|$)/;

const NETWORK_APIS = [
  ["fetch()", /\bfetch\s*\(/],
  ["XMLHttpRequest", /XMLHttpRequest/],
  ["navigator.sendBeacon", /sendBeacon/],
  ["WebSocket", /\bWebSocket\b/],
  ["EventSource", /\bEventSource\b/],
  ["navigator.send", /navigator\s*\.\s*send/],
];

describe("extension network safety", () => {
  it("content script and background make no network calls", () => {
    for (const file of ["content.js", "background.js"]) {
      const src = read(file);
      for (const [name, re] of NETWORK_APIS) {
        assert.equal(re.test(src), false, `${file} must not use ${name}`);
      }
    }
  });

  it("every popup fetch targets the configured local backend only", () => {
    const src = read("popup.js");
    const toBackend = src.match(/fetch\(\s*`\$\{backendUrl\(\)\}/g) || [];
    const total = src.match(/\bfetch\s*\(/g) || [];
    assert.ok(total.length > 0, "popup.js should fetch the backend");
    assert.equal(
      toBackend.length,
      total.length,
      "all popup.js fetches must go through backendUrl()"
    );
  });

  it("no source (JS or popup.html) references a non-local http(s) URL", () => {
    for (const file of [...SOURCES, "popup.html", "manifest.json"]) {
      const src = read(file);
      const urls = src.match(/https?:\/\/[^\s"'`)<>]+/g) || [];
      for (const url of urls) {
        assert.ok(LOCAL_URL.test(url), `${file} references non-local URL ${url}`);
      }
    }
  });

  it("manifest restricts host permissions to the local backend", () => {
    const manifest = JSON.parse(read("manifest.json"));
    const hosts = manifest.permissions.filter((p) => /https?:/.test(p));
    assert.deepEqual(
      hosts.slice().sort(),
      ["http://127.0.0.1:5000/*", "http://localhost:5000/*"]
    );
    for (const forbidden of ["webRequest", "declarativeNetRequest"]) {
      assert.equal(
        manifest.permissions.includes(forbidden),
        false,
        `manifest must not request ${forbidden}`
      );
    }
  });

  it("manifest CSP locks connect-src to the local backend", () => {
    const manifest = JSON.parse(read("manifest.json"));
    const csp = manifest.content_security_policy || "";
    assert.match(csp, /connect-src/);
    assert.match(csp, /http:\/\/127\.0\.0\.1:5000/);
    assert.match(csp, /http:\/\/localhost:5000/);
    assert.equal(/\*/.test(csp), false, "CSP must not allow a wildcard origin");
  });

  it("popup.html loads only local scripts", () => {
    const html = read("popup.html");
    const srcs = [...html.matchAll(/<script[^>]*\ssrc=["']([^"']+)["']/g)].map(
      (m) => m[1]
    );
    assert.ok(srcs.length >= 3);
    for (const src of srcs) {
      assert.equal(/^https?:|^\/\//.test(src), false, `remote script: ${src}`);
    }
  });
});
