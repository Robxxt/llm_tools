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
    "matching entry; do not copy from a different section."
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
    """Enforce verbatim-only + select-option rules. Returns (cleaned, dropped)."""
    by_key = {f.get("key"): f for f in (fields or []) if f.get("key")}
    cleaned: dict = {}
    dropped: list = []
    for key in by_key:
        value = raw_values.get(key, "")
        value = "" if value is None else str(value).strip()
        field = by_key[key] or {}
        options = field.get("options") or []
        if value == "":
            cleaned[key] = ""
            continue
        if options and value not in options:
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
        "Return ONLY JSON: {\"values\": {\"<key>\": \"<exact CV substring or empty string>\"}}. "
        "Every non-empty value MUST appear verbatim in the CV text above. "
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
