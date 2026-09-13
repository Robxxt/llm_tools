/* Client-side mirror of the backend verbatim guard.
 * Loaded by popup.js for instant pre-checks; the backend re-validates
 * authoritatively so a compromised page cannot bypass it. */
(function (root) {
  function normalize(s) {
    return String(s == null ? "" : s).trim().toLowerCase().replace(/\s+/g, " ");
  }

  function isVerbatim(value, cvText) {
    if (value == null) return false;
    if (String(value).trim() === "") return true;
    const v = normalize(value);
    const cv = normalize(cvText || "");
    if (!v || !cv) return v === "";
    return cv.includes(v);
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
