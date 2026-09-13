# CV Job Form Filler (Firefox, verbatim-only)

Fill job forms on any website by **copy-pasting verbatim info from your CV** using a local Ollama model or any OpenAI-compatible API (`base_url` + model name). Preview every value before anything touches the page.

## How it works

- **Firefox extension** (`extension/`): minimal popup — scan, fill, optional debug view — and a content script that scans/fills only on your click. Provider + CV are configured in the backend dashboard.
- **Flask backend** (`backend/`): parses CV PDFs, stores the provider + CV (`settings.json`), and proxies LLM calls with a strict anti-hallucination guard. The extension never talks to the LLM directly.

## Anti-hallucination design

1. System prompt forces **exact-substring copying**, temperature `0`, empty string when unsure, JSON-only output.
2. Backend `validate_values()` blanks any value that is not a verbatim substring of the CV (multi-line descriptions are accepted when every line is verbatim, in order), and rejects select options not in the field's option list.
3. The backend is the single source of truth: the extension sends only scanned fields, so every model value is guarded before it ever reaches the popup.
4. Passwords, file uploads, hidden fields, and CAPTCHAs are never filled.

## Ban-safe behavior

- Content script is **passive**: nothing happens on page load.
- **Scan** and **Fill** only run after your explicit button clicks.
- Filling uses normal `focus`/`input`/`change` events with a small delay between fields.
- The extension **never auto-submits** forms or clicks submit/apply buttons — you review and submit manually.
- Only data flow: page field labels → your backend → your configured LLM. No tracking, no third parties.

## Data flow / privacy (verified)

- The content script makes **no network requests at all** — filling is local DOM
  manipulation. It never calls the job site's APIs.
- The extension only opens connections to your local Flask backend
  (`http://127.0.0.1:5000` / `http://localhost:5000`); this is enforced by both
  the manifest host permissions and a restrictive extension `connect-src` CSP.
- The backend forwards to **only** the `base_url` you configure (your LLM
  provider). `base_url` must be a plain `http(s)` URL; `file://`, credential
  URLs, etc. are rejected to prevent SSRF/open-proxy abuse.
- Backend CORS is locked to the extension origin (`moz-extension://…`) and the
  local dashboard, so a website you visit cannot read `/api/settings` (your CV)
  or drive the backend.
- **The API key is never written to `settings.json`.** It is read at startup
  from `backend/.env` (git-ignored) or the process environment —
  `CVFILL_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` or `LLM_API_KEY`.
  The dashboard can override it for the current session only.
- `extension/tests/test_network_safety.test.js` and the CORS/base-url tests in
  `backend/test_app.py` guard these properties against regressions.


## Setup (you run the backend in your own venv — the agent never starts it)

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py   # serves http://127.0.0.1:5000
```

Ollama example (OpenAI-compatible endpoint):

```bash
ollama pull llama3.1
ollama serve  # allows http://localhost:11434/v1
```

If the browser can't reach Ollama directly, the Flask backend proxies it, so no CORS setup is needed.

## Backend dashboard (settings in the browser)

Open **http://127.0.0.1:5000/** while the backend runs: set the provider
`base_url` + model, paste/upload the CV, and use **Test connection**.
`base_url`, model and CV are saved to `backend/settings.json` (localhost only,
git-ignored). **The API key is never saved there** — put it in `backend/.env`
(also git-ignored), which the backend loads at startup:

```bash
# backend/.env
OPENROUTER_API_KEY=sk-or-...
# or a generic name:
CVFILL_API_KEY=sk-...
```

If a remote provider has no key, the backend refuses to call it and tells you to
add the entry to `backend/.env`. Local providers such as Ollama need no key.

The dashboard has a **dark/light theme toggle** (follows your system preference
by default). This is the only place you configure things: the extension syncs
the provider + CV from the backend and sends just the scanned fields.

## Load the extension in Firefox

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on**, select `extension/manifest.json`.
3. Configure the provider and CV once on **http://127.0.0.1:5000/** (gear
   button in the popup opens it), then use the popup on a job page:
   - **Scan this page** — finds the fields and asks the model for verbatim values in one go.
   - **Fill form** — fills every selected, non-empty value; then submit manually.
   - **Show fields** (debug, collapsed by default) — inspect/edit the scanned
     fields, tick/untick which ones to fill, and see dropped or skipped fields.
   - File uploads, passwords, and CAPTCHAs are listed under “Not filled automatically” — handle those manually.

## Troubleshooting “empty suggestions / nothing to fill”

1. Status messages now stick to the **top** of the popup — read them first.
2. Click **Test connection**: backend unreachable → start it (`python app.py` in `backend/` with venv active); model failing → `ollama serve` + `ollama pull <model>`.
3. Empty CV box → Suggest refuses. If PDF parsing yields no text, the PDF is likely scanned images — paste the text manually (or via the dashboard).
4. “Found nothing verbatim” with CV loaded → the CV genuinely lacks those answers; type them in and Fill (hand-typed values are allowed).

## Tests (TDD)

```bash
# backend (from repo root or backend/)
python3 -m pytest backend/test_app.py -v
# extension unit tests (no dependencies)
node --test extension/tests/test_content.test.js extension/tests/test_network_safety.test.js
```

Do **not** run the Flask server as part of tests; start it yourself in your venv when you want to use the extension.
