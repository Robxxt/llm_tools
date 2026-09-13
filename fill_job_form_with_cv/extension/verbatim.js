/* Client-side mirror of the backend verbatim guard.
 * Loaded by popup.js for instant pre-checks; the backend re-validates
 * authoritatively so a compromised page cannot bypass it. */
(function (root) {
  function normalize(s) {
    return String(s == null ? "" : s).trim().toLowerCase().replace(/\s+/g, " ");
  }

  function isVerbatim(value, cvText) {
    if (value == null) return false;
    const text = String(value).trim();
    if (text === "") return true;
    const cv = normalize(cvText || "");
    const v = normalize(text);
    if (!cv) return v === "";
    if (cv.includes(v)) return true;
    // Multi-line / bulleted block (experience or education description):
    // accept when every non-empty line appears verbatim, in order.
    const segments = text
      .split(/[\r\n]+|\s+[-•*]\s+/)
      .map((s) => s.replace(/^[-•*]\s+/, "").trim())
      .filter(Boolean);
    if (segments.length <= 1) return false;
    let cursor = 0;
    for (const segment of segments) {
      const seg = normalize(segment);
      if (!seg) continue;
      const idx = cv.indexOf(seg, cursor);
      if (idx === -1) return false;
      cursor = idx + seg.length;
    }
    return true;
  }

  function validateValues(rawValues, cvText, fields) {
    const byKey = {};
    (fields || []).forEach((f) => {
      if (f && f.key) byKey[f.key] = f;
    });
    const cleaned = {};
    const dropped = [];
    Object.keys(byKey).forEach((key) => {
      let value = rawValues ? rawValues[key] : "";
      value = value == null ? "" : String(value).trim();
      const field = byKey[key] || {};
      const options = field.options || [];
      if (value === "") {
        cleaned[key] = "";
        return;
      }
      if (options.length && !options.includes(value)) {
        cleaned[key] = "";
        dropped.push(key);
        return;
      }
      if (!isVerbatim(value, cvText)) {
        cleaned[key] = "";
        dropped.push(key);
        return;
      }
      cleaned[key] = value;
    });
    return { cleaned, dropped };
  }

  const api = { isVerbatim, validateValues };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.CVFillVerbatim = api;
  }
})(typeof self !== "undefined" ? self : this);
