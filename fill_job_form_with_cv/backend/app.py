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
SETTINGS_KEYS = ("base_url", "model", "api_key", "cv_text")

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
    "{\"values\": {\"<field_key>\": \"<exact CV substring or empty string>\"}}."
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


def save_settings(payload: dict) -> dict:
    """Validate and persist settings. Only known keys are stored."""
    if not isinstance(payload, dict):
        raise ValueError("Settings must be a JSON object")
    base_url = str(payload.get("base_url", "")).strip()
    model = str(payload.get("model", "")).strip()
    api_key = payload.get("api_key", "")
    cv_text = payload.get("cv_text", "")
    if not base_url or not model:
        raise ValueError("base_url and model are required")
    if not isinstance(api_key, str) or not isinstance(cv_text, str):
        raise ValueError("api_key and cv_text must be strings")
    base_url = validate_provider(base_url, model)
    data = {"base_url": base_url, "model": model,
            "api_key": api_key, "cv_text": cv_text}
    path = settings_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return data


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


def is_verbatim(value: str, cv_text: str) -> bool:
    """True if value is empty or appears verbatim (whitespace-insensitive) in the CV."""
    if value is None:
        return False
    if str(value).strip() == "":
        return True
    return _normalize(str(value)) in _normalize(cv_text or "")


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
        "Return ONLY JSON: {\"values\": {\"<key>\": \"<exact CV substring or empty string>\"}}. "
        "Every non-empty value MUST be an exact substring of the CV text above. "
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
    return jsonify({"status": "ok"})


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
    api_key = (provider.get("api_key") or "").strip() or stored["api_key"]

    if not cv_text:
        return jsonify({"error": "cv_text is required"}), 400
    if not isinstance(fields, list) or not fields:
        return jsonify({"error": "fields must be a non-empty list"}), 400
    if len(fields) > MAX_FIELDS:
        return jsonify({"error": f"Too many fields (max {MAX_FIELDS})"}), 400
    try:
        base_url = validate_provider(base_url, model)
    except ValueError as exc:
        return jsonify({"error": f"Invalid provider: {exc}"}), 400

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
    return jsonify(load_settings())


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
    api_key = str(body.get("api_key") or "").strip() or stored["api_key"]
    try:
        base_url = validate_provider(base_url, model)
    except ValueError as exc:
        return jsonify({"error": f"Invalid provider: {exc}"}), 400
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


def main() -> None:
    """Start the dev server. Honors the PORT env var (default 5000)."""
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
