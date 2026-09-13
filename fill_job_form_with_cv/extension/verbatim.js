/* Client-side mirror of the backend verbatim guard.
 * Loaded by popup.js for instant pre-checks; the backend re-validates
 * authoritatively so a compromised page cannot bypass it.
 *
 * Language-proficiency selects are the controlled exception (mirrors
 * backend/app.py): a composite option such as "B2 (Fortgeschritten)" can
 * never be verbatim when the CV only states "B2", so the guard maps
 * CV CEFR -> exact option instead of blanking it. Mapping only applies to
 * fields/options that look like language levels (or language names) and
 * the target must come from CV evidence for THAT language. */
(function (root) {
  function normalize(s) {
    return String(s == null ? "" : s).trim().toLowerCase().replace(/\s+/g, " ");
  }

  function asciiFold(s) {
    return String(s == null ? "" : s)
      .normalize("NFD")
      .replace(/[\u0300-\u036f]/g, "")
      .trim()
      .toLowerCase()
      .replace(/\s+/g, " ");
  }

  var CEFR_ORDER = { A1: 1, A2: 2, B1: 3, B2: 4, C1: 5, C2: 6, NATIVE: 7 };
  var CEFR_RE = /\b([ABC][12])\b/i;
  var CEFR_ALL_RE = /\b([ABC][12])\b/gi;
  var NATIVE_RE = /\b(natives?|muttersprache|mother\s*tongue|bilingual|nativ)\b/i;
  var NONE_RE = /\b(keine[rsmn]?|kein\b|no\s*knowledge|no\s*skills?|none\b)/i;

  var LANGUAGE_ALIASES = {
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
  var ALIAS_TO_CANONICAL = {};
  Object.keys(LANGUAGE_ALIASES).forEach(function (canon) {
    LANGUAGE_ALIASES[canon].forEach(function (a) {
      ALIAS_TO_CANONICAL[a] = canon;
    });
  });
  var ALIASES_BY_LENGTH = Object.keys(ALIAS_TO_CANONICAL).sort(function (a, b) {
    return b.length - a.length;
  });

  function escRe(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function isPlaceholderOption(opt) {
    var n = asciiFold(opt);
    if (!n) return true;
    if (n === "--" || n === "-" || n === "select" || n === "please select" ||
        n === "bitte wahlen" || n === "bitte waehlen" || n === "auswahlen" ||
        n === "wahl") return true;
    return n.indexOf("--") === 0 || n.indexOf("please select") !== -1 ||
      n.indexOf("bitte wahlen") !== -1;
  }

  function optionLevelRange(option) {
    var text = String(option == null ? "" : option);
    var codes = [];
    CEFR_ALL_RE.lastIndex = 0;
    var m = CEFR_ALL_RE.exec(text);
    while (m) {
      codes.push(m[1].toUpperCase());
      m = CEFR_ALL_RE.exec(text);
    }
    if (codes.length) {
      var scores = codes
        .filter(function (c) { return CEFR_ORDER[c]; })
        .map(function (c) { return CEFR_ORDER[c]; })
        .sort(function (a, b) { return a - b; });
      if (scores.length) return [scores[0], scores[scores.length - 1]];
    }
    var n = asciiFold(text);
    if (!n || isPlaceholderOption(text)) return null;
    if (NONE_RE.test(n)) return [0, 0];
    if (NATIVE_RE.test(n)) return [7, 7];
    if (/verhandlungssicher|business fluent|near native/.test(n)) return [6, 6];
    if (/fliessend|fluent|full professional/.test(n)) return [5, 5];
    if (/fortgeschritten|upper intermediate|independent|professional|beruf/.test(n)) return [4, 4];
    if (/(^|[^a-z])b1([^a-z]|$)|intermediate|mittelstufe|conversational/.test(n)) {
      if (n.indexOf("upper") !== -1) return [4, 4];
      return [3, 3];
    }
    if (/elementary|pre intermediate|pre-intermediate/.test(n)) return [2, 2];
    if (/grundkenntnisse|grundlagen|basic|beginner|anfanger/.test(n)) return [1, 1];
    var bare = n.match(/\b([abc])\s*([12])\b/);
    if (bare) {
      var code = (bare[1] + bare[2]).toUpperCase();
      if (CEFR_ORDER[code]) return [CEFR_ORDER[code], CEFR_ORDER[code]];
    }
    return null;
  }

  function looksLikeProficiencyOptions(options) {
    var scorable = 0;
    var hasCode = false;
    (options || []).forEach(function (o) {
      if (isPlaceholderOption(o)) return;
      if (CEFR_RE.test(String(o || ""))) {
        CEFR_RE.lastIndex = 0;
        hasCode = true;
        scorable += 1;
      } else if (optionLevelRange(o) !== null) {
        scorable += 1;
      }
    });
    return scorable >= 2 || (hasCode && scorable >= 2);
  }

  function fieldText(field) {
    field = field || {};
    return [field.label, field.name, field.context]
      .map(function (v) { return v || ""; })
      .join(" ");
  }

  function isLanguageLevelField(field, options) {
    var opts = options || [];
    if (!opts.length) return false;
    if (looksLikeProficiencyOptions(opts)) return true;
    var n = asciiFold(fieldText(field));
    if (!n) return false;
    var keywords = ["sprach", "kenntnis", "proficiency", "fluency", "fluent",
      "niveau", "cefr", "language", "langue", "idioma", "level", "stufe",
      "deutsch", "german", "englisch", "english", "spanisch", "spanish",
      "french", "roman", "italian"];
    if (keywords.some(function (k) { return n.indexOf(k) !== -1; })) {
      return opts.some(function (o) {
        return !isPlaceholderOption(o) && optionLevelRange(o) !== null;
      });
    }
    return false;
  }

  function canonicalLanguage(text) {
    var n = asciiFold(text);
    if (!n) return "";
    var base = n.replace(/\(.*?\)/g, "").trim();
    var candidates = [n, base];
    for (var i = 0; i < candidates.length; i++) {
      for (var j = 0; j < ALIASES_BY_LENGTH.length; j++) {
        var alias = ALIASES_BY_LENGTH[j];
        var re = new RegExp("(^|[^a-z])" + escRe(alias) + "([^a-z]|$)");
        if (re.test(candidates[i])) return ALIAS_TO_CANONICAL[alias];
      }
    }
    return "";
  }

  function isLanguageNameField(field, options) {
    var opts = (options || []).filter(function (o) { return !isPlaceholderOption(o); });
    if (opts.length < 2) return false;
    var hits = opts.filter(function (o) { return canonicalLanguage(o); }).length;
    if (hits >= 2 && hits / opts.length >= 0.5) return true;
    var n = asciiFold(fieldText(field));
    if (/(language|sprache|langue|idioma|muttersprache)/.test(n)) return hits >= 1;
    return false;
  }

  function fieldLanguage(field) {
    return canonicalLanguage(fieldText(field));
  }

  function languageMentioned(canonical, cvText) {
    var n = asciiFold(cvText || "");
    var aliases = LANGUAGE_ALIASES[canonical] || [canonical];
    return aliases.some(function (a) {
      return new RegExp("(^|[^a-z])" + escRe(a) + "([^a-z]|$)").test(n);
    });
  }

  function pairSegment(seg, result) {
    CEFR_ALL_RE.lastIndex = 0;
    var codeSpans = [];
    var cm = CEFR_ALL_RE.exec(seg);
    while (cm) {
      codeSpans.push({ code: cm[1].toUpperCase(), pos: cm.index });
      cm = CEFR_ALL_RE.exec(seg);
    }
    var nativeSpans = [];
    var nm = new RegExp(NATIVE_RE.source, "gi");
    var nmt = nm.exec(seg);
    while (nmt) {
      nativeSpans.push(nmt.index);
      nmt = nm.exec(seg);
    }
    if (!codeSpans.length && !nativeSpans.length) return;
    ALIASES_BY_LENGTH.forEach(function (alias) {
      var canon = ALIAS_TO_CANONICAL[alias];
      if (result[canon]) return;
      var am = seg.match(new RegExp("(^|[^a-z])" + escRe(alias) + "([^a-z]|$)"));
      if (!am) return;
      var pos = am.index;
      var best = null;
      var bestDist = null;
      codeSpans.forEach(function (cs) {
        var d = Math.abs(cs.pos - pos);
        if (bestDist === null || d < bestDist) {
          bestDist = d;
          best = cs.code;
        }
      });
      var nativeDist = nativeSpans.length
        ? Math.min.apply(null, nativeSpans.map(function (p) { return Math.abs(p - pos); }))
        : null;
      if (best !== null && (nativeDist === null || bestDist <= nativeDist)) {
        result[canon] = best;
      } else if (nativeDist !== null) {
        result[canon] = "NATIVE";
      }
    });
  }

  function parseCvLanguages(cvText) {
    var result = {};
    var lines = String(cvText || "").split(/[\r\n]+/)
      .map(function (l) { return l.trim(); })
      .filter(Boolean);
    // 1. inline: language + level on the same line
    lines.forEach(function (line) {
      var folded = asciiFold(line);
      if (!CEFR_RE.test(folded) && !NATIVE_RE.test(folded)) {
        CEFR_RE.lastIndex = 0;
        return;
      }
      CEFR_RE.lastIndex = 0;
      var segments = folded.split(/[;,|/]/);
      if (segments.length > 1) {
        segments.forEach(function (seg) { pairSegment(seg, result); });
        pairSegment(folded, result);
      } else {
        pairSegment(folded, result);
      }
    });
    function isLevelOnly(line) {
      var n = asciiFold(String(line).replace(/^[-•*]\s+/, "").trim().replace(/^[(\[]|[)\].:]+$/g, ""));
      if (["a1", "a2", "b1", "b2", "c1", "c2"].indexOf(n) !== -1) return n.toUpperCase();
      if (["native", "muttersprache", "nativ"].indexOf(n) !== -1) return "NATIVE";
      return null;
    }
    function isLanguageOnly(line) {
      var cleaned = String(line).replace(/^[-•*]\s+/, "").trim();
      var folded = asciiFold(cleaned.replace(/\(.*?\)/g, "").trim().replace(/^[(\[]|[)\].:]+$/g, ""));
      if (ALIAS_TO_CANONICAL[folded]) return ALIAS_TO_CANONICAL[folded];
      var first = (folded.split(" ")[0] || "");
      if (ALIAS_TO_CANONICAL[first]) {
        var rest = folded.slice(first.length).trim();
        if (!rest || /^[a-z]{1,3}$/.test(rest)) return ALIAS_TO_CANONICAL[first];
      }
      return null;
    }
    // 2. column-style: standalone levels block + standalone languages block
    var levelSeq = [];
    var langSeq = [];
    lines.forEach(function (line) {
      var code = isLevelOnly(line);
      if (code) {
        levelSeq.push(code);
        return;
      }
      var lang = isLanguageOnly(line);
      if (lang) langSeq.push(lang);
    });
    var deduped = [];
    langSeq.forEach(function (l) {
      if (deduped.indexOf(l) === -1) deduped.push(l);
    });
    if (levelSeq.length && deduped.length === levelSeq.length) {
      deduped.forEach(function (lang, i) {
        if (!result[lang]) result[lang] = levelSeq[i];
      });
    }
    // 3. proximity fallback
    if (lines.length) {
      var full = asciiFold(cvText || "");
      ALIASES_BY_LENGTH.forEach(function (alias) {
        var canon = ALIAS_TO_CANONICAL[alias];
        if (result[canon]) return;
        var re = new RegExp("(^|[^a-z])" + escRe(alias) + "([^a-z]|$)", "g");
        var m = re.exec(full);
        if (!m) return;
        var window = full.slice(Math.max(0, m.index - 120), m.index + 120);
        CEFR_ALL_RE.lastIndex = 0;
        var c = CEFR_ALL_RE.exec(window);
        if (c) {
          result[canon] = c[1].toUpperCase();
          return;
        }
        if (/\b(native|muttersprache|mother tongue)\b/.test(window)) {
          result[canon] = "NATIVE";
        }
      });
    }
    return result;
  }

  function siblingLanguage(key, byKey, order, rawValues, cvLangs) {
    var idx = order.indexOf(key);
    if (idx === -1) return "";
    function numOf(k) {
      var m = asciiFold(fieldText(byKey[k] || {})).match(/(\d+)\s*$/);
      return m ? m[1] : null;
    }
    var curNum = numOf(key);
    var candidates = [];
    [1, 2].forEach(function (dist) {
      [idx - dist, idx + dist].forEach(function (j) {
        if (j >= 0 && j < order.length) candidates.push(order[j]);
      });
    });
    if (curNum) {
      candidates.sort(function (a, b) {
        return (numOf(a) === curNum ? 0 : 1) - (numOf(b) === curNum ? 0 : 1);
      });
    }
    for (var i = 0; i < candidates.length; i++) {
      var other = byKey[candidates[i]] || {};
      if (!isLanguageNameField(other, other.options || [])) continue;
      var raw = rawValues ? rawValues[candidates[i]] : "";
      var canon = canonicalLanguage(String(raw == null ? "" : raw).trim());
      if (canon && cvLangs[canon]) return canon;
    }
    return "";
  }

  function rankOptions(opts, target) {
    var score = CEFR_ORDER[target];
    var scored = opts
      .map(function (o) { return [o, optionLevelRange(o)]; })
      .filter(function (pair) { return pair[1] !== null; });
    if (!scored.length) return null;
    function dist(r) {
      if (r[0] <= score && score <= r[1]) return 0;
      return Math.min(Math.abs(score - r[0]), Math.abs(score - r[1]));
    }
    if (target !== "NATIVE") {
      var exact = scored.filter(function (pair) {
        return new RegExp("\\b" + escRe(target) + "\\b", "i").test(pair[0]);
      });
      if (exact.length) return exact[0][0];
    } else {
      var nat = scored.filter(function (pair) { return NATIVE_RE.test(pair[0]); });
      if (nat.length) return nat[0][0];
    }
    var containing = scored.filter(function (pair) {
      return pair[1][0] <= score && score <= pair[1][1];
    });
    if (containing.length) {
      containing.sort(function (a, b) {
        return (a[1][1] - a[1][0]) - (b[1][1] - b[1][0]) || b[1][1] - a[1][1];
      });
      return containing[0][0];
    }
    scored.sort(function (a, b) {
      return dist(a[1]) - dist(b[1]) || b[1][1] - a[1][1];
    });
    return scored[0][0];
  }

  function expectedLanguageOption(field, options, cvText, cvLangs, rawValues, byKey, order) {
    var opts = (options || []).filter(function (o) { return !isPlaceholderOption(o); });
    if (!opts.length) return null;
    field = field || {};
    if (isLanguageNameField(field, options)) return null;
    if (!isLanguageLevelField(field, options)) return null;
    var lang = fieldLanguage(field);
    if (!lang && byKey && order) {
      lang = siblingLanguage(field.key || "", byKey, order, rawValues || {}, cvLangs || {});
    }
    if (!lang || !(cvLangs || {})[lang]) return null;
    var target = (cvLangs || {})[lang];
    if (!CEFR_ORDER[target]) return null;
    return rankOptions(opts, target);
  }

  function matchSelectOption(modelValue, field, options, cvText, cvLangs, rawValues, byKey, order) {
    var opts = options || [];
    if (!opts.length) return null;
    var text = String(modelValue == null ? "" : modelValue).trim();
    if (!text) return null;
    field = field || {};
    if (!cvLangs) cvLangs = parseCvLanguages(cvText || "");
    var folded = {};
    opts.forEach(function (o) { folded[asciiFold(o)] = o; });
    var exactHit = folded[asciiFold(text)];
    if (isLanguageNameField(field, opts)) {
      if (exactHit !== undefined) {
        var c = canonicalLanguage(exactHit);
        return c && languageMentioned(c, cvText || "") ? exactHit : null;
      }
      var canon = canonicalLanguage(text);
      if (canon && languageMentioned(canon, cvText || "")) {
        for (var i = 0; i < opts.length; i++) {
          if (canonicalLanguage(opts[i]) === canon) return opts[i];
        }
      }
      return null;
    }
    if (exactHit !== undefined && isLanguageLevelField(field, opts)) {
      return expectedLanguageOption(field, opts, cvText, cvLangs, rawValues, byKey, order);
    }
    if (exactHit !== undefined) return exactHit;
    if (!isLanguageLevelField(field, opts)) return null;
    CEFR_ALL_RE.lastIndex = 0;
    var codes = [];
    var cm = CEFR_ALL_RE.exec(text);
    while (cm) {
      codes.push(cm[1].toUpperCase());
      cm = CEFR_ALL_RE.exec(text);
    }
    var nativeHit = NATIVE_RE.test(text) && !codes.length;
    var lang = fieldLanguage(field);
    if (!lang && byKey && order) {
      lang = siblingLanguage(field.key || "", byKey, order, rawValues || {}, cvLangs || {});
    }
    if (codes.length && lang && (cvLangs || {})[lang] &&
        codes.indexOf((cvLangs || {})[lang]) !== -1) {
      var withTarget = {};
      Object.keys(cvLangs || {}).forEach(function (k) { withTarget[k] = cvLangs[k]; });
      return expectedLanguageOption(field, opts, cvText, withTarget, rawValues, byKey, order);
    }
    if (codes.length && asciiFold(text) === codes.join(" ").toLowerCase()) {
      if (isVerbatim(codes.join(" "), cvText || "") ||
          codes.some(function (c) { return isVerbatim(c, cvText || ""); })) {
        var bare = opts.filter(function (o) { return !isPlaceholderOption(o); });
        for (var k = 0; k < codes.length; k++) {
          for (var q = 0; q < bare.length; q++) {
            if (new RegExp("\\b" + escRe(codes[k]) + "\\b", "i").test(bare[q])) {
              return bare[q];
            }
          }
        }
      }
    }
    if (nativeHit && isVerbatim(text, cvText || "")) {
      for (var w = 0; w < opts.length; w++) {
        if (NATIVE_RE.test(opts[w])) return opts[w];
      }
    }
    return expectedLanguageOption(field, opts, cvText, cvLangs || {}, rawValues, byKey, order);
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
    const order = [];
    (fields || []).forEach((f) => {
      if (f && f.key) {
        byKey[f.key] = f;
        order.push(f.key);
      }
    });
    var cvLangs = {};
    try {
      cvLangs = parseCvLanguages(cvText || "");
    } catch (e) {
      cvLangs = {};
    }
    const cleaned = {};
    const dropped = [];
    Object.keys(byKey).forEach((key) => {
      let value = rawValues ? rawValues[key] : "";
      value = value == null ? "" : String(value).trim();
      const field = byKey[key] || {};
      if (!field.key) field.key = key;
      const options = field.options || [];
      if (value === "") {
        var auto = null;
        try {
          auto = expectedLanguageOption(field, options, cvText || "", cvLangs,
            rawValues || {}, byKey, order);
        } catch (e) {
          auto = null;
        }
        cleaned[key] = auto || "";
        return;
      }
      if (options.length) {
        if (options.includes(value)) {
          if (isVerbatim(value, cvText)) {
            cleaned[key] = value;
            return;
          }
          var expected = null;
          try {
            expected = expectedLanguageOption(field, options, cvText || "", cvLangs,
              rawValues || {}, byKey, order);
          } catch (e) {
            expected = null;
          }
          if (expected !== null && expected === value) {
            cleaned[key] = value;
            return;
          }
          if (expected !== null && isLanguageLevelField(field, options)) {
            cleaned[key] = expected;
            return;
          }
          var mapped = null;
          try {
            mapped = matchSelectOption(value, field, options, cvText || "", cvLangs,
              rawValues || {}, byKey, order);
          } catch (e) {
            mapped = null;
          }
          if (mapped === value) {
            cleaned[key] = value;
            return;
          }
          cleaned[key] = "";
          dropped.push(key);
          return;
        }
        var mapped2 = null;
        try {
          mapped2 = matchSelectOption(value, field, options, cvText || "", cvLangs,
            rawValues || {}, byKey, order);
        } catch (e) {
          mapped2 = null;
        }
        if (mapped2) {
          cleaned[key] = mapped2;
          return;
        }
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

  const api = {
    isVerbatim,
    validateValues,
    parseCvLanguages,
    matchSelectOption,
    expectedLanguageOption,
    isLanguageLevelField,
    isLanguageNameField,
    fieldLanguage,
    canonicalLanguage,
  };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  } else {
    root.CVFillVerbatim = api;
  }
})(typeof self !== "undefined" ? self : this);
