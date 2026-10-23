# CT-200 Parsing and Traceability — Approach Document

## Objective
Build a backend that extracts a hierarchy from the supplied CT-200 manuals, preserves document versions, pins selections to exact source content, generates QA test-case ideas, and exposes staleness during retrieval.

## Parsing approach
PyMuPDF extracts native page text first. OCR through Tesseract/Pillow is attempted only when a page has fewer than 30 native text characters. Both supplied PDFs have native text on their pages, so OCR was not needed during verification. The source is six pages for v1 and seven for v2.

Numbered headings are recognized with a CT-200-scoped expression; levels follow the number of components, but missing intermediate levels are not synthesized. Paragraphs and list-like lines preserve their source page and attach to the most recent heading stack entry. The error-code table is recognized from its `Code` header and emitted as one row node per `E1`-style code. Other tables, including specification rows, remain reading-order text and are not reconstructed as cell grids. No content is inferred to fill uncertain table cells.

## Observed structural variations and content changes
Inspection of the supplied PDFs shows six pages in v1 and seven in v2. Both include text-extractable pages and visually rendered tables (notably specifications and error codes). Heading numbering is not strictly sequential: section 3.4 appears before 3.3 in the extracted text. Section 2.1.1.1 appears beneath 2.1, skipping an expected intermediate level. The parser must preserve observed numbering and avoid renumbering based on assumptions. The documents also contain ligatures and hyphenated words in extracted text.

Observed v1-to-v2 changes in the supplied manuals include:
- Battery life changes from approximately 300 to 250 cycles; low-battery icon threshold changes from 15% to 10%.
- Inflation increments change from 40 mmHg to 30 mmHg.
- E3 auto-deflation changes from within 2 seconds to within 1.5 seconds in the error-code table.
- Error code E6 for Bluetooth sync failure is added.
- The error-code alarm range expands from E1–E5 to E1–E6.
- A new Section 5.3, Data Export, describes CSV export requirements.
Observed inconsistency: the general overpressure-protection paragraph still says the cuff vents within 2 seconds in v2, while the individual E3 error-code row says auto-deflation within 1.5 seconds. The parser stores these as separate paragraph and table-row nodes. It does not reconcile them; the version diff and generation source snapshots preserve each statement for human review.

Actual PDF text observations also include ligature characters (for example `cuﬀ` and `deﬂates`), nonbreaking hyphens, a page break between Section 5.2 and 5.3, and table columns whose values are split across lines. `clean()` normalizes whitespace and common `fi`/`fl` ligatures, but source snapshots retain the extracted body text as parsed. Page references are one-based PDF page numbers.

## Hierarchy reconstruction
Each numbered heading becomes a heading node. Level is derived from the number of numeric components. Paragraphs, list-like lines, and recognized error table rows attach to the active heading. A page break does not itself reset the stack. A skipped level attaches to the nearest existing lower-level heading; no missing parent is invented. Out-of-order numbering is retained as encountered. Duplicate heading occurrences remain distinct through sibling ordinals. Tests simulate skipped levels, out-of-order sections, duplicate headings, and page-break continuity.

## Data model and version matching
SQLite/SQLAlchemy stores document versions, nodes, selections, and generation records. Each node has a version-specific ID, logical ID, normalized content hash, page, heading, level, kind, parent, and structural path. Numbered heading identity uses its section number rather than title, so heading title edits can retain logical identity. Child paths incorporate parent paths and duplicate occurrence ordinals; matching consumes each prior candidate once. This handles the actual edits in these PDFs, including E3 text change and appended E6, but moved/renumbered nodes may appear removed and added. Full document comparison reports changed, unchanged, added, and removed nodes.

The assignment requests MongoDB or an explicitly justified JSON-store fallback. This implementation does not claim MongoDB: immutable generation outputs and source snapshots are JSON documents stored in SQLite `TEXT` columns, while document/node identities and queryable relations remain SQLAlchemy tables. SQLite is the chosen local JSON-store fallback to keep the workflow single-process and service-free.

## Selection and generation provenance
Selections store immutable snapshots of node IDs, logical IDs, document keys, versions, heading/body text, page, and content hash. A named selection is restricted to one document version. Generation records store the output and exact source JSON; repeated requests create new records. Pydantic checks 3–5 cases, required nonempty fields, and priority values; an OpenAI-compatible response gets at most two attempts and cannot cite IDs outside the selection. Without `OPENAI_API_KEY`, deterministic output is labelled `mock-fallback` and is never presented as a real LLM result. The prompt forbids unsupported thresholds, but model output still needs human review; schema checks cannot prove clinical correctness.

## Staleness
Retrieval compares each snapshot hash with the latest matching logical node within the snapshot's document key. Changed and missing/unmatched nodes mark the generation stale, with source-level statuses and original/current hashes visible. Hash differences detect textual change, not clinical impact: punctuation/wording edits and safety-threshold edits can both be flagged without claiming equal risk. This is not a clinical impact classifier.

## Validation and debugging
Verification performed in this workspace: `pytest -q` through `pytest.main()` passed 11 tests. The test workflow ingests the actual supplied PDFs into a temporary SQLite DB, creates and generates a v1 selection, ingests v2, then verifies pinned text, stale retrieval, E3 changed, E6 added, and the retained 2-second/1.5-second inconsistency. Additional simulated parser tests cover skipped heading levels, out-of-order numbering, duplicate headings, and page breaks. An LLM stub verifies malformed JSON retries once and then validates. Direct `python -m pytest` was blocked in this Windows environment by process-launch policy; the equivalent Python invocation succeeded. Before production use, visually review reconstructed hierarchy and all extracted tables, especially non-error-code tables.

## Decision log
1. **Most likely silent error and detection:** A visually split table or heading may be flattened/mis-parented by native text order. The error-code table is split using observed E-code row markers, while simulated hierarchy tests and actual PDF regressions check known structure. Manual page-image review remains necessary for all other tables and ambiguous layout.
2. **Simplicity chosen over correctness:** Structural section-number paths and occurrence ordinals are preferred over fuzzy semantic matching. This keeps matching explainable and correctly matches the observed E3 and edited numbered headings; a moved or renumbered node can be classified added/removed. The tradeoff is reduced recall for structurally moved content, which should be reviewed rather than guessed.
3. **Unsupported input and behavior:** A scanned page with no working OCR dependency may yield no text; OCR errors are currently tolerated and the API does not claim extraction completeness. Also, generated QA content cannot establish clinical validity: mock output is labeled, model output is schema/source constrained, and hash staleness is textual only. Unsupported source thresholds are not supplied by the deterministic fallback.

## What I would do with more time
Implement layout-aware extraction for all tables, OCR confidence and failure reporting, ambiguity-aware matching for moved sections, an external MongoDB adapter if deployment requires it, and a human-reviewed semantic impact workflow for safety-critical changes.
