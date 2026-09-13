/* Background script: set sane defaults on install. No page access,
 * no network calls, no auto-fill — everything happens on user click. */
(function () {
  const ext = (typeof browser !== "undefined") ? browser
    : (typeof chrome !== "undefined") ? chrome : null;
  if (!ext || !ext.runtime || !ext.storage) return;

  const DEFAULTS = {
    backendUrl: "http://127.0.0.1:5000",
    preset: "ollama",
    base_url: "http://localhost:11434/v1",
    model: "llama3.1",
    api_key: "",
    cvText: "",
  };

  async function ensureDefaults() {
    try {
      const cur = await ext.storage.local.get(Object.keys(DEFAULTS));
      const missing = {};
      for (const [k, v] of Object.entries(DEFAULTS)) {
        if (cur[k] === undefined) missing[k] = v;
      }
      if (Object.keys(missing).length) await ext.storage.local.set(missing);
    } catch (e) { /* storage unavailable */ }
  }

  if (ext.runtime.onInstalled) {
    ext.runtime.onInstalled.addListener(ensureDefaults);
  }
  ensureDefaults();
})();
