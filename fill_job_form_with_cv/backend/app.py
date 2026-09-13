"""Flask backend for the Firefox CV autofill extension.

Responsibilities (only what the extension cannot safely do itself):
1. Parse an uploaded CV PDF into plain text (/api/parse-pdf).
2. Proxy OpenAI-compatible chat-completion calls and enforce a strict
   verbatim-only guard so the model copy-pastes from the CV instead of
   hallucinating (/api/suggest-fill).

The extension itself only reads/fills the active tab after an explicit
user click; it never auto-submits forms or touches CAPTCHAs.
"""
import json
import os
import re
from io import BytesIO
from urllib.parse import urlparse

import requests
from flask import Flask, jsonify, render_template, request
from flask_cors import CORS
from pypdf import PdfReader

MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_FIELDS = 100
# Only these are ever written to settings.json. The API key is deliberately
# NOT persisted there: it is read from backend/.env (or the process
# environment) at startup and held in memory for the session. Setting one from
# the dashboard overrides it for the current run only.
SETTINGS_KEYS = ("base_url", "model", "cv_text")
ENV_API_KEY = "CVFILL_API_KEY"
# Accepted .env / environment variable names, most specific first.
KEY_ENV_VARS = ("CVFILL_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY",
                "LLM_API_KEY")
ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


def load_env_file(path: str = ENV_FILE) -> None:
    """Load KEY=VALUE lines from a .env file into os.environ (no override).

    Keeps the backend dependency-free while letting users keep secrets out of
    settings.json and shell history. Existing environment variables win.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        value = raw.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def env_api_key() -> str:
    """First non-empty API key found in the environment (.env included)."""
    for name in KEY_ENV_VARS:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


load_env_file()
_runtime_api_key = env_api_key()


def get_api_key() -> str:
    """The session API key (.env at startup, or set from the dashboard)."""
    return _runtime_api_key or env_api_key()


def set_api_key(value) -> str:
    global _runtime_api_key
    _runtime_api_key = str(value or "").strip()
    return _runtime_api_key


def is_local_provider(base_url: str) -> bool:
    """True for loopback hosts (e.g. Ollama), which need no API key."""
    host = (urlparse(base_url or "").hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".local")


def missing_key_message(base_url: str) -> str:
    """Actionable error when a remote provider has no API key configured."""
    host = urlparse(base_url or "").hostname or (base_url or "provider")
    var = "CVFILL_API_KEY"
    if "openrouter" in host.lower():
        var = "OPENROUTER_API_KEY"
    elif "openai" in host.lower():
        var = "OPENAI_API_KEY"
    return (
        f"No API key configured for '{host}'. Add a line to backend/.env "
        f"(`{var}=<your-key>`) and restart the backend, or paste a key in the "
        f"dashboard and click Save."
    )

# Only the extension UI (moz-extension://<uuid>) and the local dashboard may
# read responses. A normal website must NOT be able to read /api/settings
# (which holds the CV + API key) or drive the backend. Override with a
# comma-separated CVFILL_ALLOWED_ORIGINS if you need other origins.
DEFAULT_ALLOWED_ORIGINS = (
    "moz-extension://.*,null,"
    "http://127.0.0.1:5000,http://localhost:5000"
)
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "CVFILL_ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS
    ).split(",")
    if o.strip()
]

SYSTEM_PROMPT = (
    "You are a strict copy-paste form-filling assistant. "
    "You receive a CV and a list of job-form fields. "
    "Rules you MUST follow:\n"
    "1. Copy values VERBATIM as exact substrings of the CV text. "
    "Do not rephrase, do not reformat, do not translate, do not infer.\n"
    "2. If a field's answer is not stated explicitly in the CV, "
    "return an empty string \"\" for that field.\n"
    "3. Do not invent, guess, or hallucinate any name, email, phone, "
    "date, address, employer, school, or skill.\n"
    "4. For select/dropdown fields, return exactly one of the given "
    "options or an empty string. Never invent a new option.\n"
    "5. Never add explanations. Return ONLY a JSON object of the form "
    "{\"values\": {\"<field_key>\": \"<exact CV substring or empty string>\"}}.\n"
    "6. For multi-line free-text fields (description, responsibilities, "
    "summary), copy the ENTIRE relevant block from the matching CV section "
    "verbatim, keeping the original bullet markers and line breaks. Never "
    "summarise, reword, or merge bullets.\n"
    "7. Each field may include a \"context\" naming the CV section it belongs "
    "to (e.g. \"Education\", \"Professional Experience\"). Use it to pick the "
    "matching entry; do not copy from a different section.\n"
    "8. For language-proficiency selects (options like 'C2 "
    "(Verhandlungssicher)', 'B2 (Fortgeschritten)', 'A1-B1 "
    "(Grundkenntnisse)' or words like native/fluent/professional/"
    "intermediate/basic/Grundkenntnisse/Muttersprache/Keine): pick the "
    "EXACT option whose CEFR code matches the CV level for THAT language "
    "(e.g. CV German B2 -> the option containing 'B2'; CV NATIVE -> "
    "'native'/'Muttersprache'). Map CEFR to bare words when options have "
    "no codes: NATIVE->native/Muttersprache, C2/C1->fluent/fliessend, "
    "B2->professional/fortgeschritten, B1->intermediate, "
    "A1/A2->basic/Grundkenntnisse.\n"
    "9. When language and level are two separate fields, answer the "
    "language field with the exact language option and the level field "
    "with the matching level option for that same language."
)

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}})


def settings_path() -> str:
    """settings.json location; override with CVFILL_SETTINGS (used by tests)."""
    override = os.environ.get("CVFILL_SETTINGS")
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "settings.json")


def load_settings() -> dict:
    """Stored provider + CV config. Missing/corrupt file -> empty shape."""
    data = {k: "" for k in SETTINGS_KEYS}
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for key in SETTINGS_KEYS:
                if isinstance(raw.get(key), str):
                    data[key] = raw[key]
    except (OSError, ValueError):
        pass
    return data


def public_settings() -> dict:
    """Settings safe to send to the UI: never includes the API key itself."""
    data = load_settings()
    data["api_key_set"] = bool(get_api_key())
    return data


def save_settings(payload: dict) -> dict:
    """Validate and persist settings. The API key is never written to disk."""
    if not isinstance(payload, dict):
        raise ValueError("Settings must be a JSON object")
    base_url = str(payload.get("base_url", "")).strip()
    model = str(payload.get("model", "")).strip()
    cv_text = payload.get("cv_text", "")
    if not base_url or not model:
        raise ValueError("base_url and model are required")
    if not isinstance(cv_text, str):
        raise ValueError("cv_text must be a string")
    if "api_key" in payload:
        api_key = payload.get("api_key", "")
        if not isinstance(api_key, str):
            raise ValueError("api_key must be a string")
        if api_key.strip():
            set_api_key(api_key)
    base_url = validate_provider(base_url, model)
    data = {"base_url": base_url, "model": model, "cv_text": cv_text}
    path = settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return public_settings()


def validate_provider(base_url: str, model: str) -> str:
    """Validate an OpenAI-compatible provider target and return a clean URL.

    Only plain http(s) URLs with a host are accepted. This keeps a malicious
    page (or a bad stored setting) from turning the local backend into an
    SSRF/open proxy. Optionally restrict hosts with a comma-separated
    CVFILL_ALLOWED_PROVIDER_HOSTS env var.
    """
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("base_url is required")
    if not (model or "").strip():
        raise ValueError("model is required")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("base_url must be an http(s) URL")
    if not parsed.hostname:
        raise ValueError("base_url must include a host")
    if parsed.username or parsed.password:
        raise ValueError("base_url must not contain credentials")
    allowed_hosts = [
        h.strip().lower()
        for h in os.environ.get("CVFILL_ALLOWED_PROVIDER_HOSTS", "").split(",")
        if h.strip()
    ]
    if allowed_hosts and parsed.hostname.lower() not in allowed_hosts:
        raise ValueError(f"host '{parsed.hostname}' is not an allowed provider host")
    return base_url


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


_BULLET_SPLIT_RE = re.compile(r"[\r\n]+|\s+[-•*]\s+")


def _strip_bullet(segment: str) -> str:
    return re.sub(r"^[-•*]\s+", "", segment.strip())


# ---- Language / CEFR select mapping -------------------------------------
# Job forms often ask for a proficiency level with composite options such as
# "B2 (Fortgeschritten)" or bare words ("native", "fluent", "professional",
# "basic", "Grundkenntnisse", "Muttersprache") while the CV only states a
# bare CEFR code (A1..C2, NATIVE). An exact option match can therefore never
# be verbatim. The helpers below map deterministically CV CEFR -> option so
# the guard can accept the correct option without hallucinating. They are
# deliberately conservative: mapping only applies to fields/options that
# look like language proficiency (or language names), and the target level
# must come from CV evidence for THAT language.

CEFR_ORDER = {"A1": 1, "A2": 2, "B1": 3, "B2": 4, "C1": 5, "C2": 6,
              "NATIVE": 7}
CEFR_RE = re.compile(r"\b([ABC][12])\b", re.IGNORECASE)
NATIVE_RE = re.compile(
    r"\b(natives?|muttersprache|mother\s*tongue|bilingual|nativ)\b",
    re.IGNORECASE)
NONE_RE = re.compile(
    r"\b(keine[rsmn]?|kein\b|no\s*knowledge|no\s*skills?|none\b)\b",
    re.IGNORECASE)

# canonical language -> aliases (all compared ASCII-folded, lowercase)
_LANGUAGE_ALIASES: dict = {
    "german": ["german", "deutsch"],
    "english": ["english", "englisch"],
    "spanish": ["spanish", "spanisch", "espanol"],
    "french": ["french", "franzosisch", "francais"],
    "romanian": ["romanian", "rumanisch", "romana"],
    "italian": ["italian", "italienisch"],
    "portuguese": ["portuguese", "portugiesisch"],
    "dutch": ["dutch", "niederlandisch"],
    "polish": ["polish", "polnisch"],
    "czech": ["czech", "tschechisch"],
    "slovak": ["slovak", "slowakisch"],
    "hungarian": ["hungarian", "ungarisch"],
    "turkish": ["turkish", "turkisch"],
    "arabic": ["arabic", "arabisch"],
    "chinese": ["chinese", "chinesisch"],
    "japanese": ["japanese", "japanisch"],
    "russian": ["russian", "russisch"],
    "ukrainian": ["ukrainian", "ukrainisch"],
}
_ALIAS_TO_CANONICAL: dict = {}
for _canon, _aliases in _LANGUAGE_ALIASES.items():
    for _a in _aliases:
        _ALIAS_TO_CANONICAL[_a] = _canon


def _ascii_fold(text: str) -> str:
    import unicodedata
    folded = unicodedata.normalize("NFKD", str(text or ""))
    folded = folded.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", folded.strip().lower())


def _is_placeholder_option(option: str) -> bool:
    n = _ascii_fold(option)
    if not n or n in {"--", "-", "select", "please select", "bitte wahlen",
                      "bitte waehlen", "auswahlen", "wahl"}:
        return True
    return n.startswith("--") or "please select" in n or "bitte wahlen" in n


def _option_level_range(option: str):
    """Return (low, high) CEFR scores for a proficiency option, or None."""
    text = str(option or "")
    codes = [c.upper() for c in CEFR_RE.findall(text)]
    if codes:
        scores = sorted(CEFR_ORDER[c] for c in codes if c in CEFR_ORDER)
        if scores:
            return (scores[0], scores[-1])
    n = _ascii_fold(text)
    if not n or _is_placeholder_option(text):
        return None
    if NONE_RE.search(n):
        return (0, 0)
    if NATIVE_RE.search(n):
        return (7, 7)
    if re.search(r"verhandlungssicher|business fluent|near native", n):
        return (6, 6)
    if re.search(r"fliessend|fluent|full professional", n):
        return (5, 5)
    if re.search(r"fortgeschritten|upper intermediate|independent|"
                 r"professional|beruf", n):
        return (4, 4)
    if re.search(r"[^a-z]b1[^a-z]|^b1[^a-z]|[^a-z]b1$|^b1$|intermediate|"
                 r"mittelstufe|conversational", n):
        # "intermediate" alone -> B1; "upper intermediate" handled above
        if "upper" in n:
            return (4, 4)
        return (3, 3)
    if re.search(r"elementary|pre intermediate|pre-intermediate", n):
        return (2, 2)
    if re.search(r"grundkenntnisse|grundlagen|basic|beginner|anfanger", n):
        return (1, 1)
    # bare CEFR written with spaces/punctuation the regex missed
    m = re.search(r"\b([abc])\s*([12])\b", n)
    if m:
        code = (m.group(1) + m.group(2)).upper()
        if code in CEFR_ORDER:
            s = CEFR_ORDER[code]
            return (s, s)
    return None


def _looks_like_proficiency_options(options) -> bool:
    scorable = 0
    has_code = False
    for opt in options or []:
        if _is_placeholder_option(opt):
            continue
        if CEFR_RE.search(str(opt or "")):
            has_code = True
            scorable += 1
        elif _option_level_range(opt) is not None:
            scorable += 1
    return scorable >= 2 or (has_code and scorable >= 2)


def _field_text(field: dict) -> str:
    return " ".join(str(field.get(k) or "") for k in ("label", "name",
                                                     "context"))


def is_language_level_field(field: dict, options) -> bool:
    """True when the select asks for a proficiency level."""
    opts = list(options or [])
    if not opts:
        return False
    if _looks_like_proficiency_options(opts):
        return True
    n = _ascii_fold(_field_text(field or {}))
    if not n:
        return False
    keywords = ("sprach", "kenntnis", "proficiency", "fluency", "fluent",
                "niveau", "cefr", "language", "langue", "idioma", "level",
                "stufe", "deutsch", "german", "englisch", "english",
                "spanisch", "spanish", "french", "roman", "italian")
    if any(k in n for k in keywords):
        return any(_option_level_range(o) is not None for o in opts
                   if not _is_placeholder_option(o))
    return False


def is_language_name_field(field: dict, options) -> bool:
    """True when the select asks to pick a language name."""
    opts = [o for o in (options or []) if not _is_placeholder_option(o)]
    if len(opts) < 2:
        return False
    hits = sum(1 for o in opts if _canonical_language(o))
    if hits >= 2 and hits / max(len(opts), 1) >= 0.5:
        return True
    n = _ascii_fold(_field_text(field or {}))
    if any(k in n for k in ("language", "sprache", "langue", "idioma",
                            "muttersprache")):
        return hits >= 1
    return False


def _canonical_language(text: str) -> str:
    """Canonical language key for a label/option, or ''."""
    n = _ascii_fold(text)
    if not n:
        return ""
    # strip parentheticals like "english (us)" -> "english"
    base = re.sub(r"\(.*?\)", "", n).strip()
    candidates = [n, base]
    # longest alias first so multi-word hits win
    for cand in candidates:
        for alias in sorted(_ALIAS_TO_CANONICAL,
                            key=len, reverse=True):
            if re.search(r"(^|[^a-z])" + re.escape(alias) + r"([^a-z]|$)",
                         cand):
                return _ALIAS_TO_CANONICAL[alias]
    return ""


def field_language(field: dict) -> str:
    return _canonical_language(_field_text(field or {}))


def _language_mentioned(canonical: str, cv_text: str) -> bool:
    n = _ascii_fold(cv_text or "")
    for alias in _LANGUAGE_ALIASES.get(canonical, [canonical]):
        if re.search(r"(^|[^a-z])" + re.escape(alias) + r"([^a-z]|$)", n):
            return True
    return False


def parse_cv_languages(cv_text: str) -> dict:
    """Map canonical language -> CEFR code ('A1'..'C2','NATIVE').

    Handles inline ('German: B2', 'English (C1)', 'Spanish - Native'),
    column-style (levels block then languages block, zipped positionally),
    and proximity fallbacks.
    """
    result: dict = {}
    lines = [ln.strip() for ln in re.split(r"[\r\n]+", str(cv_text or ""))
             if ln.strip()]
    # 1. inline: language + level on the same line. Pair within the same
    # comma/semicolon segment first ("English C1, German B2" must give
    # English=C1, German=B2, not both C1), falling back to the nearest
    # code on the whole line. All matching runs on the same folded
    # string so offsets line up.
    def _pair_segment(seg: str):
        code_spans = [(m.group(1).upper(), m.start())
                      for m in CEFR_RE.finditer(seg)]
        native_spans = [m.start() for m in NATIVE_RE.finditer(seg)]
        if not code_spans and not native_spans:
            return
        for alias in sorted(_ALIAS_TO_CANONICAL, key=len, reverse=True):
            canon = _ALIAS_TO_CANONICAL[alias]
            if canon in result:
                continue
            m = re.search(r"(^|[^a-z])" + re.escape(alias) + r"([^a-z]|$)",
                          seg)
            if not m:
                continue
            pos = m.start()
            best = None
            best_dist = None
            for code, cpos in code_spans:
                d = abs(cpos - pos)
                if best_dist is None or d < best_dist:
                    best_dist = d
                    best = code
            native_dist = min((abs(p - pos) for p in native_spans),
                              default=None)
            if best is not None and (native_dist is None or
                                     best_dist <= native_dist):
                result[canon] = best
            elif native_dist is not None:
                result[canon] = "NATIVE"

    for line in lines:
        folded = _ascii_fold(line)
        if not CEFR_RE.search(folded) and not NATIVE_RE.search(folded):
            continue
        segments = re.split(r"[;,|/]", folded)
        if len(segments) > 1:
            for seg in segments:
                _pair_segment(seg)
            # leftovers (language in one segment, code in another) pair
            # line-wide
            if any(c not in result for c in
                   {_ALIAS_TO_CANONICAL[a] for a in _ALIAS_TO_CANONICAL
                    if re.search(r"(^|[^a-z])" + re.escape(a) +
                                 r"([^a-z]|$)", folded)}):
                _pair_segment(folded)
        else:
            _pair_segment(folded)

    # 2. column-style: standalone level lines + standalone language lines
    def _is_level_only(line: str):
        n = _ascii_fold(re.sub(r"^[-•*]\s+", "", line).strip("() [].:"))
        if n in {"a1", "a2", "b1", "b2", "c1", "c2", "native",
                 "muttersprache", "nativ"}:
            if n.startswith("nativ") or n in {"native", "muttersprache"}:
                return "NATIVE"
            return n.upper()
        return None

    def _is_language_only(line: str):
        cleaned = re.sub(r"^[-•*]\s+", "", line).strip()
        folded = _ascii_fold(re.sub(r"\(.*?\)", "", cleaned).strip(" [].:"))
        if folded in _ALIAS_TO_CANONICAL:
            return _ALIAS_TO_CANONICAL[folded]
        # allow "english us" (parentheses stripped above leave trailing token)
        first = folded.split(" ")[0] if folded else ""
        if first in _ALIAS_TO_CANONICAL:
            # only when the remainder looks like a variant tag, not a sentence
            rest = folded[len(first):].strip()
            if not rest or re.fullmatch(r"[a-z]{1,3}", rest):
                return _ALIAS_TO_CANONICAL[first]
        return None

    level_seq: list = []
    lang_seq: list = []
    for line in lines:
        code = _is_level_only(line)
        if code:
            level_seq.append(code)
            continue
        lang = _is_language_only(line)
        if lang:
            lang_seq.append(lang)
    # dedupe repeated language blocks ("EN..FR EN..FR") preserving order
    deduped: list = list(dict.fromkeys(lang_seq))
    if level_seq and len(deduped) == len(level_seq):
        for lang, code in zip(deduped, level_seq):
            result.setdefault(lang, code)
    # 3. proximity fallback for languages seen without a level yet
    if lines:
        full = str(cv_text or "")
        for alias, canon in _ALIAS_TO_CANONICAL.items():
            if canon in result:
                continue
            for m in re.finditer(r"(^|[^a-z])" + re.escape(alias) +
                                 r"([^a-z]|$)", _ascii_fold(full)):
                window = _ascii_fold(full[max(0, m.start() - 120):
                                          m.end() + 120])
                codes = [c.upper() for c in
                         re.findall(r"\b([ABC][12])\b", window,
                                    re.IGNORECASE)]
                if codes:
                    result[canon] = codes[0]
                    break
                if re.search(r"\b(native|muttersprache|mother tongue)\b",
                             window):
                    result[canon] = "NATIVE"
                    break
    return result


def _sibling_language(key: str, by_key: dict, order: list,
                      raw_values: dict, cv_langs: dict) -> str:
    """Language implied by a neighbouring language-name field, if any."""
    try:
        idx = order.index(key)
    except ValueError:
        return ""
    # label number pairing first ("Language 1" <-> "Level 1")
    cur_num = re.search(r"(\d+)\s*$",
                        _ascii_fold(_field_text(by_key.get(key, {}))))
    cur_num = cur_num.group(1) if cur_num else None
    candidates: list = []
    for dist in (1, 2):
        for j in (idx - dist, idx + dist):
            if 0 <= j < len(order):
                candidates.append(order[j])
    # prefer same-numbered sibling
    def _num(k):
        m = re.search(r"(\d+)\s*$",
                      _ascii_fold(_field_text(by_key.get(k, {}))))
        return m.group(1) if m else None
    if cur_num:
        candidates.sort(key=lambda k: 0 if _num(k) == cur_num else 1)
    for other in candidates:
        of = by_key.get(other, {}) or {}
        if not is_language_name_field(of, of.get("options") or []):
            continue
        raw = str((raw_values or {}).get(other, "")).strip()
        canon = _canonical_language(raw) if raw else ""
        if canon and canon in cv_langs:
            return canon
    return ""


def expected_language_option(field: dict, options, cv_text: str,
                             cv_langs: dict, raw_values=None,
                             by_key=None, order=None) -> str | None:
    """Best option for a language field derived from CV evidence, or None."""
    opts = [o for o in (options or []) if not _is_placeholder_option(o)]
    if not opts:
        return None
    field = field or {}
    if is_language_name_field(field, options):
        # handled via synonym mapping of the model's answer; no blind guess
        return None
    if not is_language_level_field(field, options):
        return None
    lang = field_language(field)
    if not lang and by_key is not None and order is not None:
        lang = _sibling_language(field.get("key", ""), by_key, order,
                                 raw_values or {}, cv_langs or {})
    if not lang or not (cv_langs or {}).get(lang):
        return None
    target = (cv_langs or {})[lang]
    if target not in CEFR_ORDER:
        return None
    scored = [(o, _option_level_range(o)) for o in opts]
    scored = [(o, r) for o, r in scored if r is not None]
    if not scored:
        return None
    if target != "NATIVE":
        exact = [o for o, _r in scored
                 if re.search(r"\b" + re.escape(target) + r"\b", o,
                              re.IGNORECASE)]
        if exact:
            return exact[0]
    else:
        native_opts = [o for o, _r in scored if NATIVE_RE.search(o)]
        if native_opts:
            return native_opts[0]
    score = CEFR_ORDER[target]
    containing = [(o, r) for o, r in scored if r[0] <= score <= r[1]]
    if containing:
        containing.sort(key=lambda x: (x[1][1] - x[1][0], -x[1][1]))
        return containing[0][0]
    # closest level (e.g. NATIVE CV with max C2 option -> C2)
    def _dist(r):
        if r[0] <= score <= r[1]:
            return 0
        return min(abs(score - r[0]), abs(score - r[1]))
    scored.sort(key=lambda x: (_dist(x[1]), -x[1][1]))
    return scored[0][0]


def match_select_option(model_value: str, field: dict, options,
                        cv_text: str, cv_langs=None, raw_values=None,
                        by_key=None, order=None) -> str | None:
    """Map a model answer to the correct option, or None if unmapped."""
    opts = list(options or [])
    if not opts:
        return None
    text = str(model_value or "").strip()
    if not text:
        return None
    field = field or {}
    cv_langs = cv_langs if cv_langs is not None else parse_cv_languages(
        cv_text or "")
    folded = {_ascii_fold(o): o for o in opts}
    exact_hit = folded.get(_ascii_fold(text))
    # language-name synonyms: "German" <-> "Deutsch" (needs CV evidence).
    # An exact hit still needs that evidence; otherwise a model guessing a
    # language the CV never mentions would slip through.
    if is_language_name_field(field, opts):
        if exact_hit is not None:
            if _canonical_language(exact_hit) and _language_mentioned(
                    _canonical_language(exact_hit), cv_text or ""):
                return exact_hit
            return None
        canon = _canonical_language(text)
        if canon and _language_mentioned(canon, cv_text or ""):
            for opt in opts:
                if _canonical_language(opt) == canon:
                    return opt
        return None
    if exact_hit is not None and is_language_level_field(field, opts):
        # Accept the exact option only if it is the CV-derived one for
        # that language; otherwise correct it (or reject when the CV has
        # no evidence for the language at all).
        try:
            expected = expected_language_option(
                field, opts, cv_text or "", cv_langs, raw_values,
                by_key, order)
        except Exception:
            expected = None
        return expected
    if exact_hit is not None:
        return exact_hit
    if not is_language_level_field(field, opts):
        return None
    # If the model answered a bare CEFR code verbatim, honour it when the
    # CV backs that language; otherwise fall through to the CV-derived
    # expected option (which corrects a confused model, e.g. English C1
    # copied into the German field).
    codes = [c.upper() for c in CEFR_RE.findall(text)]
    native_hit = bool(NATIVE_RE.search(text)) and not codes
    lang = field_language(field)
    if not lang and by_key is not None and order is not None:
        lang = _sibling_language(field.get("key", ""), by_key, order,
                                 raw_values or {}, cv_langs or {})
    if codes and lang and (cv_langs or {}).get(lang) in codes:
        target = (cv_langs or {})[lang]
        return expected_language_option(
            field, opts, cv_text, {** (cv_langs or {}), lang: target},
            raw_values, by_key, order)
    if codes and _ascii_fold(text) in {c.lower() for c in codes}:
        # bare code like "B2": only trust it with CV evidence somewhere
        joined = " ".join(codes)
        if is_verbatim(joined, cv_text or "") or any(
                is_verbatim(c, cv_text or "") for c in codes):
            scored = [(o, _option_level_range(o)) for o in opts
                      if not _is_placeholder_option(o)]
            scored = [(o, r) for o, r in scored if r]
            for code in codes:
                exact = [o for o, _r in scored
                         if re.search(r"\b" + re.escape(code) + r"\b", o,
                                      re.IGNORECASE)]
                if exact:
                    return exact[0]
    if native_hit and is_verbatim(text, cv_text or ""):
        for opt in opts:
            if NATIVE_RE.search(opt):
                return opt
    # default: the CV-derived option for this language (may correct model)
    return expected_language_option(field, opts, cv_text, cv_langs or {},
                                    raw_values, by_key, order)


def is_verbatim(value: str, cv_text: str) -> bool:
    """True if value is empty or copied verbatim from the CV.

    A single-line value must be a substring. A multi-line / bulleted block
    (e.g. an experience or education description) is accepted when every
    non-empty line appears verbatim in the CV, in order. Each fragment must
    still exist in the CV, so nothing can be hallucinated.
    """
    if value is None:
        return False
    text = str(value).strip()
    if text == "":
        return True
    cv_norm = _normalize(cv_text or "")
    if _normalize(text) in cv_norm:
        return True
    segments = [s for s in (_strip_bullet(p) for p in _BULLET_SPLIT_RE.split(text)) if s]
    if len(segments) <= 1:
        return False
    cursor = 0
    for segment in segments:
        seg_norm = _normalize(segment)
        if not seg_norm:
            continue
        idx = cv_norm.find(seg_norm, cursor)
        if idx == -1:
            return False
        cursor = idx + len(seg_norm)
    return True


def validate_values(raw_values: dict, cv_text: str, fields: list):
    """Enforce verbatim-only + select-option rules. Returns (cleaned, dropped).

    Language-proficiency selects are the controlled exception: a composite
    option such as "B2 (Fortgeschritten)" can never be verbatim when the CV
    only states "B2", so the guard maps CV CEFR -> exact option instead of
    blanking it. Mapping only applies when the field/options look like a
    language level (or language names) and the target comes from CV
    evidence for that language — everything else stays strict verbatim.
    """
    by_key = {f.get("key"): f for f in (fields or []) if f.get("key")}
    order = [f.get("key") for f in (fields or []) if f.get("key")]
    raw_values = raw_values or {}
    try:
        cv_langs = parse_cv_languages(cv_text or "")
    except Exception:
        cv_langs = {}
    cleaned: dict = {}
    dropped: list = []
    for key in by_key:
        value = raw_values.get(key, "")
        value = "" if value is None else str(value).strip()
        field = by_key[key] or {}
        # make sibling lookup (split language/level pairs) work
        if not field.get("key"):
            field = {**field, "key": key}
        options = field.get("options") or []
        if value == "":
            auto = None
            try:
                auto = expected_language_option(
                    field, options, cv_text or "", cv_langs,
                    raw_values, by_key, order)
            except Exception:
                auto = None
            if auto:
                cleaned[key] = auto
            else:
                cleaned[key] = ""
            continue
        if options:
            if value in options:
                if is_verbatim(value, cv_text):
                    cleaned[key] = value
                    continue
                # Exact option but not verbatim: accept only if it is the
                # CV-derived language option (e.g. "B2 (Fortgeschritten)"
                # for a CV German B2). This also fixes the "Professional"
                # false positive where a bare word matches CV boilerplate
                # ("PROFESSIONAL EXPERIENCE") without proving the level.
                try:
                    expected = expected_language_option(
                        field, options, cv_text or "", cv_langs,
                        raw_values, by_key, order)
                except Exception:
                    expected = None
                if expected is not None and expected == value:
                    cleaned[key] = value
                    continue
                # A correctable language field: fall back to the CV-derived
                # option instead of leaving it empty (still CV-grounded).
                if expected is not None and is_language_level_field(
                        field, options):
                    cleaned[key] = expected
                    continue
                # Language-name synonym ("German" answered, option is
                # "Deutsch") with CV evidence.
                try:
                    mapped = match_select_option(
                        value, field, options, cv_text or "", cv_langs,
                        raw_values, by_key, order)
                except Exception:
                    mapped = None
                if mapped == value:
                    cleaned[key] = value
                    continue
                cleaned[key] = ""
                dropped.append(key)
                continue
            # not an exact option: try deterministic mapping (bare "B2" ->
            # "B2 (Fortgeschritten)", "German" -> "Deutsch", ...)
            try:
                mapped = match_select_option(
                    value, field, options, cv_text or "", cv_langs,
                    raw_values, by_key, order)
            except Exception:
                mapped = None
            if mapped:
                cleaned[key] = mapped
                continue
            cleaned[key] = ""
            dropped.append(key)
            continue
        if not is_verbatim(value, cv_text):
            cleaned[key] = ""
            dropped.append(key)
            continue
        cleaned[key] = value
    return cleaned, dropped


def build_messages(cv_text: str, fields: list, model: str) -> dict:
    """Build an OpenAI-compatible chat payload with temperature 0."""
    slim_fields = [
        {
            "key": f.get("key", ""),
            "label": f.get("label", ""),
            "name": f.get("name", ""),
            "type": f.get("type", "text"),
            **({"context": f["context"]} if f.get("context") else {}),
            **({"options": f["options"]} if f.get("options") else {}),
        }
        for f in fields
    ]
    user_content = (
        "CV TEXT (only source of truth):\n"
        "-----\n"
        f"{cv_text}\n"
        "-----\n\n"
        "FORM FIELDS (JSON):\n"
        f"{json.dumps(slim_fields, ensure_ascii=False)}\n\n"
        "Each field's optional 'context' names its CV section; match it to that "
        "section. For a description/responsibilities field, return EVERY matching "
        "line from that section exactly as written (keep the leading '- ' bullets "
        "and one per line). Do NOT summarise, reword or merge lines.\n"
        "Language selects: options may combine a CEFR code with a description "
        "('B2 (Fortgeschritten)') or use only words (native/fluent/professional/"
        "intermediate/basic/Grundkenntnisse/Muttersprache/Keine). Always return "
        "the EXACT option text, chosen by the CV's CEFR level for the language "
        "named in the field label (e.g. 'Deutsch-Kenntnisse' uses the CV's "
        "German level; CV German B2 -> the option containing 'B2'). If level "
        "is a separate field next to a language field, it belongs to that "
        "language.\n"
        "Return ONLY JSON: {\"values\": {\"<key>\": \"<exact CV substring or empty string>\"}}. "
        "Every non-empty value MUST appear verbatim in the CV text above, "
        "except language-proficiency selects where you return the exact "
        "option text for the CV's CEFR level of that language. "
        "If unsure, use an empty string."
    )
    return {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }


def _extract_json_object(text: str) -> dict:
    """Pull the first {...} JSON object out of model output (strips code fences)."""
    cleaned = re.sub(r"```(?:json)?|```", "", text or "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object in model output")
    return json.loads(cleaned[start:end + 1])


@app.get("/api/health")
def health():
    stored = load_settings()
    configured = bool(stored["base_url"] and stored["model"]
                      and stored["cv_text"].strip())
    return jsonify({"status": "ok", "configured": configured})


@app.post("/api/parse-pdf")
def parse_pdf():
    if "file" not in request.files:
        return jsonify({"error": "Missing 'file' upload"}), 400
    f = request.files["file"]
    filename = (f.filename or "").lower()
    if not filename.endswith(".pdf"):
        return jsonify({"error": "Only .pdf files are accepted"}), 400
    data = f.read()
    if not data or len(data) > MAX_PDF_BYTES:
        return jsonify({"error": "PDF is empty or exceeds 10MB"}), 413
    if not data.startswith(b"%PDF"):
        return jsonify({"error": "Not a valid PDF file"}), 400
    try:
        reader = PdfReader(BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n".join(pages).strip()
    except Exception as exc:  # invalid/corrupt PDF
        return jsonify({"error": f"Could not parse PDF: {exc}"}), 400
    return jsonify({"text": text})


@app.post("/api/suggest-fill")
def suggest_fill():
    body = request.get_json(silent=True) or {}
    cv_text = (body.get("cv_text") or "").strip()
    fields = body.get("fields") or []
    provider = body.get("provider") or {}
    stored = load_settings()
    base_url = ((provider.get("base_url") or "").strip()
                or stored["base_url"]).rstrip("/")
    model = (provider.get("model") or "").strip() or stored["model"]
    api_key = (provider.get("api_key") or "").strip() or get_api_key()
    # The extension may omit the CV and rely on the one saved in settings.json.
    if not cv_text:
        cv_text = stored["cv_text"].strip()

    if not cv_text:
        return jsonify({"error": "cv_text is required (save your CV in the "
                                 "localhost:5000 dashboard)"}), 400
    if not isinstance(fields, list) or not fields:
        return jsonify({"error": "fields must be a non-empty list"}), 400
    if len(fields) > MAX_FIELDS:
        return jsonify({"error": f"Too many fields (max {MAX_FIELDS})"}), 400
    try:
        base_url = validate_provider(base_url, model)
    except ValueError as exc:
        return jsonify({"error": f"Invalid provider: {exc}"}), 400
    if not api_key and not is_local_provider(base_url):
        return jsonify({"error": missing_key_message(base_url)}), 400

    payload = build_messages(cv_text, fields, model)
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = requests.post(f"{base_url}/chat/completions",
                             headers=headers, json=payload, timeout=90)
    except requests.RequestException as exc:
        return jsonify({"error": f"LLM request failed: {exc}"}), 502
    if resp.status_code != 200:
        detail = ""
        try:
            detail = json.dumps(resp.json())[:500]
        except Exception:
            detail = (resp.text or "")[:500]
        return jsonify({"error": f"LLM error {resp.status_code}: {detail}"}), 502
    try:
        content = resp.json()["choices"][0]["message"]["content"]
        parsed = _extract_json_object(content)
        raw_values = parsed.get("values", {})
        if not isinstance(raw_values, dict):
            raise ValueError("'values' must be an object")
    except Exception as exc:
        return jsonify({"error": f"Could not parse model output: {exc}"}), 502

    cleaned, dropped = validate_values(raw_values, cv_text, fields)
    return jsonify({"values": cleaned, "dropped": dropped})


@app.get("/api/settings")
def get_settings():
    return jsonify(public_settings())


@app.post("/api/settings")
def post_settings():
    try:
        data = save_settings(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(data)


@app.post("/api/test-provider")
def test_provider():
    """Lightweight connectivity check: backend -> LLM with a trivial prompt."""
    body = request.get_json(silent=True) or {}
    stored = load_settings()
    base_url = (str(body.get("base_url") or "").strip()
                or stored["base_url"]).rstrip("/")
    model = str(body.get("model") or "").strip() or stored["model"]
    api_key = str(body.get("api_key") or "").strip() or get_api_key()
    try:
        base_url = validate_provider(base_url, model)
    except ValueError as exc:
        return jsonify({"error": f"Invalid provider: {exc}"}), 400
    if not api_key and not is_local_provider(base_url):
        return jsonify({"error": missing_key_message(base_url)}), 400
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "temperature": 0,
        "stream": False,
        "max_tokens": 5,
        "messages": [{"role": "user",
                      "content": "Reply with exactly: ok"}],
    }
    try:
        resp = requests.post(f"{base_url}/chat/completions",
                             headers=headers, json=payload, timeout=60)
    except requests.RequestException as exc:
        return jsonify({"error": f"LLM request failed: {exc}"}), 502
    if resp.status_code != 200:
        detail = ""
        try:
            detail = json.dumps(resp.json())[:500]
        except Exception:
            detail = (resp.text or "")[:500]
        return jsonify({"error": f"LLM error {resp.status_code}: {detail}"}), 502
    try:
        reply = resp.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        return jsonify({"error": f"Could not parse model output: {exc}"}), 502
    return jsonify({"ok": True, "reply": str(reply)[:50]})


@app.get("/")
def dashboard():
    return render_template("index.html")


def strip_api_key_from_file(path: str = None) -> bool:
    """Remove a legacy plaintext api_key from settings.json (one-time cleanup).

    The key is moved into memory for the current run (unless CVFILL_API_KEY is
    already set) and then dropped from disk. Returns True if the file changed.
    """
    path = path or settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict) or not raw.get("api_key"):
        return False
    if not get_api_key():
        set_api_key(raw.get("api_key"))
    raw.pop("api_key", None)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def main() -> None:
    """Start the dev server. Honors the PORT env var (default 5000)."""
    strip_api_key_from_file()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
