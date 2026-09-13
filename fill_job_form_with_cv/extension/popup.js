/* Popup logic: settings + CV + scan -> suggest (via Flask backend) ->
 * user-reviewed preview -> fill checked fields on explicit click.
 * No auto-fill, no auto-submit, no CAPTCHA interaction.
 */
(function () {
  "use strict";

  const ext = (typeof browser !== "undefined") ? browser
    : (typeof chrome !== "undefined") ? chrome : null;

  const $ = (id) => document.getElementById(id);
  const statusEl = () => $("status");
  function setStatus(msg, kind) {
    const el = statusEl();
    el.textContent = msg || "";
    el.className = kind || "";
  }

  const OLLAMA_DEFAULTS = { base_url: "http://localhost:11434/v1", model: "llama3.1" };

  let scannedFields = [];
  let scannedSkipped = [];
  let suggestedValues = {};
  // Keys the user typed into by hand. These are the user's own data, not
  // model output, so they bypass the verbatim guard on Fill.
  let dirtyKeys = new Set();

  async function storeGet(keys) {
    if (!ext || !ext.storage) return {};
    return ext.storage.local.get(keys);
  }
  async function storeSet(obj) {
    if (!ext || !ext.storage) return;
    return ext.storage.local.set(obj);
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

  function currentProvider() {
    return {
      base_url: $("baseUrl").value.trim(),
      model: $("model").value.trim(),
      api_key: $("apiKey").value,
    };
  }

  function backendUrl() {
    return $("backendUrl").value.trim().replace(/\/+$/, "") || "http://127.0.0.1:5000";
  }

  async function readJson(resp) {
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(body.error || (`HTTP ${resp.status}`));
    return body;
  }

  function renderPreview() {
    const wrap = $("fields");
    wrap.innerHTML = "";
    if (!scannedFields.length) {
      wrap.textContent = "No fields scanned yet. Click “Scan this page”.";
      return;
    }
    const table = document.createElement("table");
    const head = document.createElement("tr");
    ["Use", "Field", "Proposed value (verbatim from CV)"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      head.appendChild(th);
    });
    table.appendChild(head);
    scannedFields.forEach((f) => {
      const tr = document.createElement("tr");
      const tdCheck = document.createElement("td");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = !!(suggestedValues[f.key] || "");
      cb.dataset.key = f.key;
      tdCheck.appendChild(cb);
      const tdLabel = document.createElement("td");
      tdLabel.textContent = f.label + (f.type === "select" && f.options ? ` [${f.options.join(" / ")}]` : "");
      if (f.required) {
        const star = document.createElement("span");
        star.className = "req";
        star.textContent = " *";
        tdLabel.appendChild(star);
      }
      const tdVal = document.createElement("td");
      const inp = document.createElement("input");
      inp.type = "text";
      inp.value = suggestedValues[f.key] || "";
      inp.dataset.key = f.key;
      inp.addEventListener("input", () => {
        suggestedValues[f.key] = inp.value;
        dirtyKeys.add(f.key);
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
      const div = document.createElement("div");
      div.className = "dropped";
      div.textContent = `Left empty (not found verbatim in CV, dropped by guard): ${dropped.join(", ")}`;
      el.appendChild(div);
    }
  }

  async function loadStored() {
    const s = await storeGet(["backendUrl", "preset", "base_url", "model", "api_key", "cvText"]);
    if (s.backendUrl) $("backendUrl").value = s.backendUrl;
    if (s.preset) $("preset").value = s.preset;
    if (s.base_url) $("baseUrl").value = s.base_url;
    if (s.model) $("model").value = s.model;
    if (s.api_key) $("apiKey").value = s.api_key;
    if (s.cvText) $("cvText").value = s.cvText;
  }

  function onPresetChange() {
    if ($("preset").value === "ollama") {
      $("baseUrl").value = OLLAMA_DEFAULTS.base_url;
      if (!$("model").value || $("model").value === "") $("model").value = OLLAMA_DEFAULTS.model;
    }
  }

  async function onSave() {
    await storeSet({
      backendUrl: backendUrl(),
      preset: $("preset").value,
      base_url: $("baseUrl").value.trim(),
      model: $("model").value.trim(),
      api_key: $("apiKey").value,
      cvText: $("cvText").value,
    });
    setStatus("Saved.", "ok");
  }

  async function onUploadPdf() {
    const file = $("pdfFile").files[0];
    if (!file) { setStatus("Choose a PDF first.", "err"); return; }
    setStatus("Parsing PDF…");
    try {
      const form = new FormData();
      form.append("file", file);
      const resp = await fetch(`${backendUrl()}/api/parse-pdf`, { method: "POST", body: form });
      const body = await readJson(resp);
      $("cvText").value = body.text || "";
      await storeSet({ cvText: $("cvText").value });
      if (!$("cvText").value.trim()) {
        setStatus("PDF parsed but no text found (scanned/image PDF?). Paste the CV text manually.", "err");
      } else {
        setStatus(`PDF parsed (${$("cvText").value.length} chars). Review it, then Suggest.`, "ok");
      }
    } catch (e) {
      setStatus(`PDF parse failed: ${e.message}. Is the Flask backend running at ${backendUrl()}?`, "err");
    }
  }

  async function onTestConnection() {
    setStatus("Testing backend + model (cold Ollama models can take a while)…");
    try {
      await readJson(await fetch(`${backendUrl()}/api/health`));
    } catch (e) {
      setStatus(`Backend unreachable at ${backendUrl()}: ${e.message}. Start it with: python app.py (in backend/, venv active).`, "err");
      return;
    }
    try {
      const body = await readJson(await fetch(`${backendUrl()}/api/test-provider`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(currentProvider()),
      }));
      setStatus(`Backend OK, model OK (reply: ${JSON.stringify(body.reply)}).`, "ok");
    } catch (e) {
      setStatus(`Backend OK, but model failed: ${e.message}. Is Ollama running? Did you pull the model (${$("model").value.trim() || "?"}: try “ollama pull …”)?`, "err");
    }
  }

  async function onLoadBackend() {
    setStatus("Loading settings from backend…");
    try {
      const s = await readJson(await fetch(`${backendUrl()}/api/settings`));
      if (s.base_url) $("baseUrl").value = s.base_url;
      if (s.model) $("model").value = s.model;
      if (s.api_key) $("apiKey").value = s.api_key;
      if (s.cv_text) $("cvText").value = s.cv_text;
      await onSave();
      setStatus("Settings loaded from backend and saved locally.", "ok");
    } catch (e) {
      setStatus(`Could not load backend settings: ${e.message}.`, "err");
    }
  }

  async function onScan() {
    setStatus("Scanning visible form fields…");
    try {
      const tab = await activeTab();
      const resp = await sendToTab(tab.id, { type: "CVFILL_SCAN" });
      scannedFields = (resp && resp.fields) || [];
      scannedSkipped = (resp && resp.skipped) || [];
      suggestedValues = {};
      dirtyKeys = new Set();
      scannedFields.forEach((f) => { suggestedValues[f.key] = ""; });
      renderPreview();
      renderSkipped();
      renderDropped([]);
      setStatus(`Found ${scannedFields.length} fillable field(s)` +
        (scannedSkipped.length ? `, ${scannedSkipped.length} left for manual handling (see below).` : "."), "ok");
    } catch (e) {
      setStatus(`Scan failed: ${e.message}`, "err");
    }
  }

  async function onSuggest() {
    const cvText = $("cvText").value;
    if (!cvText.trim()) { setStatus("Paste your CV or parse a PDF first.", "err"); return; }
    if (!scannedFields.length) { setStatus("Scan the page first.", "err"); return; }
    setStatus("Asking model (verbatim-only)…");
    try {
      // content.js exposes the same payload builder used in unit tests.
      const payload = window.CVFillContent.buildSuggestPayload(cvText, scannedFields, currentProvider());
      const resp = await fetch(`${backendUrl()}/api/suggest-fill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await readJson(resp);
      // Client-side mirror of the guard (backend already enforced it).
      const guard = window.CVFillVerbatim.validateValues(body.values, cvText, scannedFields);
      suggestedValues = guard.cleaned;
      dirtyKeys = new Set();
      renderPreview();
      const dropped = body.dropped && body.dropped.length ? body.dropped : guard.dropped;
      renderDropped(dropped);
      const n = Object.values(suggestedValues).filter(Boolean).length;
      if (n === 0 && dropped.length >= scannedFields.length) {
        setStatus("The model found nothing verbatim in your CV for these fields (all left empty). Check the CV text is loaded, then Fill what you type manually.", "err");
      } else if (n === 0) {
        setStatus("The model returned no values. Check the model name and use “Test connection”.", "err");
      } else {
        setStatus(`Got ${n}/${scannedFields.length} value(s) found verbatim in your CV. Review, edit, then Fill.`, "ok");
      }
    } catch (e) {
      setStatus(`Suggest failed: ${e.message}. Use “Test connection” to diagnose.`, "err");
    }
  }

  async function onFill() {
    const boxes = Array.from(document.querySelectorAll('#fields input[type="checkbox"]'));
    const inputs = Array.from(document.querySelectorAll('#fields input[type="text"]'));
    const byKey = {};
    inputs.forEach((i) => { byKey[i.dataset.key] = i.value; });
    const checked = boxes.filter((cb) => cb.checked).map((cb) => cb.dataset.key);
    if (!checked.length) { setStatus("Tick the checkboxes of the fields you want to fill.", "err"); return; }
    const withText = checked.filter((k) => (byKey[k] || "").trim());
    if (!withText.length) {
      setStatus("Checked rows are all empty — click “Suggest from CV” or type values into the boxes first.", "err");
      return;
    }
    // Model-suggested (untouched) values must still pass the verbatim guard.
    // Values you typed by hand are your own data and fill as-is.
    const manual = {};
    const suggested = {};
    withText.forEach((k) => {
      const v = byKey[k].trim();
      if (dirtyKeys.has(k)) manual[k] = v;
      else suggested[k] = v;
    });
    const guard = window.CVFillVerbatim.validateValues(suggested, $("cvText").value, scannedFields);
    if (guard.dropped.length) {
      const names = guard.dropped.map((k) => {
        const f = scannedFields.find((x) => x.key === k);
        return f ? f.label : k;
      });
      setStatus(`Blocked ${guard.dropped.length} suggested value(s) not found verbatim in CV (${names.join(", ")}). Edit them by hand to fill anyway.`, "err");
      suggestedValues = { ...suggestedValues, ...guard.cleaned, ...manual };
      renderPreview();
      if (!Object.keys(manual).length) return;
    }
    const values = { ...guard.cleaned, ...manual };
    Object.keys(values).forEach((k) => { if (!values[k]) delete values[k]; });
    if (!Object.keys(values).length) { setStatus("Nothing left to fill after the verbatim check.", "err"); return; }
    setStatus(`Filling ${Object.keys(values).length} field(s)…`);
    try {
      const tab = await activeTab();
      const resp = await sendToTab(tab.id, { type: "CVFILL_FILL", values });
      setStatus(`Filled ${resp && resp.filled ? resp.filled : 0} field(s). Please review and submit manually.`, "ok");
    } catch (e) {
      setStatus(`Fill failed: ${e.message}`, "err");
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    loadStored().catch(() => {});
    $("preset").addEventListener("change", onPresetChange);
    $("saveAll").addEventListener("click", onSave);
    $("uploadPdf").addEventListener("click", onUploadPdf);
    $("testConn").addEventListener("click", onTestConnection);
    $("loadBackend").addEventListener("click", onLoadBackend);
    $("scan").addEventListener("click", onScan);
    $("suggest").addEventListener("click", onSuggest);
    $("fill").addEventListener("click", onFill);
    renderPreview();
  });
})();
