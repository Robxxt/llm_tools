/* Popup logic: scan the page, ask the local backend for verbatim CV values,
 * review (optional debug view), then fill on explicit click.
 * Provider + CV live in the backend settings; nothing is configured here.
 * No auto-fill, no auto-submit, no CAPTCHA interaction.
 */
(function () {
  "use strict";

  const ext = (typeof browser !== "undefined") ? browser
    : (typeof chrome !== "undefined") ? chrome : null;

  const DEFAULT_BACKEND = "http://127.0.0.1:5000";
  const $ = (id) => document.getElementById(id);

  function setStatus(msg, kind) {
    const el = $("status");
    el.textContent = msg || "";
    el.className = "status" + (kind ? " " + kind : "");
  }

  let backendUrlOverride = "";
  function backendUrl() {
    return (backendUrlOverride || DEFAULT_BACKEND).replace(/\/+$/, "");
  }

  // CV pulled from the backend settings so we can send it with each request
  // (works even if the running backend predates its own stored-CV fallback).
  let backendCvText = "";

  let scannedFields = [];
  let scannedSkipped = [];
  let suggestedValues = {};
  let selectedKeys = new Set();
  let debugOpen = false;
  let hasScanned = false;
  let lastScanTotal = 0;

  async function storeGet(keys) {
    if (!ext || !ext.storage) return {};
    return ext.storage.local.get(keys);
  }

  async function activeTab() {
    const tabs = await ext.tabs.query({ active: true, currentWindow: true });
    if (!tabs || !tabs[0]) throw new Error("No active tab");
    return tabs[0];
  }

  async function sendToTab(tabId, msg) {
    if (ext.tabs.sendMessage) return ext.tabs.sendMessage(tabId, msg);
    return new Promise((resolve, reject) => {
      chrome.tabs.sendMessage(tabId, msg, (resp) => {
        if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
        else resolve(resp);
      });
    });
  }

  async function readJson(resp) {
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || (`HTTP ${resp.status}`));
    return body;
  }

  // Pull provider + CV from the backend so the extension can work without
  // duplicating that configuration. Returns the settings or null on failure.
  async function syncSettings() {
    try {
      const s = await readJson(await fetch(`${backendUrl()}/api/settings`));
      backendCvText = (s.cv_text || "").trim();
      return s;
    } catch (e) {
      return null;
    }
  }

  async function checkBackend() {
    try {
      const [health, settings] = await Promise.all([
        readJson(await fetch(`${backendUrl()}/api/health`)),
        syncSettings(),
      ]);
      const configured = (typeof health.configured === "boolean")
        ? health.configured
        : !!(settings && settings.base_url && settings.model && backendCvText);
      if (configured) {
        setStatus("Synced with backend — ready to scan.", "ok");
      } else {
        setStatus("Backend not configured. Open settings (⚙) to add your model and CV.", "warn");
      }
      return configured;
    } catch (e) {
      setStatus(`Backend unreachable at ${backendUrl()}. Start it with: python app.py (in backend/).`, "err");
      return false;
    }
  }

  function updateSummary() {
    if (!hasScanned) {
      $("summary").textContent = "No page scanned yet.";
      return;
    }
    const n = Object.values(suggestedValues).filter(Boolean).length;
    $("summary").textContent = `${scannedFields.length} field(s) found · ${n} suggestion(s)`;
  }

  function isValidScan(resp) {
    return !!resp && typeof resp === "object" && Array.isArray(resp.fields);
  }

  // A tab that was open before the extension (re)loaded has no live content
  // script. Depending on the browser, tabs.sendMessage then either rejects or
  // resolves with undefined. Both look like "0 fields", so retry once after
  // injecting content.js on demand.
  async function scanTab(tabId) {
    let resp = null;
    try {
      resp = await sendToTab(tabId, { type: "CVFILL_SCAN" });
    } catch (firstErr) {
      resp = null;
    }
    if (isValidScan(resp)) return resp;
    if (!(ext.tabs && ext.tabs.executeScript)) {
      if (resp === null) throw new Error("content script unavailable — reload the page and try again");
      return resp;
    }
    await ext.tabs.executeScript(tabId, { file: "content.js" });
    resp = await sendToTab(tabId, { type: "CVFILL_SCAN" });
    if (!isValidScan(resp)) {
      throw new Error("no response from the page — reload the page and try again");
    }
    return resp;
  }

  function setFillEnabled() {
    const has = Object.entries(suggestedValues).some(
      ([k, v]) => v && selectedKeys.has(k)
    );
    $("fill").disabled = !has;
  }

  function renderFields() {
    const wrap = $("fields");
    wrap.innerHTML = "";
    if (!scannedFields.length) {
      wrap.textContent = hasScanned
        ? `Scanned ${lastScanTotal} form control(s); none are visible/fillable.`
        : "No fields scanned yet.";
      return;
    }
    const table = document.createElement("table");
    const head = document.createElement("tr");
    ["Use", "Field", "Value (verbatim from CV)"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      head.appendChild(th);
    });
    table.appendChild(head);
    scannedFields.forEach((f) => {
      const val = suggestedValues[f.key] || "";
      const tr = document.createElement("tr");

      const tdCheck = document.createElement("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = selectedKeys.has(f.key) && !!val;
      cb.dataset.key = f.key;
      cb.addEventListener("change", () => {
        if (cb.checked) selectedKeys.add(f.key);
        else selectedKeys.delete(f.key);
        setFillEnabled();
      });
      tdCheck.appendChild(cb);

      const tdLabel = document.createElement("td");
      tdLabel.textContent = f.label + (f.context ? ` · ${f.context}` : "");
      if (f.required) {
        const star = document.createElement("span");
        star.className = "req";
        star.textContent = " *";
        tdLabel.appendChild(star);
      }

      const tdVal = document.createElement("td");
      const multiline = f.type === "textarea" || /\n/.test(val) || val.length > 120;
      const inp = document.createElement(multiline ? "textarea" : "input");
      if (multiline) inp.rows = 3;
      else inp.type = "text";
      inp.value = val;
      inp.dataset.key = f.key;
      inp.addEventListener("input", () => {
        suggestedValues[f.key] = inp.value;
        if (inp.value.trim()) selectedKeys.add(f.key);
        else selectedKeys.delete(f.key);
        setFillEnabled();
      });
      tdVal.appendChild(inp);

      tr.append(tdCheck, tdLabel, tdVal);
      table.appendChild(tr);
    });
    wrap.appendChild(table);
  }

  function renderSkipped() {
    const el = $("skipped");
    el.innerHTML = "";
    if (!scannedSkipped.length) return;
    const div = document.createElement("div");
    div.className = "skip";
    const title = document.createElement("strong");
    title.textContent = "Not filled automatically:";
    div.appendChild(title);
    const ul = document.createElement("ul");
    scannedSkipped.forEach((s) => {
      const li = document.createElement("li");
      li.textContent = `${s.label} — ${s.reason}`;
      ul.appendChild(li);
    });
    div.appendChild(ul);
    el.appendChild(div);
  }

  function renderDropped(dropped) {
    const el = $("dropped");
    el.innerHTML = "";
    if (dropped && dropped.length) {
      const names = dropped.map((k) => {
        const f = scannedFields.find((x) => x.key === k);
        return f ? f.label : k;
      });
      const div = document.createElement("div");
      div.className = "dropped";
      div.textContent = `Left empty (not found verbatim in CV): ${names.join(", ")}`;
      el.appendChild(div);
    }
  }

  function setDebug(open) {
    debugOpen = open;
    $("debug").classList.toggle("hidden", !open);
    $("debugToggle").textContent = open ? "Hide fields" : "Show fields";
    $("debugToggle").setAttribute("aria-expanded", String(open));
    if (open) {
      renderFields();
      renderSkipped();
    }
  }

  async function suggest() {
    setStatus("Asking the model (verbatim-only)…");
    try {
      // Refresh the CV from the backend in case it changed since popup open.
      await syncSettings();
      const payload = { fields: scannedFields };
      if (backendCvText) payload.cv_text = backendCvText;
      const resp = await fetch(`${backendUrl()}/api/suggest-fill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await readJson(resp);
      let values = body.values || {};
      // Mirror the backend guard client-side when we have the CV locally.
      if (backendCvText && window.CVFillVerbatim) {
        values = window.CVFillVerbatim.validateValues(
          values, backendCvText, scannedFields
        ).cleaned;
      }
      suggestedValues = values;
      selectedKeys = new Set(
        Object.entries(suggestedValues)
          .filter(([, v]) => v)
          .map(([k]) => k)
      );
      renderDropped(body.dropped || []);
      updateSummary();
      setFillEnabled();
      if (debugOpen) renderFields();
      const n = selectedKeys.size;
      if (n === 0) {
        setStatus("No values found verbatim in your CV. Open “Show fields” to review or type values manually.", "err");
      } else {
        setStatus(`Found ${n} value(s) for ${scannedFields.length} field(s). Review, then Fill.`, "ok");
      }
    } catch (e) {
      setStatus(`Suggest failed: ${e.message}`, "err");
    }
  }

  async function onScan() {
    $("scan").disabled = true;
    $("fill").disabled = true;
    setStatus("Scanning visible form fields…");
    try {
      const tab = await activeTab();
      const resp = await scanTab(tab.id);
      scannedFields = (resp && resp.fields) || [];
      scannedSkipped = (resp && resp.skipped) || [];
      const total = (resp && resp.total) || 0;
      hasScanned = true;
      lastScanTotal = total;
      suggestedValues = {};
      selectedKeys = new Set();
      scannedFields.forEach((f) => { suggestedValues[f.key] = ""; });
      renderDropped([]);
      updateSummary();
      if (debugOpen) { renderFields(); renderSkipped(); }
      if (!scannedFields.length) {
        if (total > 0) {
          setStatus(`Found ${total} form control(s) but none is visible/fillable. Expand the application form (e.g. click Apply), then scan again.`, "err");
        } else {
          setStatus("No form fields found. Open the job application form on this page, then scan again.", "err");
        }
        return;
      }
      await suggest();
    } catch (e) {
      setStatus(`Scan failed: ${e.message}`, "err");
    } finally {
      $("scan").disabled = false;
    }
  }

  async function onFill() {
    const values = {};
    selectedKeys.forEach((k) => {
      const v = (suggestedValues[k] || "").trim();
      if (v) values[k] = v;
    });
    if (!Object.keys(values).length) {
      setStatus("No non-empty values are selected to fill.", "err");
      return;
    }
    setStatus(`Filling ${Object.keys(values).length} field(s)…`);
    try {
      const tab = await activeTab();
      const resp = await sendToTab(tab.id, { type: "CVFILL_FILL", values });
      setStatus(`Filled ${resp && resp.filled ? resp.filled : 0} field(s). Review and submit manually.`, "ok");
    } catch (e) {
      setStatus(`Fill failed: ${e.message}`, "err");
    }
  }

  function onOpenSettings() {
    const url = `${backendUrl()}/`;
    if (ext && ext.tabs && ext.tabs.create) ext.tabs.create({ url });
    else window.open(url, "_blank");
  }

  document.addEventListener("DOMContentLoaded", () => {
    storeGet(["backendUrl"])
      .then((s) => { if (s && s.backendUrl) backendUrlOverride = s.backendUrl; })
      .catch(() => {})
      .then(checkBackend);
    $("scan").addEventListener("click", onScan);
    $("fill").addEventListener("click", onFill);
    $("settings").addEventListener("click", onOpenSettings);
    $("debugToggle").addEventListener("click", () => setDebug(!debugOpen));
    updateSummary();
  });
})();
