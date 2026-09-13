const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const {
  isFillable,
  skipReason,
  resolveLabel,
  describeField,
  buildSuggestPayload,
  labelTextForElement,
  isRequired,
  findSectionContext,
  collectCandidates,
  scanPage,
  findBestOption,
} = require("../content.js");
const {
  isVerbatim,
  validateValues,
  parseCvLanguages,
  matchSelectOption,
  isLanguageLevelField,
} = require("../verbatim.js");

// ---------- isFillable: ban-safe allowlist ----------

describe("isFillable", () => {
  it("accepts visible text inputs", () => {
    assert.equal(isFillable({ tag: "INPUT", type: "text", disabled: false, hidden: false }), true);
  });
  it("rejects password, file, submit, hidden, disabled", () => {
    assert.equal(isFillable({ tag: "INPUT", type: "password" }), false);
    assert.equal(isFillable({ tag: "INPUT", type: "file" }), false);
    assert.equal(isFillable({ tag: "INPUT", type: "submit" }), false);
    assert.equal(isFillable({ tag: "INPUT", type: "hidden" }), false);
    assert.equal(isFillable({ tag: "INPUT", type: "text", disabled: true }), false);
    assert.equal(isFillable({ tag: "INPUT", type: "text", hidden: true }), false);
  });
  it("rejects captcha-like fields", () => {
    assert.equal(isFillable({ tag: "INPUT", type: "text", name: "g-recaptcha-response" }), false);
    assert.equal(isFillable({ tag: "TEXTAREA", name: "h-captcha-response" }), false);
  });
});

// ---------- resolveLabel ----------

describe("resolveLabel", () => {
  it("prefers aria-label, then associated label, then placeholder, then name", () => {
    assert.equal(
      resolveLabel({ ariaLabel: "Email address", labelText: "Ignored", placeholder: "x", name: "y" }),
      "Email address"
    );
    assert.equal(resolveLabel({ labelText: "Full name", placeholder: "x", name: "y" }), "Full name");
    assert.equal(resolveLabel({ placeholder: "Phone", name: "y" }), "Phone");
    assert.equal(resolveLabel({ name: "firstName" }), "firstName");
  });
});

// ---------- describeField ----------

describe("describeField", () => {
  it("builds a stable key and keeps select options", () => {
    const d = describeField(
      { tag: "SELECT", name: "auth", id: "auth", options: ["Yes", "No"] },
      3,
      "Work authorization"
    );
    assert.equal(d.key, "f3");
    assert.equal(d.label, "Work authorization");
    assert.deepEqual(d.options, ["Yes", "No"]);
  });
});

// ---------- buildSuggestPayload ----------

describe("buildSuggestPayload", () => {
  it("throws on empty CV or fields or provider", () => {
    assert.throws(() => buildSuggestPayload("", [{ key: "f0" }], { base_url: "http://x", model: "m" }));
    assert.throws(() => buildSuggestPayload("cv", [], { base_url: "http://x", model: "m" }));
    assert.throws(() => buildSuggestPayload("cv", [{ key: "f0" }], { base_url: "", model: "" }));
  });
  it("builds the backend payload", () => {
    const p = buildSuggestPayload("John Doe", [{ key: "f0", label: "Name" }], {
      base_url: "http://localhost:11434/v1", model: "llama3", api_key: "",
    });
    assert.equal(p.cv_text, "John Doe");
    assert.equal(p.fields.length, 1);
    assert.equal(p.provider.model, "llama3");
  });
});

// ---------- verbatim guard (client-side mirror) ----------

describe("verbatim guard", () => {
  it("accepts exact substrings, rejects inventions", () => {
    assert.equal(isVerbatim("John Doe", "John Doe\njohn@example.com"), true);
    assert.equal(isVerbatim("Jane Smith", "John Doe"), false);
  });
  it("drops hallucinated values and bad select options", () => {
    const cv = "John Doe\njohn@example.com";
    const fields = [{ key: "f0", label: "Name" }, { key: "f1", label: "Email" }];
    const { cleaned, dropped } = validateValues({ f0: "John Doe", f1: "fake@evil.com" }, cv, fields);
    assert.equal(cleaned.f0, "John Doe");
    assert.equal(cleaned.f1, "");
    assert.ok(dropped.includes("f1"));
  });
  it("rejects select values not in options", () => {
    const { cleaned } = validateValues({ f0: "Maybe" }, "Maybe", [
      { key: "f0", options: ["Yes", "No"] },
    ]);
    assert.equal(cleaned.f0, "");
  });
  it("accepts a multi-line bullet block copied from the CV in order", () => {
    const cv = "EXPERIENCE\n- Built thing A.\n- Built thing B.\n";
    assert.equal(
      isVerbatim("- Built thing A.\n- Built thing B.", cv),
      true
    );
  });
  it("rejects a bullet block with an invented line", () => {
    const cv = "- Built thing A.\n- Built thing B.";
    assert.equal(
      isVerbatim("- Built thing A.\n- Led a team of astronauts.", cv),
      false
    );
  });
  it("rejects reordered bullets", () => {
    const cv = "- First thing.\n- Second thing.";
    assert.equal(isVerbatim("- Second thing.\n- First thing.", cv), false);
  });
});

// ---------- skip reasons (explains what the scanner leaves alone) ----------

describe("skipReason", () => {
  it("returns null for fillable fields", () => {
    assert.equal(skipReason({ tag: "INPUT", type: "text" }), null);
  });
  it("classifies file, password and captcha separately from other skips", () => {
    assert.equal(skipReason({ tag: "INPUT", type: "file", name: "resume" }), "file");
    assert.equal(skipReason({ tag: "INPUT", type: "password" }), "password");
    assert.equal(skipReason({ tag: "INPUT", type: "text", name: "g-recaptcha-response" }), "captcha");
    assert.equal(skipReason({ tag: "INPUT", type: "checkbox" }), "other");
    assert.equal(skipReason({ tag: "INPUT", type: "text", disabled: true }), "other");
  });
});

// ---------- label quality ----------

function fakeNode({ tag = "DIV", text = "", attrs = {}, children = [] } = {}) {
  const node = {
    tagName: tag,
    textContent: text,
    parentElement: null,
    previousElementSibling: null,
    getAttribute: (n) => (n in attrs ? attrs[n] : null),
    closest: () => null,
    querySelector: () => null,
    children,
    setAttribute: () => {},
    offsetParent: {},
  };
  children.forEach((c, i) => {
    c.parentElement = node;
    if (i > 0) c.previousElementSibling = children[i - 1];
  });
  return node;
}

describe("labelTextForElement", () => {
  it("resolves aria-labelledby references", () => {
    const label = fakeNode({ tag: "LABEL", text: "Work authorization" });
    const doc = { getElementById: (id) => (id === "lbl1" ? label : null), querySelector: () => null };
    const el = fakeNode({ tag: "INPUT", attrs: { "aria-labelledby": "lbl1" } });
    assert.equal(labelTextForElement(el, doc), "Work authorization");
  });
  it("finds a nearby label when the input has no id or wrapping label", () => {
    const label = fakeNode({ tag: "LABEL", text: "Location*" });
    const input = fakeNode({ tag: "INPUT", attrs: { placeholder: "Start typing..." } });
    const wrapper = fakeNode({ tag: "DIV", children: [input] });
    fakeNode({ tag: "DIV", children: [label, wrapper] });
    const doc = { getElementById: () => null, querySelector: () => null };
    assert.equal(labelTextForElement(input, doc), "Location*");
  });
  it("ignores nearby containers that hold other form controls", () => {
    const decoy = fakeNode({ tag: "DIV", text: "Not a label", children: [fakeNode({ tag: "INPUT" })] });
    decoy.querySelector = () => ({}) // contains a form control
    const input = fakeNode({ tag: "INPUT", attrs: { placeholder: "Email" } });
    const wrapper = fakeNode({ tag: "DIV", children: [input] });
    fakeNode({ tag: "DIV", children: [decoy, wrapper] });
    const doc = { getElementById: () => null, querySelector: () => null };
    assert.equal(labelTextForElement(input, doc), "");
  });
});

describe("findSectionContext", () => {
  it("finds the nearest heading above the field", () => {
    const input = fakeNode({ tag: "INPUT" });
    const fieldWrapper = fakeNode({ tag: "DIV", children: [input] });
    const heading = fakeNode({ tag: "H2", text: "Education" });
    fakeNode({ tag: "SECTION", children: [heading, fieldWrapper] });
    assert.equal(findSectionContext(input, {}), "Education");
  });
  it("returns an empty string when no heading is nearby", () => {
    const input = fakeNode({ tag: "INPUT" });
    fakeNode({ tag: "DIV", children: [input] });
    assert.equal(findSectionContext(input, {}), "");
  });
});

describe("describeField context", () => {
  it("carries the section context through to the model payload", () => {
    const d = describeField(
      { tag: "TEXTAREA", name: "description", context: "Education" },
      0,
      "Description"
    );
    assert.equal(d.context, "Education");
  });
});

describe("resolveLabel", () => {
  it("cleans whitespace and required-field asterisks", () => {
    assert.equal(resolveLabel({ labelText: "Location*" }), "Location");
    assert.equal(resolveLabel({ labelText: "  First   and  Last Name  " }), "First and Last Name");
  });
});

describe("isRequired", () => {
  it("detects required flags, aria-required and label asterisks", () => {
    assert.equal(isRequired({ required: true }, ""), true);
    assert.equal(isRequired({ getAttribute: () => "true" }, ""), true);
    assert.equal(isRequired({}, "Location*"), true);
    assert.equal(isRequired({}, "Nickname"), false);
  });
});

describe("describeField", () => {
  it("marks required fields", () => {
    const d = describeField({ tag: "INPUT", name: "email", required: true }, 0, "Email");
    assert.equal(d.required, true);
  });
  it("omits the flag when not required", () => {
    const d = describeField({ tag: "INPUT", name: "nick" }, 0, "Nickname");
    assert.ok(!("required" in d));
  });
});

// ---------- nested controls (shadow DOM / same-origin iframes) ----------

describe("collectCandidates", () => {
  it("descends into open shadow roots and same-origin iframes", () => {
    const shadowInput = { tagName: "INPUT" };
    const shadowRoot = {
      querySelectorAll: (sel) => (sel === "input, textarea, select" ? [shadowInput] : []),
    };
    const host = { tagName: "DIV", shadowRoot, querySelectorAll: () => [] };
    const frameInput = { tagName: "TEXTAREA" };
    const frameDoc = {
      querySelectorAll: (sel) => (sel === "input, textarea, select" ? [frameInput] : []),
    };
    const iframe = { tagName: "IFRAME", contentDocument: frameDoc, shadowRoot: null };
    const root = {
      querySelectorAll: (sel) => (sel === "input, textarea, select" ? [] : [host, iframe]),
    };
    const out = collectCandidates(root);
    assert.ok(out.includes(shadowInput));
    assert.ok(out.includes(frameInput));
  });
});

// ---------- scanPage reports skipped fields ----------

describe("scanPage", () => {
  it("returns fields plus human-readable skipped entries", () => {
    const text = fakeNode({ tag: "INPUT", attrs: {}, });
    text.type = "text";
    text.name = "fullName";
    text.id = "";
    text.className = "";
    text.placeholder = "";
    const file = fakeNode({ tag: "INPUT" });
    file.type = "file";
    file.name = "resume";
    file.id = "";
    file.className = "";
    file.placeholder = "";
    const doc = {
      querySelectorAll: () => [text, file],
      querySelector: () => null,
      getElementById: () => null,
    };
    const { fields, skipped } = scanPage(doc);
    assert.equal(fields.length, 1);
    assert.equal(fields[0].name, "fullName");
    assert.equal(skipped.length, 1);
    assert.match(skipped[0].reason, /manually/i);
  });
});

// ---------- language-proficiency select mapping ----------

const SHOT_OPTIONS = [
  "-- Please select --",
  "C2 (Verhandlungssicher)",
  "C1 (Fließend)",
  "B2 (Fortgeschritten)",
  "A1-B1 (Grundkenntnisse)",
  "Keine",
];
const SHOT_CV = [
  "Robert-Marian Dragan", "C1", "B2", "NATIVE", "NATIVE", "A1",
  "English (US)", "German", "Spanish", "Romanian", "French",
].join("\n");

describe("parseCvLanguages", () => {
  it("pairs column-style level and language blocks positionally", () => {
    assert.deepEqual(parseCvLanguages(SHOT_CV), {
      english: "C1",
      german: "B2",
      spanish: "NATIVE",
      romanian: "NATIVE",
      french: "A1",
    });
  });
  it("pairs inline language+level segments independently", () => {
    assert.deepEqual(
      parseCvLanguages("Languages: English C1, German B2, French A1"),
      { english: "C1", german: "B2", french: "A1" }
    );
  });
});

describe("language select guard (verbatim mirror)", () => {
  const shot = () => [{ key: "f0", label: "Deutsch-Kenntnisse", type: "select", options: SHOT_OPTIONS }];
  it("maps a bare CEFR code to the composite option", () => {
    const { cleaned, dropped } = validateValues({ f0: "B2" }, SHOT_CV, shot());
    assert.equal(cleaned.f0, "B2 (Fortgeschritten)");
    assert.deepEqual(dropped, []);
  });
  it("auto-resolves an empty language level from the CV", () => {
    const { cleaned, dropped } = validateValues({ f0: "" }, SHOT_CV, shot());
    assert.equal(cleaned.f0, "B2 (Fortgeschritten)");
    assert.deepEqual(dropped, []);
  });
  it("corrects a confused level to the CV-derived option", () => {
    const { cleaned } = validateValues({ f0: "C1 (Fließend)" }, SHOT_CV, shot());
    assert.equal(cleaned.f0, "B2 (Fortgeschritten)");
  });
  it("maps CEFR to bare descriptive words", () => {
    const fields = [{ key: "f0", label: "German proficiency", options: ["Native", "Fluent", "Professional", "Intermediate", "Basic"] }];
    assert.equal(validateValues({ f0: "" }, SHOT_CV, fields).cleaned.f0, "Professional");
  });
  it("resolves split language/level pairs via the sibling field", () => {
    const fields = [
      { key: "f0", label: "Language", options: ["English", "German", "French"] },
      { key: "f1", label: "Level", options: ["Native", "Fluent", "Intermediate", "Basic"] },
    ];
    const { cleaned, dropped } = validateValues({ f0: "German", f1: "" }, SHOT_CV, fields);
    assert.equal(cleaned.f0, "German");
    assert.equal(cleaned.f1, "Fluent");
    assert.deepEqual(dropped, []);
  });
  it("maps language-name synonyms with CV evidence", () => {
    const fields = [{ key: "f0", label: "Sprache", options: ["Deutsch", "Englisch", "Französisch"] }];
    assert.equal(validateValues({ f0: "German" }, SHOT_CV, fields).cleaned.f0, "Deutsch");
  });
  it("leaves levels for languages missing from the CV empty", () => {
    const fields = [{ key: "f0", label: "Italian proficiency", options: ["Native", "Fluent", "Intermediate", "Basic"] }];
    const { cleaned, dropped } = validateValues({ f0: "Fluent" }, SHOT_CV, fields);
    assert.equal(cleaned.f0, "");
    assert.ok(dropped.includes("f0"));
  });
  it("keeps non-language selects strict", () => {
    const { cleaned } = validateValues({ f0: "Maybe" }, "Maybe", [{ key: "f0", options: ["Yes", "No"] }]);
    assert.equal(cleaned.f0, "");
  });
  it("detects proficiency option lists", () => {
    assert.equal(isLanguageLevelField({ label: "Deutsch-Kenntnisse" }, SHOT_OPTIONS), true);
    assert.equal(isLanguageLevelField({ label: "Work auth" }, ["Yes", "No"]), false);
  });
  it("matches bare model values to options for mapping", () => {
    assert.equal(
      matchSelectOption("B2", { label: "Deutsch-Kenntnisse", key: "f0" }, SHOT_OPTIONS, SHOT_CV),
      "B2 (Fortgeschritten)"
    );
  });
});

describe("findBestOption", () => {
  const fakeSelect = (texts) => ({ options: texts.map((t) => ({ text: t, value: t })) });
  it("resolves bare CEFR codes to composite options", () => {
    const m = findBestOption(fakeSelect(SHOT_OPTIONS), "B2");
    assert.equal(m.text, "B2 (Fortgeschritten)");
  });
  it("resolves language synonyms", () => {
    const m = findBestOption(fakeSelect(["Deutsch", "Englisch"]), "German");
    assert.equal(m.text, "Deutsch");
  });
  it("still matches exact option text", () => {
    const m = findBestOption(fakeSelect(SHOT_OPTIONS), "Keine");
    assert.equal(m.text, "Keine");
  });
  it("returns null instead of inventing an option", () => {
    assert.equal(findBestOption(fakeSelect(["Yes", "No"]), "Maybe"), null);
  });
});

describe("scanPage", () => {
  it("keeps hidden selects that back searchable dropdown widgets", () => {
    // Searchable dropdowns hide the native <select> (display:none) and
    // render custom UI; filling the hidden select still works.
    const sel = fakeNode({ tag: "SELECT" });
    sel.type = "select-one";
    sel.name = "deutsch";
    sel.id = "";
    sel.className = "";
    sel.placeholder = "";
    sel.offsetParent = null; // hidden layout box
    sel.options = [{ textContent: "B2 (Fortgeschritten) " }];
    sel.getAttribute = () => null;
    sel.closest = () => null;
    const doc = {
      querySelectorAll: (s) => (s === "input, textarea, select" ? [sel] : []),
      querySelector: () => null,
      getElementById: () => null,
      defaultView: {
        getComputedStyle: () => ({ display: "none", visibility: "visible" }),
      },
    };
    const { fields } = scanPage(doc);
    assert.equal(fields.length, 1);
    assert.deepEqual(fields[0].options, ["B2 (Fortgeschritten)"]);
  });
});
