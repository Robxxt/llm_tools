"""TDD tests for Flask backend.

Covers:
- health check
- PDF parsing (missing/invalid/valid)
- suggest-fill validation + strict anti-hallucination prompt + verbatim guard
"""
import io
import json
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

import pytest

from app import (
    app,
    build_messages,
    is_verbatim,
    main,
    validate_provider,
    validate_values,
    SYSTEM_PROMPT,
)


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


# ---------- health ----------

def test_health(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


# ---------- parse-pdf ----------

def test_parse_pdf_missing_file(client):
    resp = client.post("/api/parse-pdf")
    assert resp.status_code == 400


def test_parse_pdf_rejects_non_pdf(client):
    data = {"file": (io.BytesIO(b"not a pdf"), "cv.txt")}
    resp = client.post("/api/parse-pdf", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_parse_pdf_extracts_text(client):
    # Build a minimal real PDF with pypdf so we don't hand-craft bytes.
    from pypdf import PdfWriter
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    # blank page has no text -> backend should return "" not crash
    w.write(buf)
    buf.seek(0)
    data = {"file": (buf, "cv.pdf")}
    resp = client.post("/api/parse-pdf", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "text" in body
    assert isinstance(body["text"], str)


def test_parse_pdf_rejects_too_large(client):
    big = io.BytesIO(b"%PDF-1.4 fake" + b"x" * (11 * 1024 * 1024))
    data = {"file": (big, "cv.pdf")}
    resp = client.post("/api/parse-pdf", data=data,
                       content_type="multipart/form-data")
    assert resp.status_code in (400, 413)


# ---------- suggest-fill validation ----------

def test_suggest_fill_requires_cv_text(client, isolated_settings):
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "",
        "fields": [{"key": "a", "label": "Full name"}],
        "provider": {"base_url": "http://localhost:11434/v1",
                     "model": "llama3"},
    })
    assert resp.status_code == 400


def test_suggest_fill_requires_fields(client):
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "John Doe",
        "fields": [],
        "provider": {"base_url": "http://localhost:11434/v1",
                     "model": "llama3"},
    })
    assert resp.status_code == 400


def test_suggest_fill_requires_provider(client, isolated_settings):
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "John Doe",
        "fields": [{"key": "a", "label": "Full name"}],
        "provider": {"base_url": "", "model": ""},
    })
    assert resp.status_code == 400


# ---------- anti-hallucination prompt ----------

def test_system_prompt_forbids_hallucination():
    p = SYSTEM_PROMPT.lower()
    assert "exact" in p or "verbatim" in p or "copy" in p
    assert "empty string" in p
    assert "do not invent" in p or "never invent" in p or "do not hallucinate" in p


def test_build_messages_uses_temperature_zero_and_strict_rules():
    cv = "Jane Smith\njane@example.com"
    fields = [{"key": "f0", "label": "Email", "name": "email",
               "type": "email"}]
    payload = build_messages(cv, fields, model="llama3")
    assert payload["temperature"] == 0
    text = json.dumps(payload["messages"]).lower()
    assert "empty string" in text
    assert "jane@example.com" in payload["messages"][1]["content"]


def test_build_messages_forwards_field_context():
    fields = [{"key": "f0", "label": "Description", "type": "textarea",
               "context": "Education"}]
    payload = build_messages("some cv", fields, model="llama3")
    content = payload["messages"][1]["content"]
    assert "\"context\": \"Education\"" in content


# ---------- verbatim guard ----------

def test_is_verbatim_accepts_exact_substring():
    cv = "John Doe\njohn.doe@example.com\n+1 555-0100"
    assert is_verbatim("John Doe", cv) is True
    assert is_verbatim("john.doe@example.com", cv) is True


def test_is_verbatim_rejects_hallucination():
    cv = "John Doe\njohn.doe@example.com"
    assert is_verbatim("Jane Smith", cv) is False
    assert is_verbatim("john.doe@gmail.com", cv) is False
    assert is_verbatim("Senior Astronaut at NASA", cv) is False


def test_is_verbatim_empty_is_allowed():
    assert is_verbatim("", "anything") is True


def test_is_verbatim_accepts_multiline_bullet_block_in_order():
    cv = (
        "PROFESSIONAL EXPERIENCE\n"
        "- Designed and built a custom framework.\n"
        "- Automated the documentation process.\n"
        "EDUCATION\n"
    )
    block = ("- Designed and built a custom framework.\n"
             "- Automated the documentation process.")
    assert is_verbatim(block, cv) is True


def test_is_verbatim_rejects_invented_bullet():
    cv = "- Designed a framework.\n- Automated documentation."
    block = "- Designed a framework.\n- Led a team of astronauts."
    assert is_verbatim(block, cv) is False


def test_is_verbatim_rejects_reordered_bullets():
    cv = "- First thing.\n- Second thing."
    block = "- Second thing.\n- First thing."
    assert is_verbatim(block, cv) is False


def test_validate_values_keeps_multiline_description():
    cv = ("EXPERIENCE\n- Built thing A.\n- Built thing B.\n")
    fields = [{"key": "f0", "label": "Description", "type": "textarea"}]
    cleaned, dropped = validate_values(
        {"f0": "- Built thing A.\n- Built thing B."}, cv, fields)
    assert cleaned["f0"] == "- Built thing A.\n- Built thing B."
    assert dropped == []


def test_validate_values_drops_hallucinations():
    cv = "John Doe\njohn.doe@example.com"
    fields = [
        {"key": "f0", "label": "Full name"},
        {"key": "f1", "label": "Email"},
    ]
    raw = {"f0": "John Doe", "f1": "hacker@evil.com"}
    cleaned, dropped = validate_values(raw, cv, fields)
    assert cleaned["f0"] == "John Doe"
    assert cleaned["f1"] == ""
    assert "f1" in dropped


def test_validate_values_enforces_select_options():
    cv = "Work auth: EU citizen"
    fields = [{"key": "f0", "label": "Work auth",
               "type": "select", "options": ["Yes", "No"]}]
    cleaned, dropped = validate_values({"f0": "Maybe"}, cv, fields)
    assert cleaned["f0"] == ""
    assert "f0" in dropped


def test_validate_values_keeps_valid_select_option_supported_by_cv():
    cv = "Work authorization: Yes, EU citizen"
    fields = [{"key": "f0", "label": "Work auth",
               "type": "select", "options": ["Yes", "No"]}]
    cleaned, dropped = validate_values({"f0": "Yes"}, cv, fields)
    assert cleaned["f0"] == "Yes"
    assert dropped == []


# ---------- suggest-fill LLM integration (mocked) ----------

def test_suggest_fill_returns_verbatim_only_values(client, monkeypatch):
    cv = "John Doe\njohn.doe@example.com"

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "values": {"f0": "John Doe", "f1": "invented@fake.com"}})}}]}

    import app as appmod
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: FakeResp())

    resp = client.post("/api/suggest-fill", json={
        "cv_text": cv,
        "fields": [{"key": "f0", "label": "Full name"},
                   {"key": "f1", "label": "Email"}],
        "provider": {"base_url": "http://localhost:11434/v1",
                     "model": "llama3"},
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["values"]["f0"] == "John Doe"
    # hallucinated email must be blanked by the guard
    assert body["values"]["f1"] == ""
    assert "f1" in body["dropped"]


def test_suggest_fill_handles_llm_error(client, monkeypatch):
    import app as appmod
    import requests as req

    def boom(*a, **k):
        raise req.ConnectionError("down")

    monkeypatch.setattr(appmod.requests, "post", boom)
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "John Doe",
        "fields": [{"key": "f0", "label": "Full name"}],
        "provider": {"base_url": "http://localhost:11434/v1",
                     "model": "llama3"},
    })
    assert resp.status_code == 502


# ---------- runnable entrypoint ----------

def test_main_starts_server_on_localhost_default_port(monkeypatch):
    import app as appmod
    monkeypatch.delenv("PORT", raising=False)
    calls = {}
    monkeypatch.setattr(appmod.app, "run",
                        lambda *a, **k: calls.update({"args": a, "kwargs": k}))
    main()
    assert calls["kwargs"].get("host") == "127.0.0.1"
    assert calls["kwargs"].get("port") == 5000


def test_main_respects_port_env_var(monkeypatch):
    import app as appmod
    monkeypatch.setenv("PORT", "8000")
    calls = {}
    monkeypatch.setattr(appmod.app, "run",
                        lambda *a, **k: calls.update({"args": a, "kwargs": k}))
    main()
    assert calls["kwargs"].get("port") == 8000


# ---------- settings store + dashboard ----------

@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CVFILL_SETTINGS",
                       str(tmp_path / "settings.json"))
    return tmp_path / "settings.json"


def test_settings_get_returns_empty_shape(client, isolated_settings):
    resp = client.get("/api/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"base_url": "", "model": "",
                    "api_key": "", "cv_text": ""}


def test_settings_post_roundtrip(client, isolated_settings):
    payload = {"base_url": "http://localhost:11434/v1",
               "model": "llama3.1", "api_key": "",
               "cv_text": "Jane Smith\njane@example.com"}
    resp = client.post("/api/settings", json=payload)
    assert resp.status_code == 200
    assert client.get("/api/settings").get_json() == payload
    # persisted to disk, not just memory
    assert json.loads(isolated_settings.read_text()) == payload


def test_settings_post_rejects_missing_provider(client, isolated_settings):
    resp = client.post("/api/settings", json={"base_url": "", "model": ""})
    assert resp.status_code == 400


def test_settings_post_ignores_unknown_keys(client, isolated_settings):
    resp = client.post("/api/settings", json={
        "base_url": "http://x/v1", "model": "m", "evil": "1"})
    assert resp.status_code == 200
    assert "evil" not in resp.get_json()


def test_suggest_fill_uses_stored_provider_when_omitted(
        client, isolated_settings, monkeypatch):
    client.post("/api/settings", json={
        "base_url": "http://stored:8080/v1", "model": "stored-model",
        "api_key": "", "cv_text": ""})
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "values": {"f0": "John Doe"}})}}]}

    import app as appmod

    def fake_post(url, **kwargs):
        seen["url"] = url
        seen["model"] = kwargs["json"]["model"]
        return FakeResp()

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "John Doe",
        "fields": [{"key": "f0", "label": "Full name"}],
        "provider": {"base_url": "", "model": ""},
    })
    assert resp.status_code == 200
    assert seen["url"] == "http://stored:8080/v1/chat/completions"
    assert seen["model"] == "stored-model"


def test_suggest_fill_uses_stored_cv_when_omitted(
        client, isolated_settings, monkeypatch):
    client.post("/api/settings", json={
        "base_url": "http://stored:8080/v1", "model": "stored-model",
        "api_key": "", "cv_text": "Jane Smith\njane@example.com"})
    seen = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "values": {"f0": "Jane Smith"}})}}]}

    import app as appmod

    def fake_post(url, **kwargs):
        seen["content"] = kwargs["json"]["messages"][1]["content"]
        return FakeResp()

    monkeypatch.setattr(appmod.requests, "post", fake_post)
    resp = client.post("/api/suggest-fill", json={
        "fields": [{"key": "f0", "label": "Full name"}],
        "provider": {"base_url": "http://stored:8080/v1",
                     "model": "stored-model"},
    })
    assert resp.status_code == 200
    assert resp.get_json()["values"]["f0"] == "Jane Smith"
    assert "jane@example.com" in seen["content"]


def test_health_reports_configured_state(client, isolated_settings):
    assert client.get("/api/health").get_json()["configured"] is False
    client.post("/api/settings", json={
        "base_url": "http://stored:8080/v1", "model": "m",
        "api_key": "", "cv_text": "Jane Smith"})
    assert client.get("/api/health").get_json()["configured"] is True


def test_dashboard_serves_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "CV Job Form Filler" in html
    assert "/api/settings" in html
    # dark/light theme toggle is wired up
    assert 'id="themeToggle"' in html
    assert "data-theme" in html
    assert "prefers-color-scheme: dark" in html


# ---------- provider connection test ----------

def test_test_provider_ok(client, monkeypatch):
    import app as appmod

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: FakeResp())
    resp = client.post("/api/test-provider", json={
        "base_url": "http://localhost:11434/v1", "model": "llama3.1"})
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_test_provider_requires_model(client, isolated_settings):
    resp = client.post("/api/test-provider", json={"base_url": "http://x"})
    assert resp.status_code == 400


def test_test_provider_connection_error(client, monkeypatch):
    import app as appmod
    import requests as req

    def boom(*a, **k):
        raise req.ConnectionError("refused")

    monkeypatch.setattr(appmod.requests, "post", boom)
    resp = client.post("/api/test-provider", json={
        "base_url": "http://localhost:11434/v1", "model": "llama3.1"})
    assert resp.status_code == 502


# ---------- CORS lockdown ----------

def test_cors_denies_arbitrary_web_origin(client):
    """A random website must not be able to read /api/settings."""
    resp = client.get("/api/settings", headers={"Origin": "https://evil.example"})
    assert resp.status_code == 200
    assert "Access-Control-Allow-Origin" not in resp.headers


def test_cors_allows_extension_origin(client):
    origin = "moz-extension://abc-123"
    resp = client.get("/api/health", headers={"Origin": origin})
    assert resp.headers.get("Access-Control-Allow-Origin") == origin


def test_cors_does_not_send_wildcard_for_unknown_origin(client):
    resp = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert resp.headers.get("Access-Control-Allow-Origin") != "*"


# ---------- provider base_url validation ----------

def test_validate_provider_accepts_http_and_strips_slash():
    assert validate_provider("http://localhost:11434/v1/", "m") == \
        "http://localhost:11434/v1"
    assert validate_provider("https://api.openai.com/v1", "m") == \
        "https://api.openai.com/v1"


@pytest.mark.parametrize("bad", [
    "",
    "ftp://example.com",
    "file:///etc/passwd",
    "gopher://example.com",
    "http://",
    "http://user:pass@example.com/v1",
])
def test_validate_provider_rejects_bad_urls(bad):
    with pytest.raises(ValueError):
        validate_provider(bad, "m")


def test_validate_provider_requires_model():
    with pytest.raises(ValueError):
        validate_provider("http://localhost:11434/v1", "")


def test_validate_provider_host_allowlist(monkeypatch):
    monkeypatch.setenv("CVFILL_ALLOWED_PROVIDER_HOSTS", "localhost")
    assert validate_provider("http://localhost:11434/v1", "m") == \
        "http://localhost:11434/v1"
    with pytest.raises(ValueError):
        validate_provider("https://api.openai.com/v1", "m")


def test_suggest_fill_rejects_bad_provider_without_calling_llm(client, monkeypatch):
    import app as appmod
    called = {}
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: called.setdefault("hit", True))
    resp = client.post("/api/suggest-fill", json={
        "cv_text": "John Doe",
        "fields": [{"key": "f0", "label": "Name"}],
        "provider": {"base_url": "file:///etc/passwd", "model": "m"},
    })
    assert resp.status_code == 400
    assert "hit" not in called


def test_test_provider_rejects_bad_provider_without_calling_llm(client, monkeypatch):
    import app as appmod
    called = {}
    monkeypatch.setattr(appmod.requests, "post",
                        lambda *a, **k: called.setdefault("hit", True))
    resp = client.post("/api/test-provider", json={
        "base_url": "ftp://example.com", "model": "m"})
    assert resp.status_code == 400
    assert "hit" not in called


def test_settings_post_rejects_bad_base_url(client, isolated_settings):
    resp = client.post("/api/settings", json={
        "base_url": "file:///etc/passwd", "model": "m",
        "api_key": "", "cv_text": ""})
    assert resp.status_code == 400
