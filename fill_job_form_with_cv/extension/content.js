/* Content script: passive field scanner + user-gesture-only filler.
 *
 * Ban-safe rules:
 * - Does NOTHING on page load. Only responds to SCAN/FILL messages that
 *   originate from the extension popup (i.e. an explicit user click).
 * - Never auto-submits forms, never clicks submit buttons, never touches
 *   CAPTCHA / hidden bot-check fields.
 * - Filling sets values field-by-field with normal focus/input/change
 *   events so sites see ordinary user-like edits, with a small delay
 *   between fields. The user reviews every value in the popup first.
 */
(function () {
  "use strict";

  const SKIP_TYPES = new Set([
    "hidden", "submit", "button", "image", "reset",
    "file", "password", "checkbox", "radio",
  ]);
  const CAPTCHA_RE = /(captcha|g-recaptcha|h-captcha|cf-turnstile)/i;
  // Reasons surfaced to the user as info rows (everything else is
  // skipped silently to avoid noise from hidden/disabled inputs).
  const SKIP_MESSAGE = {
    file: "File upload — attach manually, the extension never uploads files",
    password: "Password field — never filled",
    captcha: "CAPTCHA / bot-check — never touched",
  };

  // Null when fillable, otherwise a short reason code.
  // Supports both real DOM elements and plain test doubles.
  function skipReason(el) {
    const tag = String(el.tag || el.tagName || "").toUpperCase();
    const type = String(el.type || (tag === "TEXTAREA" ? "textarea" : tag === "SELECT" ? "select" : "text")).toLowerCase();
    if (!["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return "other";
    const hay = `${el.name || ""} ${el.id || ""} ${el.className || ""}`;
    if (CAPTCHA_RE.test(hay)) return "captcha";
    if (type === "file") return "file";
    if (type === "password") return "password";
    if (SKIP_TYPES.has(type)) return "other";
    if (el.disabled || el.hidden || el.readOnly) return "other";
    return null;
  }

  function isFillable(el) {
    return skipReason(el) === null;
  }

  function cleanLabel(s) {
    return String(s || "").replace(/\s+/g, " ").replace(/[\s*]+$/g, "").trim();
  }

  function resolveLabel(info) {
    // info: {ariaLabel, labelText, placeholder, name, id}
    if (info.ariaLabel && cleanLabel(info.ariaLabel)) return cleanLabel(info.ariaLabel);
    if (info.labelText && cleanLabel(info.labelText)) return cleanLabel(info.labelText);
    if (info.placeholder && cleanLabel(info.placeholder)) return cleanLabel(info.placeholder);
    if (info.name && cleanLabel(info.name)) return cleanLabel(info.name);
    if (info.id && cleanLabel(info.id)) return cleanLabel(info.id);
    return "Unlabeled field";
  }

  function describeField(info, index, labelOverride) {
    const label = labelOverride || resolveLabel(info);
    const desc = {
      key: `f${index}`,
      label,
      name: info.name || "",
      id: info.id || "",
      type: String(info.type || info.tag || "text").toLowerCase(),
    };
    if (info.placeholder) desc.placeholder = info.placeholder;
    if (info.context) desc.context = info.context;
    if (Array.isArray(info.options)) desc.options = info.options.slice(0, 200);
    if (info.required) desc.required = true;
    return desc;
  }

  function buildSuggestPayload(cvText, fields, provider) {
    if (!cvText || !String(cvText).trim()) throw new Error("cv_text is required");
    if (!Array.isArray(fields) || fields.length === 0) throw new Error("fields must be a non-empty list");
    const base_url = provider && provider.base_url ? String(provider.base_url).trim() : "";
    const model = provider && provider.model ? String(provider.model).trim() : "";
    if (!base_url || !model) throw new Error("provider.base_url and provider.model are required");
    return {
      cv_text: cvText,
      fields,
      provider: {
        base_url,
        model,
        api_key: provider.api_key ? String(provider.api_key) : "",
      },
    };
  }

  // ---- DOM helpers (browser only) ----

  function nodeText(node) {
    try {
      const t = node && node.textContent;
      return typeof t === "string" ? t : "";
    } catch (e) { return ""; }
  }

  function hasFormControl(node) {
    try {
      if (!node || !node.querySelector) return false;
      return !!node.querySelector("input, textarea, select, button");
    } catch (e) { return false; }
  }

  function siblingLabelText(sib) {
    if (!sib) return "";
    const tag = String(sib.tagName || "").toUpperCase();
    const raw = nodeText(sib).replace(/\s+/g, " ").trim();
    if (!raw || cleanLabel(raw).length === 0 || raw.length > 120) return "";
    if (tag === "LABEL") return raw;
    if (hasFormControl(sib)) return ""; // a container with its own fields
    return raw;
  }

  // Walk up a few levels looking for a label-ish previous sibling, e.g.
  // <label>Location*</label><div><input placeholder="Start typing…"></div>
  function findNearbyLabel(el) {
    try {
      let node = el;
      for (let depth = 0; depth < 3 && node; depth++) {
        const sib = node.previousElementSibling;
        const text = sib ? siblingLabelText(sib) : "";
        if (text) return text;
        node = node.parentElement;
      }
    } catch (e) { /* ignore */ }
    return "";
  }

  function labelTextForElement(el, doc) {
    try {
      const getAttr = (name) => {
        try { return el.getAttribute ? el.getAttribute(name) : null; }
        catch (e) { return null; }
      };
      const aria = getAttr("aria-label");
      if (aria && aria.trim()) return aria.trim();
      const labelledBy = getAttr("aria-labelledby");
      if (labelledBy && doc && doc.getElementById) {
        const ref = doc.getElementById(labelledBy.trim().split(/\s+/)[0]);
        if (ref && nodeText(ref).trim()) return nodeText(ref).trim().slice(0, 120);
      }
      if (el.id && doc && doc.querySelector && typeof CSS !== "undefined") {
        const lab = doc.querySelector(`label[for="${CSS.escape(el.id)}"]`);
        if (lab && nodeText(lab).trim()) return nodeText(lab).trim();
      }
      const closest = el.closest && el.closest("label");
      if (closest && nodeText(closest).trim()) return nodeText(closest).trim().slice(0, 120);
      return findNearbyLabel(el);
    } catch (e) { /* ignore */ }
    return "";
  }

  function isRequired(el, labelText) {
    try {
      if (el && el.required === true) return true;
      const aria = el && el.getAttribute ? el.getAttribute("aria-required") : null;
      if (aria === "true") return true;
    } catch (e) { /* ignore */ }
    return /\*/.test(String(labelText || ""));
  }

  function headingText(node) {
    if (!node) return "";
    const tag = String(node.tagName || "").toUpperCase();
    const isHeading = /^H[1-6]$/.test(tag) || tag === "LEGEND"
      || (node.getAttribute && node.getAttribute("role") === "heading");
    if (!isHeading) return "";
    return cleanLabel(nodeText(node)).slice(0, 120);
  }

  // Nearest heading/legend above the field. This lets the model know which
  // CV section (e.g. "Education" vs "Professional Experience") a generic
  // field like "Description" belongs to.
  function findSectionContext(el, doc) {
    try {
      const fs = el.closest && el.closest("fieldset");
      if (fs) {
        const t = headingText(fs.querySelector && fs.querySelector("legend"));
        if (t) return t;
      }
    } catch (e) { /* ignore */ }
    try {
      let node = el;
      for (let depth = 0; depth < 6 && node; depth++) {
        let sib = node.previousElementSibling;
        for (let hop = 0; sib && hop < 6; hop++) {
          const t = headingText(sib);
          if (t) return t;
          sib = sib.previousElementSibling;
        }
        node = node.parentElement;
      }
    } catch (e) { /* ignore */ }
    return "";
  }

  function toInfo(el, doc) {
    const tag = (el.tagName || "").toUpperCase();
    const labelText = labelTextForElement(el, doc);
    const info = {
      tag,
      type: tag === "SELECT" ? "select" : tag === "TEXTAREA" ? "textarea" : (el.type || "text"),
      name: el.name || "",
      id: el.id || "",
      className: el.className || "",
      placeholder: el.placeholder || "",
      ariaLabel: (el.getAttribute && el.getAttribute("aria-label")) || "",
      labelText,
      context: findSectionContext(el, doc),
      required: isRequired(el, labelText),
      disabled: !!el.disabled,
      hidden: el.type === "hidden" || (el.offsetParent === null && tag !== "SELECT"),
      readOnly: !!el.readOnly,
    };
    if (tag === "SELECT") {
      info.options = Array.from(el.options || []).map((o) => o.textContent.trim()).filter(Boolean).slice(0, 200);
    }
    return info;
  }

  // Gather controls from the document, same-origin iframes and open shadow
  // roots. Many job widgets render inside one of those, which plain
  // querySelectorAll on the top document would miss.
  function collectCandidates(root, out) {
    const acc = out || [];
    if (!root || !root.querySelectorAll) return acc;
    try {
      Array.from(root.querySelectorAll("input, textarea, select")).forEach((el) => acc.push(el));
    } catch (e) { /* ignore */ }
    let all = [];
    try { all = Array.from(root.querySelectorAll("*")); } catch (e) { all = []; }
    all.forEach((el) => {
      try {
        if (el.shadowRoot) collectCandidates(el.shadowRoot, acc);
        if (String(el.tagName || "").toUpperCase() === "IFRAME") {
          const inner = el.contentDocument;
          if (inner) collectCandidates(inner, acc);
        }
      } catch (e) { /* cross-origin iframe / detached node */ }
    });
    return acc;
  }

  function isHidden(el, root) {
    if (el.hidden || el.type === "hidden") return true;
    try {
      const view = (root && root.defaultView)
        || (typeof window !== "undefined" ? window : null);
      if (el.offsetParent === null) {
        const style = view && view.getComputedStyle ? view.getComputedStyle(el) : null;
        if (style && (style.display === "none" || style.visibility === "hidden")) return true;
      }
    } catch (e) { /* ignore */ }
    return false;
  }

  function asciiFold(s) {
    try {
      return String(s == null ? "" : s).normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "").trim().toLowerCase()
        .replace(/\s+/g, " ");
    } catch (e) {
      return String(s == null ? "" : s).trim().toLowerCase();
    }
  }

  var _LANG_ALIASES = {
    german: ["german", "deutsch"],
    english: ["english", "englisch"],
    spanish: ["spanish", "spanisch", "espanol"],
    french: ["french", "franzosisch", "francais"],
    romanian: ["romanian", "rumanisch", "romana"],
    italian: ["italian", "italienisch"],
    portuguese: ["portuguese", "portugiesisch"],
    dutch: ["dutch", "niederlandisch"],
    polish: ["polish", "polnisch"],
    czech: ["czech", "tschechisch"],
    slovak: ["slovak", "slowakisch"],
    hungarian: ["hungarian", "ungarisch"],
    turkish: ["turkish", "turkisch"],
    arabic: ["arabic", "arabisch"],
    chinese: ["chinese", "chinesisch"],
    japanese: ["japanese", "japanisch"],
    russian: ["russian", "russisch"],
    ukrainian: ["ukrainian", "ukrainisch"],
  };
  var _ALIAS_TO_CANON = {};
  Object.keys(_LANG_ALIASES).forEach(function (canon) {
    _LANG_ALIASES[canon].forEach(function (a) { _ALIAS_TO_CANON[a] = canon; });
  });

  function canonicalLanguageShort(text) {
    const n = asciiFold(String(text || "").replace(/\(.*?\)/g, ""));
    if (!n) return "";
    const aliases = Object.keys(_ALIAS_TO_CANON).sort((a, b) => b.length - a.length);
    for (const alias of aliases) {
      const esc = alias.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      if (new RegExp("(^|[^a-z])" + esc + "([^a-z]|$)").test(n)) {
        return _ALIAS_TO_CANON[alias];
      }
    }
    return "";
  }

  // Value -> <option> matcher. The backend already maps CV CEFR levels to
  // exact option text, but the user may hand-type a short form ("B2",
  // "Deutsch") in the debug view, so resolve those deterministically too.
  function findBestOption(selectEl, value) {
    const opts = Array.from((selectEl && selectEl.options) || []);
    if (!opts.length) return null;
    const want = String(value == null ? "" : value).trim();
    if (!want) return null;
    let m = opts.find((o) => o.text === want || o.value === want);
    if (m) return m;
    const folded = asciiFold(want);
    m = opts.find((o) => asciiFold(o.text) === folded || asciiFold(o.value) === folded);
    if (m) return m;
    // Bare CEFR code ("b2") or level word -> option containing that code.
    const codeMatch = folded.match(/^([abc][12])$/);
    if (codeMatch) {
      const code = codeMatch[1];
      m = opts.find((o) => new RegExp("\\b" + code + "\\b", "i").test(o.text || ""));
      if (m) return m;
    }
    if (/^(native|muttersprache|nativ)$/.test(folded)) {
      m = opts.find((o) => /(native|muttersprache|nativ)/i.test(o.text || ""));
      if (m) return m;
    }
    if (/^(keine|kein|none|no knowledge)$/.test(folded)) {
      m = opts.find((o) => /(keine|kein|none|no knowledge)/i.test(o.text || ""));
      if (m) return m;
    }
    // Language synonym ("German" -> "Deutsch").
    const canon = canonicalLanguageShort(want);
    if (canon) {
      m = opts.find((o) => canonicalLanguageShort(o.text) === canon
        || canonicalLanguageShort(o.value) === canon);
      if (m) return m;
    }
    // Normalized substring ("Fortgeschritten" -> "B2 (Fortgeschritten)").
    if (folded.length >= 3) {
      m = opts.find((o) => {
        const t = asciiFold(o.text);
        if (!t || t.startsWith("--") || t === "please select") return false;
        return t.includes(folded) || folded.includes(t);
      });
      if (m) return m;
    }
    return null;
  }

  function scanPage(doc) {
    const root = doc || (typeof document !== "undefined" ? document : null);
    if (!root) return { fields: [], skipped: [], total: 0 };
    const nodes = collectCandidates(root);
    const fields = [];
    const skipped = [];
    let idx = 0;
    nodes.forEach((el) => {
      if (!el || !el.tagName) return;
      // Searchable dropdown widgets (like the "Deutsch-Kenntnisse" one with
      // a search box) often hide the native <select> with display:none and
      // render a custom UI. Filling the hidden <select> + firing change
      // still works, so keep hidden selects that actually have options.
      if (isHidden(el, root)) {
        const tag = String(el.tagName || "").toUpperCase();
        if (tag !== "SELECT") return;
        try {
          if (!el.options || el.options.length === 0) return;
        } catch (e) { return; }
      }
      const info = toInfo(el, root);
      const reason = skipReason(info);
      if (reason !== null) {
        if (SKIP_MESSAGE[reason]) {
          skipped.push({ label: resolveLabel(info), reason: SKIP_MESSAGE[reason] });
        }
        return;
      }
      fields.push(describeField(info, idx, resolveLabel(info)));
      try { el.setAttribute("data-cvfill-key", `f${idx}`); } catch (e) { /* ignore */ }
      idx += 1;
    });
    return { fields, skipped, total: nodes.length };
  }

  function setNativeValue(el, value) {
    const protoTag = el.tagName === "TEXTAREA" ? window.HTMLTextAreaElement.prototype
      : el.tagName === "SELECT" ? window.HTMLSelectElement.prototype
      : window.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(protoTag, "value")?.set
      || Object.getOwnPropertyDescriptor(window.HTMLElement.prototype, "value")?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function fillPage(values, doc, delayMs = 120) {
    const root = doc || (typeof document !== "undefined" ? document : null);
    if (!root) return { filled: 0 };
    let filled = 0;
    for (const [key, value] of Object.entries(values || {})) {
      if (!value) continue;
      const el = root.querySelector(`[data-cvfill-key="${key}"]`);
      if (!el) continue;
      try {
        el.focus({ preventScroll: false });
        if (el.tagName === "SELECT") {
          const match = findBestOption(el, value);
          if (!match) continue; // never invent an option
          el.value = match.value;
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
        } else {
          setNativeValue(el, value);
        }
        filled += 1;
      } catch (e) { /* skip field, continue */ }
      await new Promise((r) => setTimeout(r, delayMs));
    }
    return { filled };
  }

  // Message handling: only in a real extension context.
  try {
    const ext = (typeof browser !== "undefined" && browser.runtime) ? browser
      : (typeof chrome !== "undefined" && chrome.runtime) ? chrome : null;
    if (ext && ext.runtime && ext.runtime.onMessage) {
      ext.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
        if (!msg || typeof msg.type !== "string") return undefined;
        if (msg.type === "CVFILL_SCAN") {
          const { fields, skipped } = scanPage();
          sendResponse({ fields, skipped });
          return true;
        }
        if (msg.type === "CVFILL_FILL") {
          fillPage(msg.values || {}).then((res) => sendResponse(res));
          return true; // async response
        }
        return undefined;
      });
    }
  } catch (e) { /* non-browser (tests) */ }

  if (typeof module !== "undefined" && module.exports) {
    module.exports = { isFillable, skipReason, resolveLabel, describeField, buildSuggestPayload, labelTextForElement, isRequired, findSectionContext, collectCandidates, scanPage, fillPage, findBestOption, canonicalLanguageShort };
  } else if (typeof window !== "undefined") {
    window.CVFillContent = { isFillable, skipReason, resolveLabel, describeField, buildSuggestPayload, labelTextForElement, isRequired, findSectionContext, collectCandidates, scanPage, fillPage, findBestOption, canonicalLanguageShort };
  }
})();
