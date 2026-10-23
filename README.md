# Tri9T AI — CT-200 Document Traceability API

A time-boxed backend MVP for parsing the supplied CT-200 PDF manuals, persisting a versioned node tree, pinning selections to source text, generating QA test-case ideas, and exposing source staleness.

## Setup
Python 3.11+ is recommended. From the project directory:

```bash
python -m venv .venv
# Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` if configuring an LLM provider. Without `OPENAI_API_KEY`, the API uses a deterministic fallback explicitly labelled `mock-fallback`; this is for pipeline testing, not real LLM generation.

Place the provided files at:
- `data/ct200_manual.pdf`
- `data/ct200_manual_v2.pdf`

The default database is `ct200.db` in this project directory. Set `DATABASE_URL` to use another SQLAlchemy-supported database URL; SQLite is the tested configuration. Generated cases and their immutable source snapshots are stored as JSON documents in SQLite `TEXT` columns alongside the relational document/node records. MongoDB is not implemented; SQLite JSON documents are the deliberately small local fallback so the assignment runs without another service.

### Environment variables
- `DATABASE_URL`: SQLAlchemy URL, default `sqlite:///.../ct200.db`.
- `OPENAI_API_KEY`: optional API key. If absent, generation is explicitly labelled `mock-fallback`.
- `OPENAI_BASE_URL`: optional OpenAI-compatible chat completions URL.
- `OPENAI_MODEL`: optional model name, default `gpt-4o-mini`.

No `.env` file is loaded automatically; export variables in the shell or configure them in the process environment. Never commit credentials.

## Run
```bash
uvicorn app.main:app --reload
```
Open http://127.0.0.1:8000/docs.

## Versioning workflow
Use this order so the v1 snapshot and its generation exist before v2 is ingested:

1. `POST /documents/ingest` with `{"filename":"ct200_manual.pdf"}`. Ingesting another file appends a version; it does not overwrite earlier rows.
2. `GET /documents/ct200/sections?version=1` lists top-level headings; `GET /nodes/search?q=Battery&version=1` finds matching text and returns version-specific node IDs.
3. `POST /selections` with `{"name":"Battery requirement","node_ids":["<v1-node-id>"]}`. A selection must contain nodes from exactly one document version.
4. `POST /selections/{selection_id}/generate` creates a new generation each time. Output is Pydantic-validated and its source references must belong to the pinned selection.
5. Ingest v2 with `{"filename":"ct200_manual_v2.pdf"}`.
6. `GET /documents/ct200/changes?from_version=1&to_version=2` reports added, removed, changed, and unchanged logical nodes.
7. `GET /generations/{generation_id}` returns the original source snapshots and `staleness`; list generations with `GET /generations?selection_id=...` or `GET /generations?node_id=...`.

Example PowerShell commands (run from the project directory with the virtualenv active):

```powershell
uvicorn app.main:app --reload
```

Then use the interactive API at `http://127.0.0.1:8000/docs`, or run requests from another PowerShell window:

```powershell
$base = 'http://127.0.0.1:8000'
$v1 = Invoke-RestMethod -Method Post -Uri "$base/documents/ingest" -ContentType 'application/json' -Body '{"filename":"ct200_manual.pdf"}'
$battery = Invoke-RestMethod -Uri "$base/nodes/search?q=300%20measurement%20cycles&version=1"
$selection = Invoke-RestMethod -Method Post -Uri "$base/selections" -ContentType 'application/json' -Body (@{name='Battery v1'; node_ids=@($battery[0].id)} | ConvertTo-Json)
$generation = Invoke-RestMethod -Method Post -Uri "$base/selections/$($selection.selection_id)/generate"
$v2 = Invoke-RestMethod -Method Post -Uri "$base/documents/ingest" -ContentType 'application/json' -Body '{"filename":"ct200_manual_v2.pdf"}'
Invoke-RestMethod -Uri "$base/documents/ct200/changes?from_version=1&to_version=2"
Invoke-RestMethod -Uri "$base/generations/$($generation.generation_id)"
```

Example:
```bash
curl -X POST http://127.0.0.1:8000/documents/ingest -H "Content-Type: application/json" -d "{\"filename\":\"ct200_manual.pdf\"}"
```

## Important limitations
- The parser uses PyMuPDF native text first and optional OCR only when a page has under 30 extracted characters. The supplied PDFs were text-extractable; OCR was not needed for this run.
- Numbered headings, paragraphs, list-like lines, and the error-code table rows are represented as hierarchy nodes. Other tables are kept as extracted text, not a dependable cell grid; visually review them.
- Matching uses structural paths based on heading numbers and occurrence ordinals. It handles title edits and duplicate occurrences conservatively, but moved/renumbered nodes may appear removed and added.
- Hash staleness detects normalized textual changes, not clinical significance. The v2 2-second paragraph / 1.5-second E3 table inconsistency is preserved and reported as source text, not reconciled.
- The no-key provider is deterministic mock output, not an LLM. Provider responses retry at most twice and are rejected if invalid or if they cite nodes outside the selection. Human review is still required to rule out unsupported test interpretation.
- This is an assignment prototype, not validated medical-device software.

## Tests
```bash
pytest -q
```

The tests use an isolated temporary SQLite database and exercise the actual supplied v1/v2 PDFs. If using the workspace virtualenv on Windows, run `python -c "import pytest; raise SystemExit(pytest.main(['-q']))"` if direct `python -m pytest` launch is blocked by local execution policy.
