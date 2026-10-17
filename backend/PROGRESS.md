# Development Progress

## Current phase
Phase 9 — pdfplumber text/table extraction, extraction quality check and PaddleOCR fallback

## Status
Completed. The extraction pipeline is now: content validation -> **pdfplumber** (page text
**and** tables) -> **extraction quality check** (required invoice fields, table
completeness, pages without a usable text layer) -> **PaddleOCR fallback** for those
pages (layout/table recovery from the recognised line geometry) -> per-page text +
recovered tables + quality report -> LLM invoice JSON (Phase 3) -> VAT validation
(Phase 4). PyMuPDF is no longer imported by the application at all.

# Phase 9 — pdfplumber extraction, quality check and PaddleOCR fallback (Completed)

## Environment setup (installed for this phase)
- `.\.venv\Scripts\python.exe -m pip install "pdfplumber>=0.11"` → pdfplumber 0.11.10,
  pdfminer.six 20260107, Pillow 12.3.0, pypdfium2 5.14.0, cryptography 50.0.2 (installed
  into the existing `backend/.venv`; nothing was installed into the global interpreter).
- `.\.venv\Scripts\python.exe -m pip install numpy` → numpy 2.5.4. It is needed only by
  the tests of the PaddleOCR adapter (the real PaddleOCR brings its own numpy); no
  application module imports numpy.
- `paddleocr` / `paddlepaddle` are deliberately **not** installed: they are large,
  platform-specific wheels and the fallback is optional by design
  (`backend/requirements-ocr.txt`). Every OCR test injects `tests/ocr_stubs.py`
  (`StubOcrEngine`) through the `get_document_ocr_engine` dependency instead, so the
  suite stays offline and PaddleOCR-free.

## Completed work
- `app/services/pdf_extractor.py` (rewritten): content validation (`%PDF-` in the leading
  bytes) plus a real `pdfplumber.open` on the in-memory byte stream, per-page text and
  table extraction, the extraction quality check and the OCR fallback orchestration.
  `fitz`/PyMuPDF is gone from `app/`; the module docstring documents the five-step
  pipeline, and failure mapping distinguishes "not a PDF" (415), "empty" (400),
  "corrupted" (400) and "password protected" (400, detected through the exception chain
  because pdfplumber wraps `PDFPasswordIncorrect` in its own `PdfminerException`).
- `app/services/ocr_fallback.py` (new): the optional PaddleOCR adapter. The import is
  lazy (`_load_paddleocr`), the constructor tolerates 2.x/3.x keyword differences, the
  prediction call accepts `ocr()` or `predict()`, and every result shape
  (`[[box, (text, score)]]`, `rec_texts`/`rec_scores`/`dt_polys`, `{"res": ...}`, numpy
  arrays, attribute-style objects) is normalised into `OcrLine` records. Unknown shapes
  are skipped instead of raising, and `get_ocr_engine()` returns `None` when PaddleOCR
  is missing — a supported deployment state, not an error.
- Layout and table recovery: `group_lines_into_rows` clusters lines by vertical overlap,
  `recover_table_rows` clusters their left edges into columns (ragged rows padded with
  empty cells, running text returns `[]`, so a paragraph is never turned into a table),
  and `ocr_lines_to_text` keeps the column layout in the page text (`" | "` between
  cells, one visual row per line) so the LLM field extraction sees the table.
- Extraction quality check (`build_extraction_quality`, `INVOICE_FIELD_PATTERNS`,
  `REQUIRED_INVOICE_FIELDS`): reports the required invoice fields that were found and the
  ones that were not (never guessed), the pages that still lack a usable text layer, the
  pages a table was recovered from, and the pages that draw ruling lines whose table
  could not be rebuilt.
- OCR candidate rule: a page is OCR'd when its collapsed text is shorter than
  `VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS` (default 12), or when it is image-dominated
  (embedded images covering >= 60% of the page) while its text stays below ten times that
  threshold — which catches scans whose text layer holds only a scanner stamp. A page
  with a real text layer is never replaced by OCR output (OCR must read *more* text to
  win), and the fallback never raises: a missing engine, a rendering failure, an engine
  error or an empty read becomes a quality issue and the page keeps its pdfplumber text.
- `app/config.py`: `ocr_enabled`, `ocr_language`, `ocr_dpi`, `ocr_min_page_characters` and
  `ocr_max_pages` (all `VAT_AUDIT_OCR_*`, invalid values fall back to the defaults). The
  shared `_bool_from_env`/`_text_from_env` helpers and `TRUE_VALUES`/`FALSE_VALUES` moved
  next to `_positive_int_from_env`, so the Phase 2 `Settings` object and the Phase 4 audit
  settings use one implementation.
- `app/models/document.py`, `app/storage/memory.py`, `app/api/documents.py`: the upload
  and detail responses now carry `used_ocr`, `ocr_pages`, `table_count` and the `quality`
  report (`ExtractionQualityInfo`), every page carries its `source` (`pdfplumber` /
  `paddleocr`) and its `table_count`, and `include_text=true` adds the recovered `tables`
  (rows of cells). `DocumentRecord` stores the quality report, and the upload route
  resolves the OCR engine through the new `get_document_ocr_engine` dependency, so a
  deployment without PaddleOCR (and every test) simply answers `None`.
- Requirements and docs: `requirements.txt` declares **pdfplumber** for extraction and
  keeps PyMuPDF only as the fixture author of `tests/pdf_factory.py` and
  `scripts/live_smoke.py` (the application never imports it); the new
  `requirements-ocr.txt` declares the optional PaddleOCR stack. `.env.example`,
  `README.md` (pipeline, configuration, limitations, structure, roadmap), `app/main.py`
  and the frontend (`lib/types.ts`, `components/DocumentOutputPanel.tsx`,
  `components/InvoicePanel.tsx`) were updated: the UI shows the OCR page(s), the recovered
  table count and the quality findings, and no longer claims that OCR is out of scope.
- Tests: `tests/pdf_factory.py` gained `build_table_pdf` (ruled grid), `build_scanned_pdf`
  (full-page image plus a scanner stamp) and `build_encrypted_pdf`; `tests/ocr_stubs.py`
  (new) provides the deterministic OCR engine; `tests/test_pdf_extractor.py` (new) covers
  validation, text/table extraction, the quality check, the OCR candidate rules and every
  fallback outcome; `tests/test_ocr_fallback.py` (new) covers both PaddleOCR result shapes,
  constructor fallbacks, error mapping, availability and the geometry helpers;
  `tests/test_documents.py`, `tests/test_end_to_end.py` and
  `tests/test_documents_extraction_api.py` were updated for the new fields, wording and
  scope.

## Tests and results (real executions)
- `Set-Location 'd:\Ai\SuperAuditor\backend'; .\.venv\Scripts\python.exe -m pytest -q -W error::DeprecationWarning`
  → **329 passed in 6.20s**, exit code `0` (was 253 passed before this phase).
- Target runs while developing: `... -m pytest -q tests/test_pdf_extractor.py tests/test_ocr_fallback.py`
  → 42 passed; `... -m pytest -q tests/test_documents.py` → 26 passed.
- Real-world check on the repository's own sample:
  `extract_pdf_text(Path('sample_invoice.pdf').read_bytes())` → 1 page, 373 characters,
  0 tables, `used_ocr=False`,
  `quality=passed fields_found=invoice_number, invoice_date, supplier_trn, taxable_amount, vat_amount, total_amount fields_missing=none pages_needing_ocr=0 table_pages=0 ocr_pages=none issues=0`.
- A ruled-table fixture (`build_table_pdf(["Description","Amount"], ...)`) is rebuilt by
  pdfplumber as 4 rows × 3 columns with the expected cells; a scanned fixture
  (`build_scanned_pdf`) reports `coverage=0.942` and is an OCR candidate, and with the stub
  engine its text becomes the OCR text (`source=paddleocr`), its layout becomes a 2-column
  table and the quality report passes.
- `ruff check app tests scripts` → 58 findings, all in the repository's pre-existing style
  categories (`B008` FastAPI `Depends`/`File` defaults, `FURB157` `Decimal("5")` in tests,
  `FLY002` `"\n".join([...])` fixtures, `ISC004` message constants, `UP037`/`PYI041` in the
  existing schemas). No new finding was introduced by the new modules beyond those
  categories, and the intentional broad catches carry the repository's
  `# noqa: BLE001 - <reason>` comments.
- `Set-Location 'd:\Ai\SuperAuditor\frontend'; .\node_modules\.bin\tsc.cmd --noEmit` → exit
  code `0` after the type/UI updates.
- Live run: `Start-Process .\.venv\Scripts\python.exe -ArgumentList '-m','uvicorn','app.main:app','--port','8021'`
  followed by real `httpx` calls (then `Stop-Process`):
  `sample_invoice.pdf` → `extracted`, 0 tables, `used_ocr=false`,
  `quality=passed fields_found=invoice_number, invoice_date, supplier_trn, taxable_amount, vat_amount, total_amount fields_missing=none pages_needing_ocr=0 table_pages=0 ocr_pages=none issues=0`;
  a full-page-image scan (`build_scanned_pdf`) → `extracted` (scanner stamp),
  `pages_needing_ocr=[1]`, `used_ocr=false` (PaddleOCR not installed);
  a ruled-table PDF (`build_table_pdf`) → `table_count=1`, `table_pages=[1]`, and
  `GET /documents/{document_no}?include_text=true` returned
  `pages[0].source="pdfplumber"` with `pages[0].tables == [["Description","Amount"],["Consulting","1,000.00"],["Total","1,050.00"]]`.

## Revision — real PaddleOCR on a CPU-only laptop (2026-10-17)

The optional stack was then installed on a development laptop (PaddleOCR 3.7.0,
PaddlePaddle 3.3.1 **CPU** build, `paddle.device.is_compiled_with_cuda() == False`) and a
real scanned invoice (`scaned_inv.pdf`, 1 page, 367 KB, 11 characters of text layer) was
run through the upload pipeline. Three real failures surfaced; all three were fixed in the
adapter, not in the pipeline:

1. **`ValueError: Unknown argument: use_gpu`** — PaddleOCR 3.x deleted `use_gpu` (and
   `show_log`) and validates the leftovers in `paddleocr._common_args.parse_common_args`,
   which accepts only `device`, `engine`, `engine_config`, `enable_hpi`, `use_tensorrt`,
   `precision`, `enable_mkldnn`, `mkldnn_cache_capacity`, `cpu_threads`, `enable_cinn`.
   The old candidate ladder sent `use_gpu` first and only advanced on `TypeError`, so the
   `ValueError` aborted it and became the page's quality issue. Fixed by deriving the
   keywords from the installed signature (`_accepted_init_parameters` +
   `_engine_init_kwargs`, with a `device` -> `use_gpu` translation for 2.x) and by dropping
   just the refused keyword and retrying (bounded) if a version still rejects one. `predict`
   is now preferred over 3.x's deprecated `ocr` wrapper, and a failing method falls through
   to the next one.
2. **`NotImplementedError: (Unimplemented) ConvertPirAttribute2RuntimeAttribute not support
   [pir::ArrayAttribute<pir::DoubleAttribute>] (onednn_instruction.cc:118)`** — this
   PaddlePaddle CPU build's oneDNN kernels fail on text detection, so every page was lost.
   `VAT_AUDIT_OCR_ENABLE_MKLDNN` (default `false`) keeps OCR off that path; with it off the
   same page is read correctly.
3. **Runtime** — the first working run took **166 s for one page**: PaddleOCR 3.7 defaults to
   the PP-OCRv6 *medium* detector/recogniser, 10 CPU threads, 200 DPI and the document
   orientation + unwarping models. The portable CPU profile now sends `device=cpu`,
   `enable_mkldnn=False`, `cpu_threads=4`, the mobile models
   (`PP-OCRv5_mobile_det` / `en_PP-OCRv5_mobile_rec`), `DPI=150` and disables the two
   document pre-processors. Measured on the same page: **28 s**, recovering
   `UBC-TINV-2026-684`, TRN `100561525500003`, `15-Oct-2026`, taxable `2,926.00`,
   VAT `146.30` and total `3,072.30`, with `quality=passed` and one recovered table.

Tests were adjusted so the suite stays hermetic on a machine that *does* have PaddleOCR:
the `without_paddleocr` fixture now patches the import probe in `tests/conftest.py` (the
single place the route, the extractor and the cache meet) instead of only the FastAPI
dependency, which had let the real engine load during three API tests (and made them take
4-9 s each). Nine new adapter tests cover the reported crash, the retry paths, 2.x/3.x
keyword translation, `predict` preference, method fall-through and the setting-driven
device/mkldnn/threads/models.

## Known issues / decisions
- PaddleOCR is optional and is **not** installed by default in this checkout, so the OCR
  path is validated with a stub engine (the adapter itself is covered by unit tests that
  patch the lazy import with fake PaddleOCR classes). Installing `requirements-ocr.txt` on
  the target platform is what enables real scans; the code needs no change to pick it up.
  It *was* installed on the development laptop for the Revision above, which is how the
  `use_gpu`/oneDNN/runtime problems were found.
- OCR is per page, not per document: a partly scanned PDF keeps its pdfplumber text on the
  readable pages and gets OCR text only where the text layer is unusable, and every page
  says which engine produced it (`source`).
- The engine object is cached (`lru_cache` in `get_ocr_engine`) because PaddleOCR loads its
  models in the constructor; the cache is dropped by `clear_ocr_engine_cache()` (tests, or
  after installing the package in a running process).
- Tables come from pdfplumber's line-based detector, so a borderless table is not rebuilt
  (and if a page draws ruling lines the quality check says so). The OCR path reconstructs
  the grid from line geometry: it recovers columns and cells, but it is a geometric
  reconstruction, not a semantic model of the invoice.
- The quality check never fills a gap: missing required fields and unreadable pages are
  reported, and the LLM extraction (Phase 3) plus the VAT rules (Phase 4) remain the only
  steps that produce or judge values. A page that already carried real text is never
  overwritten by OCR output.
- OCR cost is bounded by `VAT_AUDIT_OCR_MAX_PAGES` (25) and `VAT_AUDIT_OCR_DPI` (150; raise
  it for small print); raising the DPI improves accuracy and slows the upload, and raising
  the page bound increases the time the request spends in the OCR engine. At the measured
  ~28 s per scanned page on a CPU-only laptop, a 25-page scan is the worst case and a
  one-page invoice is the normal one. The image-coverage threshold
  (60%) and the ten-times-text multiplier are constants in `pdf_extractor.py` rather than
  settings, to avoid knob sprawl.
- PyMuPDF stays in `requirements.txt`, but only to author the in-memory fixtures of
  `tests/pdf_factory.py` and `scripts/live_smoke.py`; `test_extraction_uses_pdfplumber_and_only_paddleocr_as_the_ocr_engine`
  asserts that `import fitz`/`pymupdf` never appears in `app/` and that PaddleOCR is never
  imported at module level, so the runtime boundary stays enforced.
- The Phase 7 scope test `test_no_database_frontend_auth_or_ocr_was_added` became
  `test_no_database_frontend_auth_or_unapproved_extraction_was_added` plus
  `test_extraction_uses_pdfplumber_and_only_paddleocr_as_the_ocr_engine`, because "no OCR"
  is no longer the scope; `pytesseract`, `easyocr` and `pdf2image` stay forbidden.
- `APP_VERSION` is still `0.2.0` (pinned by a Phase 2 test), unchanged by this phase.

## Next phase
- Optional: install `requirements-ocr.txt` on the deployment host and re-run
  `scripts/live_smoke.py` against a real scanned invoice to validate PaddleOCR end to end
  (the wheels are platform-specific, so this needs the target platform).
- Optional: expose `used_ocr` / the quality summary in the `GET /documents` list row (the
  detail view and the UI panel already show them).

# Phase 7 — End-to-End Testing and Cleanup (Completed, archived)

## Environment setup (local `.venv`)
- `backend/.venv` now exists and holds every dependency from `requirements.txt`, installed with the
  machine's Python 3.13.7 (`python -m venv .venv`), pip upgraded to 26.2.1, then
  `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` → fastapi 0.143.2, uvicorn 0.54.0,
  pydantic 2.14.1, pydantic-settings 2.16.0, pytest 9.1.1, httpx 0.28.1, PyMuPDF 1.28.2,
  python-multipart 0.0.32, langgraph 1.2.15, langchain-core 1.6.11, langchain-openai 1.7.2.
- The environment is now the supported way to run the project:
  `.\.venv\Scripts\python.exe -m pytest -q` and
  `.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload` (or activate
  `.\.venv\Scripts\Activate.ps1` first). Nothing was installed into the global interpreter.
- `.venv/` was already covered by `.gitignore`, so the environment stays untracked; the README
  "Setup" and "Tests" sections and `.vscode/settings.json` now point at it.
- Verified: full suite **253 passed in 9.55s** on the environment interpreter, and a live
  `.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8010` start answered `GET /health`
  with `{"status":"ok"}`.
- The "no virtual environment was created" notes in the archived Phase 1 and Phase 2 logs describe
  those phases and are kept as history; they no longer describe the current checkout.


## Completed work
- `tests/test_end_to_end.py` (new, 40 tests): walks the whole documented workflow over real HTTP (`POST /documents/upload` -> `POST /documents/{document_no}/extract` -> `GET /documents/{document_no}` -> `POST /audit/{document_no}`) for every scenario in the plan, and asserts rule-level results, confidence percentages and human-review flags. Everything is offline: scripted chat models, stub extractors, explicit settings.
- Scenarios covered: correct standard-rated invoice; wrong VAT amount; wrong rate (and a 15% rate with 15% VAT, which still fails because VAT is recomputed at the expected rate); wrong total; missing supplier TRN; missing customer TRN with and without the requirement; zero-rated and exempt invoices; unknown treatment; missing amounts; low-confidence model findings; model output contradicting the arithmetic; scanned (needs-OCR) PDF; corrupted/non-PDF uploads; unknown document numbers; missing API key; provider timeout, rate limit, server and auth errors; malformed model JSON; invented rule IDs.
- Checklist coverage in the same module: `.env`-only provider/model switch (report shows `provider`/`model` from the file), `.env`-only confidence-threshold change, base URL + timeout from the environment, ruleset version + tolerance from the environment, `.env.example` placeholders only, `.env` git-ignored, no secret-like value committed, no database/frontend/auth/OCR dependency added, in-memory records disappearing on "restart", bounded agent loop (1 vs 2 iterations -> exactly 2 vs 3 model calls), and tool calls traced by name only.
- `scripts/live_smoke.py` (new): starts uvicorn, runs the four endpoints over real HTTP against the provider configured in `.env`, prints a secret-free summary and can write the report JSON.
- `SAMPLE_AUDIT_REPORT.md` (new): the Phase 7 sample report, built from two **real live runs** (`deepseek` / `deepseek-flash` / `uae-vat-rules-1.0.0`): a `completed` audit (confidence 94.1) and a `requires_review` audit of an invoice with no stated VAT treatment (confidence 70, classification review + rate check flagged).

### Bug fixes found by the end-to-end work
- `app/audit/state.py`: `conflicts` was written by `guardrail_validate` but never declared in `AuditState`, so LangGraph dropped the write and the documented conflict confidence cap (70%) could never apply. Now declared (and initialised), with a regression test that asserts the 70% cap and the conflict wording in `confidence_explanation`.
- `app/audit/graph.py`: a missing or unconstructable provider made `build_audit_graph` raise, so `POST /audit/{document_no}` returned a 500. New `resolve_audit_chat_model()` returns an internal "unavailable model" that fails with a sanitised `LLMError`, so the audit still returns its deterministic findings with `requires_review`, a clear `LLM_API_KEY`/`LLM_MODEL` warning and no clean pass.

### Configuration hygiene (Phase 7 requirement: placeholders only)
- The real `LLM_API_KEY` and model name were moved out of the committed `.env.example` into the git-ignored `.env`, and `.env.example` now carries placeholders (`your-openai-compatible-api-key`, `your-model-name`) with a comment saying so.
- The live run first failed with `HTTP 400` because `LLM_MODEL=deepseek-v4.1-flash` is not a supported DeepSeek model (the API replied that it supports `deepseek-flash` and `deepseek-v4-pro`). `.env` now uses `deepseek-flash`, and both `.env.example` and the README provider table were updated.
- `LLM_PROVIDER_LABEL=deepseek` in `.env` so reports record the provider that actually produced them.

## Files created or changed
- New: `backend/tests/test_end_to_end.py`, `backend/scripts/live_smoke.py`, `backend/SAMPLE_AUDIT_REPORT.md`, `backend/.env` (git-ignored, not part of the deliverable tree).
- Changed: `backend/app/audit/state.py`, `backend/app/audit/graph.py`, `backend/.env.example`, `backend/README.md`, `backend/PROGRESS.md`.

## Commands executed
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest tests/test_end_to_end.py -q` (iterated while writing the scenario matrix)
- `python -m pytest -q`
- `python -m pytest -q -W error::DeprecationWarning`
- `python -m ruff check app/api app/main.py app/audit app/models/audit.py tests/test_audit_api.py tests/test_audit_graph.py tests/test_end_to_end.py tests/audit_stubs.py --statistics`
- `python scripts/live_smoke.py --port 8123 --report <tmp>\live_report.json` (real uvicorn + real provider)
- `python scripts/live_smoke.py --port 8126 --report <tmp>\live_report2.json`

## Tests and results
- `tests/test_end_to_end.py` -> **40 passed**; full suite -> **253 passed in 10.87s** (exit code 0), and **253 passed** with `-W error::DeprecationWarning` (128 -> 148 -> 195 -> 213 -> 253 across phases).
- Ruff: the new/changed files introduce no new findings; the remaining `B008` (`Depends`/`File` in FastAPI defaults), `FURB157` (`Decimal("0.01")`), `ISC004` and `PYI041` findings are the pre-existing house style of this codebase.
- Live run 1 (real provider, before the model name was corrected): `GET /health` 200, upload 201, extract **502** with a sanitised provider message - `The supported API model names are deepseek-flash, deepseek-v4-pro, but you passed deepseek-v4.1-flash`. The key was not leaked and no pass was claimed, which is exactly the documented behaviour.
- Live run 2 (`deepseek-flash`, invoice without a stated VAT treatment): extract 200 (`INV-2024-0001`, total 1050.00); audit 200 `requires_review`, confidence **70.0**, findings `VAT_RATE_CHECK review_required (60)`, `INVOICE_TOTAL_CHECK pass (95)`, `SUPPLIER_TRN_CHECK pass (90)`, `VAT_CLASSIFICATION_REVIEW review_required (80)`, `CUSTOMER_TRN_CHECK not_applicable (95)`; reasons included the unknown VAT treatment and a missing invoice date.
- Live run 3 (`deepseek-flash`, compliant standard-rated invoice): audit 200 **`completed`**, confidence **94.1**, 4 passes + 1 not_applicable, no review reasons, `provider=deepseek`, `model=deepseek-flash`, `ruleset_version=uae-vat-rules-1.0.0`. Recorded in `SAMPLE_AUDIT_REPORT.md`.

## Acceptance criteria (plan checklist -> where it is verified)
- `.env` controls key, base URL, model, timeout and confidence threshold: `test_env_file_alone_switches_provider_and_model`, `test_env_file_alone_changes_the_review_threshold`, `test_env_file_controls_the_base_url_and_timeout`, `test_ruleset_version_and_tolerance_come_from_the_environment`.
- `.env` is git-ignored and `.env.example` contains placeholders only: `test_env_is_git_ignored_and_the_example_is_not`, `test_env_example_contains_placeholders_only`, `test_env_example_documents_every_setting` (and the values were physically moved during this phase).
- No API key is logged or returned: `test_provider_failures_never_produce_a_passing_audit`, `test_extraction_provider_failures_are_mapped_and_store_nothing`, `test_response_excludes_secrets_trns_and_full_page_text` (Phase 6), `test_no_secret_looking_value_is_committed`.
- Tests mock the provider and need no network: the whole suite (scripted models + stub extractors, explicit settings, no `.env` dependency).
- Decimal is used for monetary arithmetic: `test_reported_rate_drives_the_vat_recomputation`, the money findings assert exact `"50.00"`/`"1050.00"` strings computed in Python.
- Model output is schema-validated and cannot introduce unknown rules: `test_model_cannot_introduce_rules_outside_the_ruleset`, `test_malformed_model_output_never_produces_a_passing_audit`.
- Audit failures never silently become passes: the provider-failure, malformed-output, missing-key, low-confidence and conflict tests all assert `requires_review`/`cannot_audit` with deterministic-only findings.
- No database, frontend, ERP integration or authentication has been added: `test_no_database_frontend_auth_or_unapproved_extraction_was_added` (named `..._or_ocr_was_added` in Phase 7; Phase 9 renamed it because the optional OCR fallback was added).
- In-memory records disappear after a restart and this is documented: `test_records_disappear_when_the_process_restarts`, `test_in_memory_storage_limitation_is_documented`.
- Revision 2 additions: `.env`-only provider/model switch (verified live and in tests), bounded agent run (`test_agent_loop_is_bounded_by_max_iterations`) with an exposed trace (`trace.nodes`/`model_calls`/`tool_calls`), deterministic arithmetic overriding the model (`test_model_contradicting_arithmetic_keeps_the_deterministic_result`), LLM failure never yielding a pass, every test offline, and the provider label/model/ruleset recorded in `SAMPLE_AUDIT_REPORT.md`.

## Known issues / decisions
- `APP_VERSION` is still `0.2.0` (pinned by a Phase 2 test); bumping it remains a user decision.
- Provider/model names are provider-side state: an unknown `LLM_MODEL` produces a clear `HTTP 400`-derived 502. Pin the model per deployment and re-run `scripts/live_smoke.py` after a provider change.
- A missing *optional* field (for example the customer TRN when not required) raises no rule finding but still flags the report for review, because missing evidence is a documented review trigger; the confidence is capped accordingly.
- A low-confidence model finding does not lower the deterministic confidence number; it flags the finding and caps the overall confidence below the review threshold.

## Next phase
None - the Phase 7 scope (Phases 1-7) was complete. Superseded by Phase 9, which added the
optional PaddleOCR fallback: OCR is no longer out of scope, while persistent
storage/database, authentication, frontend and ERP integration remain out of scope at that
point (the frontend arrived in Phase 8).

# Phase 6 — Audit API (`POST /audit/{document_no}`) (Completed, archived)

## Completed work
- `app/api/audits.py` (new): the audit route. `POST /audit/{document_no}` takes the document number as its only input, returns the structured `AuditReport` with Swagger summaries, descriptions and documented `404`/`409`/`500` responses, and resolves the service through a `get_ai_auditor` dependency so tests stay offline.
- `app/api/documents.py`: module docstring and Swagger descriptions now describe the full upload -> extract -> audit workflow; `GET /documents/{document_no}` adds an `AUDIT_NOTE` to `notes` when an extraction exists, and the upload `message` points at both the extract and the audit endpoint.
- `app/main.py`: registers the audit router and rewrites the app description for Phase 6 (workflow, scope, disclaimer). `APP_VERSION` deliberately stays `0.2.0`: `tests/test_documents.py` pins it and Phase 1/2 were not to be modified.
- `app/models/audit.py`: `AuditReport.message` (next-step guidance, including the repeated-call behaviour) plus a docstring stating the status rules and latest-report-per-document storage.
- `app/audit/ai_auditor.py`: `latest()` and `remember()` so the route reports and stores the latest report without touching private state.
- `app/audit/state.py`: **bug fix** — `human_review_required` is now a declared `AuditState` key set by the review/aggregate nodes. Phase 5's nodes wrote it, but because it was not declared, LangGraph silently dropped the write and the report fell back to the per-finding flags. The Phase 6 endpoint tests caught it; a Phase 5 regression test now covers both the clean and the model-failure case.
- Status rules documented end to end: `completed` (clean), `completed_with_warnings` (e.g. a dropped model finding), `requires_review` (low confidence, ambiguous treatment, missing evidence, model/deterministic conflict, provider failure) and `cannot_audit` (no enabled rule applies to the treatment).
- Refusals: unknown document -> `404`; needs OCR or no extracted fields -> `409`; provider outage -> `200` with `requires_review` (never a claimed success); unexpected error -> `500` with no stack trace, secret or invoice text.
- Repeated calls: the audit re-runs and replaces the stored report, and the response `message` says so (documented in README, Swagger and `AuditReport`).

## Files created or changed
- New: `backend/app/api/audits.py`, `backend/tests/test_audit_api.py`.
- Changed: `backend/app/main.py`, `backend/app/api/documents.py`, `backend/app/models/audit.py`, `backend/app/audit/ai_auditor.py`, `backend/app/audit/state.py`, `backend/tests/test_audit_graph.py`, `backend/README.md`, `backend/PROGRESS.md`.

## Commands executed
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest tests/test_audit_api.py -q`
- `python -m pytest -q`
- `python -m pytest -q -W error::DeprecationWarning`
- `python -m ruff check app/api app/main.py tests/test_audit_api.py tests/test_audit_graph.py app/audit/state.py app/audit/ai_auditor.py app/models/audit.py`
- a live end-to-end HTTP script (upload -> extract -> audit -> repeat -> needs OCR -> unknown document) using `TestClient` and a scripted model, then removed.

## Tests and results
- `tests/test_audit_api.py` -> **17 passed**; `tests/test_audit_graph.py` -> **48 passed**; full suite -> **213 passed** (exit code `0`), and **213 passed** with `-W error::DeprecationWarning` (128 -> 148 -> 195 -> 213 across phases).
- Live run output: `1) upload: 201 extracted DOC-04E9A035198B412784389F19DCEA8C32`; `2) extract: 200 invoice_number=INV-2024-0001`; `3) audit: 200 completed | confidence 94.1 | review False` with `provider/model: test-provider test-model` and `ruleset uae-vat-rules-1.0.0`; `4) repeat call: 200 | repeated note: True`; `5) needs OCR: 409 The document has no extractable text (it needs OCR), so it cannot be a...`; `6) unknown doc: 404 Document 'DOC-NOT-REAL' was not found...`; `7) stored reports: 1`.
- Fixes made while writing the endpoint tests: a test-helper sentinel made `invoice=None` mean "use the default invoice" (409 on every happy path); two over-strict assertions (findings are severity-ordered; the cannot-audit assessment says "no audit could be performed"); and the real product bug described above (`human_review_required` missing from `AuditState`).

## Acceptance criteria
- `POST /audit/{document_no}` with the path number as the only input: met.
- 404 for unknown documents, and a refusal to claim completion for failed/needs-OCR documents: met (404 / 409, with the model never called).
- Structured report with document_no, invoice_number, audit_status, provider/model, ruleset version, audited_at, confidence, findings, summary counts and human_review_required: met (asserted field by field).
- Per-finding rule ID, status, severity, explanation, expected/actual values, page/evidence, confidence, review reason and recommended action: met (also asserted against the OpenAPI schema).
- Documented statuses and review triggers: met.
- Latest report per document number with documented repeated-call behaviour: met.
- No stack traces, secrets or unnecessary full text: met (the response text is asserted free of the API key, `sk-`, raw TRNs and page text).
- Swagger response models plus README instructions for upload, document detail and audit: met.
- All existing tests pass; Phase 1/2 untouched apart from additive documentation text: met.

## Known issues / decisions
- `APP_VERSION` stays `0.2.0` (pinned by a Phase 2 test); bumping it is a user decision and would need that assertion updated.
- A provider outage is a `200 requires_review` rather than `502`: the deterministic checks still produce findings and the report states that no model assessment is available.
- `cannot_audit` is reachable when no enabled rule applies to the treatment (all applicable rules disabled), which the tests cover; needs-OCR and not-yet-extracted documents are refused earlier with `409`.

## Next phase
Phase 7 — End-to-End Testing and Cleanup ("do not add new product features"): start FastAPI and run the full workflow (upload -> retrieve -> `POST /audit/{document_no}`), then verify rule-level results, confidences and review flags for the documented scenarios (correct standard-rated invoice; wrong VAT amount/rate; wrong total; missing supplier TRN; missing customer TRN only when required; zero-rated needing review; exempt needing review; unknown classification or missing amount; low-confidence model finding). Also verify that changing `.env` alone switches provider and model, that the agent run is bounded (`VAT_AUDIT_MAX_AGENT_ITERATIONS` and the recursion limit) and exposes a trace, that deterministic arithmetic overrides the model on conflict, that an LLM failure never yields a passing audit, and that every test runs offline without an API key — then record the provider label, model identifier and ruleset version of the audited run in the final sample report, and clean up.

# Phase 5 — Agentic Audit Engine (LangGraph) (Completed, archived)

## Completed work
- `app/audit/state.py` (new): JSON-safe `AuditState` (`TypedDict`). The invoice travels as a `mode="json"` dump so Decimals survive as strings, reducers accumulate warnings/nodes/tool names, `review_reasons` is recomputed on every pass, and full page text never enters the state.
- `app/audit/tools.py` (new): `AuditToolbelt` with seven deterministic tools — `list_rules`, `get_rule`, `get_invoice_field`, `get_page_evidence`, `compute_expected_vat`, `check_invoice_totals`, `flag_human_review`. Failures return `{"error": ...}` instead of raising; TRNs and long digit runs are masked; page evidence is bounded.
- `app/audit/prompts.py` (new): `AUDITOR_SYSTEM_PROMPT` (one finding per supplied rule ID, allowed statuses, "call the tools for arithmetic", "never claim fraud") plus a size-bounded `build_auditor_prompt` carrying invoice facts, the configured rules, the deterministic results and masked evidence lines.
- `app/audit/nodes.py` (new): nine nodes, the Decimal checks, `overall_confidence`, model invocation (`bind_tools` when supported, otherwise prompt-based JSON), the guardrail validators and the invoice merge used by re-extraction.
- `app/audit/graph.py` (new): `build_audit_graph` wiring `START` -> `load_context` -> `select_rules` -> `deterministic_checks` -> `agent_review` -> `guardrail_validate` -> (`re_extract_fields` -> `select_rules` | `human_review` | `aggregate_report` | `cannot_audit`) -> `aggregate_report` -> `END`, with a `MemorySaver` checkpointer and bounded loops.
- `app/audit/ai_auditor.py` (new): `AiAuditor` service (`audit`, `audit_record`), `AuditReportStore` (latest report per document number), `UnknownDocumentError` and `DocumentNotAuditableError`.
- `app/models/audit.py`: added the Phase 5 models — `ModelFinding`/`ModelAuditReview` (lenient shape, semantic validation in the guardrail), `AuditFinding`, `AuditTrace`, `AuditSummary`, `AuditReport`, `summarise_findings` and severity weights.
- `app/config.py`: `agent_max_iterations` (`VAT_AUDIT_MAX_AGENT_ITERATIONS`, default 2, range 1-5).
- Guarantees implemented and tested: arithmetic is recomputed in Python with `Decimal`; unknown, disabled or inapplicable model rule IDs are dropped with a warning; a decisive model/deterministic conflict keeps the deterministic values and forces `review_required`; missing evidence, invalid statuses, missing confidence, invalid JSON, timeouts and provider errors all end in review, never a clean pass; the report never stores the API key, full page text or unmasked TRNs.
- No audit endpoint was added and the PDF extractor was left untouched, exactly as Phase 5 requires.

## Files created or changed
- New: `backend/app/audit/state.py`, `tools.py`, `prompts.py`, `nodes.py`, `graph.py`, `ai_auditor.py`, `backend/tests/audit_stubs.py`, `backend/tests/test_audit_graph.py`.
- Changed: `backend/app/models/audit.py`, `backend/app/config.py`, `backend/app/audit/__init__.py`, `backend/.env.example`, `backend/README.md`, `backend/PROGRESS.md`.

## Commands executed
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest tests/test_audit_graph.py -q`
- `python -m pytest -q`
- `python -m pytest -q -W error::DeprecationWarning`
- `python -m ruff check app/audit tests/audit_stubs.py tests/test_audit_graph.py app/models/audit.py`
- a live offline audit script (scripted tool-calling model) exercising the deterministic checks and the whole graph, then removed.

## Tests and results
- `tests/test_audit_graph.py` → **47 passed**. Full suite → **195 passed** (exit code `0`); with `-W error::DeprecationWarning` → `195 passed` (128 → 148 → 195 across phases).
- Failures found and fixed while writing the tests: missing `Mapping`, `INVOICE_FIELD_NAMES` and `content_text` imports; a LangGraph wiring bug where `select_rules` had both a conditional self-loop and a static edge (fixed with per-node route maps); the `missing_evidence` heuristic counted `not_applicable` findings and capped confidence at 70; review *reasons* were accumulated across loop passes and left a stale "no model assessment" reason after a successful retry (now derived from a persisted `extraction_note` plus per-pass recomputation); the re-extraction node let a raw extractor exception escape (now mapped to a sanitised message).
- Live deterministic check (no model needed) on a mismatched invoice: `TAXABLE_AMOUNT_VAT_MISMATCH fail expected=50.00 actual=49.00`, `INVOICE_TOTAL_CHECK fail expected=1049.00 actual=1050.00`, `SUPPLIER_TRN_CHECK pass actual=100****0003` (masked).
- Live tool-calling run: `status: completed | confidence: 94.1`, `provider/model: test-provider test-model`, `ruleset: uae-vat-rules-1.0.0`, trace `tool_calls: ['get_invoice_field', 'check_invoice_totals']`, explanation "severity-weighted mean of 4 finding(s) ... No capping was necessary."

## Acceptance criteria
- One result per applicable rule, from the model where possible and from the deterministic check otherwise: met.
- All model output validated with Pydantic; unknown rule IDs, invalid statuses and out-of-range confidence rejected, coerced or clamped with a recorded warning: met.
- Arithmetic recomputed in Python with `Decimal`; the model is never the sole authority, and a conflict forces human review: met.
- Missing or ambiguous inputs produce `warning`/`review_required`, never invented values: met.
- A documented overall confidence that is not an unexplained average (severity-weighted with explicit caps): met.
- Configurable review threshold (default 80) applied to every finding: met.
- No fraud or definitive legal claims: met (the report carries a review-aid disclaimer).
- Timeout, rate limit, provider error, malformed JSON and invalid model output handled without a passing audit: met.
- No audit endpoint in this phase: met.

## Known issues / decisions
- The agent loop is bounded by `VAT_AUDIT_MAX_AGENT_ITERATIONS` (default 2). With a failing model and no missing fields that means up to three model calls before the report is finalised — deliberate, bounded retry behaviour.
- A successful retry keeps the earlier failure as a warning (historical fact) but drops it as a review reason.
- Re-extraction only fills `None` fields; it never overwrites a value the first pass read.
- Tool *names* are recorded in the trace; tool arguments and results are not.
- The graph is compiled per audit run and bound to one document, which keeps page text out of the state; the compile cost is negligible at this scale.

## Next phase
Phase 6 — Audit API (`POST /audit/{document_no}`): integrate the registry, extraction service, rules and this auditor; map `UnknownDocumentError` to 404 and `DocumentNotAuditableError` to a documented 409; store the latest report per document; add Swagger response models and endpoint tests.

# Phase 4 — Configurable VAT Rules Engine (Completed, archived)

## Completed work
- `app/models/audit.py` (new): typed ruleset schemas — `RuleSeverity`, `AuditCheck`, `FindingStatus`, `TrnRole` literals; `AuditRule` (rule-ID pattern plus normalisation, per-treatment applicability, check-specific invariants such as "a rate rule may never target zero-rated or exempt supplies", per-rule AED tolerance and review-threshold overrides, `as_tool_payload()`); `RulesConfig` (unique-ID validation, `find`/`require`/`enabled_rules`/`disabled_rules`/`applicable_rules`, `as_tool_payload()`, `audit_context_payload()`, `describe()`); and the rule errors `RuleConfigError`, `UnknownRuleError`, `DisabledRuleError`.
- `app/audit/rules_config.py` (new): the six documented initial rules (`VAT_RATE_CHECK`, `SUPPLIER_TRN_CHECK`, `CUSTOMER_TRN_CHECK`, `TAXABLE_AMOUNT_VAT_MISMATCH`, `INVOICE_TOTAL_CHECK`, `VAT_CLASSIFICATION_REVIEW`), the constants `HUMAN_REVIEW_CONFIDENCE_THRESHOLD = 80`, `DEFAULT_TOLERANCE = 0.01`, `STANDARD_VAT_RATE_PERCENT = 5`, `TRN_FORMAT_PATTERN = ^\d{15}$`, `TRN_FORMAT_NOTE` and `LEGISLATION_REVIEW_NOTE`, the pure `default_rules()` builder, `build_rules_config()`, the cached `get_rules_config()`, `validate_rule_id()` (the gate the Phase 5 guardrails use) and `resolve_expected_vat_rate()` (standard-rated → configured rate, zero-rated/exempt → 0%, unknown → `None`).
- `app/audit/__init__.py` (new): package docstring plus the public re-exports of the rules engine.
- `app/config.py`: `AuditSettings` and the cached `get_audit_settings()`, the tolerant env helpers `_decimal_from_env`, `_bool_from_env`, `_text_from_env`, `_list_from_env`, `_rounding_policy_from_env`, and `MONETARY_QUANTUM` with the documented default constants.
- Rules stay authoritative configuration rather than model output: rate rules cannot target zero-rated/exempt supplies, a rate check requires a rate, a TRN rule requires a role, duplicate rule IDs are rejected, and unknown or disabled rule IDs are refused by `validate_rule_id()`.
- No LLM is called and no audit endpoint exists yet, exactly as Phase 4 requires.

## Files created or changed
- `backend/app/models/audit.py` (new), `backend/app/audit/__init__.py` (new), `backend/app/audit/rules_config.py` (new), `backend/tests/test_rules_config.py` (new).
- `backend/app/config.py` (audit settings), `backend/.env.example` (rules-engine variables), `backend/README.md` (ruleset table and variables), `backend/PROGRESS.md`.

## Commands executed
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest tests/test_rules_config.py -q`
- `python -m pytest -q`
- `python -m pytest -q -W error::DeprecationWarning`
- live ruleset checks with and without the `VAT_AUDIT_*` variables set in the shell.

## Tests and results
- `tests/test_rules_config.py` → **20 passed**. Full suite → **148 passed** (exit code `0`); with `-W error::DeprecationWarning` → `148 passed`.
- Two failures occurred while writing the tests: `config.require()` was called on a rule the same test had disabled, and `RuleConfigError` was not re-exported by `rules_config`; both were fixed (test-side fix plus an explicit `__all__`).
- Live check without overrides: `ruleset=uae-vat-rules-1.0.0 rules=6 enabled=6 tolerance=0.01 review_threshold=80 rounding=ROUND_HALF_UP`; `resolve_expected_vat_rate` → `5 / 0 / 0 / None` for standard/zero/exempt/unknown; `validate_rule_id("FRAUD_CHECK")` rejected as unknown; `validate_rule_id(" invoice_total_check ")` → `INVOICE_TOTAL_CHECK`.
- Live check with overrides (`VAT_AUDIT_MONETARY_TOLERANCE=0.25`, `VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD=92`, `VAT_AUDIT_DISABLED_RULES=customer_trn_check`, `VAT_AUDIT_RULESET_VERSION=uae-vat-rules-2026.1`, `VAT_AUDIT_REQUIRES_CUSTOMER_TRN=true`): 5 rules enabled, tolerance 0.25, threshold 92, customer TRN required while its rule is disabled, and the disabled rule ID refused by `validate_rule_id`.

## Acceptance criteria
- Rules are typed, versioned, documented configuration separate from route code: met.
- A model cannot invent a rule: unknown and disabled IDs are rejected, and the guardrail hook (`validate_rule_id`) is ready for Phase 5.
- The defaults (`0.01` tolerance, `80` review threshold, `5%` standard rate, supplier TRN required, customer TRN optional) are environment-configurable and covered by tests.
- Uncertain treatment never becomes a positive rate: zero-rated/exempt resolve to `0%`, unknown resolves to `None`.
- No audit endpoint and no LLM audit decisions in this phase: met.

## Known issues / decisions
- The six default rules are a documented starting point and must be reviewed against current UAE FTA legislation and guidance before production use (stated in code and README).
- `CUSTOMER_TRN_CHECK` ships enabled with `trn_required=false`, so it reports `not_applicable` unless the configured scenario requires a customer TRN, instead of treating every missing customer TRN as a violation.
- Per-rule `tolerance` and `review_confidence_threshold` overrides exist for future scenario packs; the ruleset-level values stay the defaults.

## Next phase
Phase 5 — Agentic Audit Engine (LangGraph): audit state, deterministic tools, nodes, graph, prompts and the auditor service, with scripted-model tests (no network).

# Phase 3 — Structured Invoice Field Extraction with a provider-agnostic LLM layer (Completed, archived)

## Status
Completed

## Revision 2 refactor (Phase 3R)
- Why: the user asked for the LLM layer to stop being DeepSeek-specific (only the base URL, API key and model name belong in `.env`, so any LLM can be used) and asked for the master plan to be revised so the audit itself becomes an agentic LangGraph process. Phase 3 was therefore reopened on explicit instruction; Phase 1 and Phase 2 were left untouched.
- Provider-agnostic LLM layer: `app/services/deepseek_client.py` was **deleted** and replaced by `app/services/llm.py` (`LLMSettings` consumers, `build_chat_model()`, `LLMExtractor`, `RawExtraction`), `app/services/llm_prompts.py` (prompts, untrusted-text delimiters, truncation) and `app/services/llm_errors.py` (`LLMError`, `LLMConfigurationError`, `LLMTimeoutError`, `LLMProviderError`, `LLMInvalidResponseError`, message sanitisation). No module, class or test file is named after a vendor any more, and a test asserts that no `deepseek_client` module exists.
- Configuration is now `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_TIMEOUT_SECONDS`, `LLM_MAX_INPUT_CHARACTERS`, `LLM_MAX_RETRIES`, `LLM_STRUCTURED_METHOD`, `LLM_PROVIDER_LABEL`. `.env.example` documents DeepSeek, OpenAI, Groq, OpenRouter and Ollama combinations; switching provider is a configuration change only (covered by a test and asserted end to end through the API).
- The client is built with LangChain (`ChatOpenAI` with `base_url`/`api_key`/`model`), so LangGraph can reuse the same model later. `LLM_STRUCTURED_METHOD` (default `json_mode`) selects native structured output; when a provider cannot do it, or a server-side failure occurs, the extractor falls back once to prompt-based JSON parsing. Client errors (4xx, timeouts, missing configuration) fail fast.
- Errors are mapped from any provider/library exception to the neutral LLM error types with the API key redacted and provider text collapsed and truncated to 200 characters, so the API can keep the same `409`/`502`/`503`/`504` contract.
- Master plan document (`UAE_VAT_Audit_MVP_Development_Plan.docx`) updated **in place** after backing it up to `UAE_VAT_Audit_MVP_Development_Plan_v1_backup.docx`: subtitle, product scope, target architecture, structure guidance, progress-reporting preamble, master instruction, the **Phase 3 prompt rewritten** for the provider-agnostic layer, a Revision 2 note appended to Phase 4 (rules are the agent's authoritative tools), the **Phase 5 heading and prompt rewritten** as "Agentic Audit Engine (LangGraph)" with the graph diagram, Revision 2 notes appended to Phases 6 and 7, an API-contract note for `POST /documents/{document_no}/extract`, five agentic limitations and four agentic definition-of-done items. Result verified: 94 → 108 paragraphs, `word/document.xml` well-formed, all 18 zip entries intact.
- Tests renamed and reworked: `tests/deepseek_stubs.py` → `tests/llm_stubs.py` (scripted chat models that record every call and can refuse structured output), `tests/test_deepseek_config.py` → `tests/test_llm_config.py`, `tests/test_deepseek_client.py` → `tests/test_llm_extractor.py`.

### Revision 2 files created or changed
- `backend/app/services/llm.py`, `backend/app/services/llm_prompts.py`, `backend/app/services/llm_errors.py` (new).
- `backend/app/services/deepseek_client.py` (deleted), `backend/tests/deepseek_stubs.py`, `backend/tests/test_deepseek_config.py`, `backend/tests/test_deepseek_client.py` (deleted).
- `backend/app/config.py` — `DeepSeekSettings`/`get_deepseek_settings` replaced by `LLMSettings`/`get_llm_settings` (env + `.env`, `missing_settings()`, method validation).
- `backend/app/services/invoice_extractor.py` — injects an `LLMExtractor` (was `DeepSeekClient`) and takes the provider label from configuration.
- `backend/app/api/documents.py` — neutral LLM error types, provider-neutral messages and Swagger text.
- `backend/app/models/invoice.py` — `InvoiceData.provider` default is now `openai-compatible`.
- `backend/app/main.py`, `backend/app/__init__.py` — Revision 2 scope wording only.
- `backend/.env.example` — `LLM_*` variables plus DeepSeek/OpenAI/Groq/OpenRouter/Ollama examples.
- `backend/requirements.txt` — added `langgraph`, `langchain-core`, `langchain-openai`.
- `backend/tests/llm_stubs.py`, `test_llm_config.py`, `test_llm_extractor.py` (new); `test_invoice_extraction.py` and `test_documents_extraction_api.py` updated to the new layer.
- `backend/README.md`, `backend/PROGRESS.md` — Revision 2 documentation.
- `UAE_VAT_Audit_MVP_Development_Plan.docx` (updated in place) and `UAE_VAT_Audit_MVP_Development_Plan_v1_backup.docx` (backup of the original).

### Revision 2 tests and results
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q` → **`128 passed in 2.43s`**, exit code `0`.
- `python -m pytest -q -W error::DeprecationWarning` → `128 passed in 2.33s` (no warnings).
- Live uvicorn check on port 8014 with a real text PDF: `LIVE_HEALTH 200`; `LIVE_UPLOAD 201`; `GET /documents/{no}` before extraction returns `invoice=null` with the guidance note; `POST /documents/{no}/extract` → `503 "The LLM is not configured: LLM_API_KEY, LLM_MODEL must be set. Copy .env.example to .env ..."`; unknown document `404`; `/docs 200`; no `.env` file was created.
- Plan document verification: `word/document.xml` is well-formed XML, paragraph count `94 → 108`, all 18 zip entries preserved, and the backup still contains the original Phase 5 heading.
- One failure occurred while refactoring: `NameError: name 'StubClient' is not defined` in `tests/test_documents_extraction_api.py` (a rename I missed); fixed by switching that call to `StubExtractor`. As before, no live provider call was possible (no usable key in this environment), so every provider path is exercised through scripted models.
- Acceptance: no vendor-specific LLM module or class remains; provider/model/base URL are `.env`-only and asserted end to end; Phase 1 and Phase 2 tests and routes are unchanged and still pass; no Phase 4+ code exists.

## Completed work
- Added a DeepSeek (OpenAI-compatible) chat client in `app/services/deepseek_client.py` that sends the page-level text already stored by Phase 2 and requests JSON-mode structured invoice fields. The official base URL `https://api.deepseek.com` is the documented default and can be overridden.
- Hardened the prompt against prompt injection: invoice text is wrapped in explicit `<<<BEGIN_UNTRUSTED_INVOICE_TEXT>>>` delimiters, the system prompt states the document is untrusted data whose embedded instructions must never be followed, requires `null` instead of guessing, and allows only the four documented VAT classifications.
- Added provider configuration loaded from the environment and `backend/.env` through `pydantic-settings`: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_TIMEOUT_SECONDS`, plus a request-size cap `DEEPSEEK_MAX_INPUT_CHARACTERS`. The key is held as `SecretStr`, never hardcoded, never logged (there is no logging call anywhere in `app/`) and never returned by the API.
- Added `.env.example` with placeholders only, and a `!.env.example` negation in `.gitignore` because the existing `.env.*` rule would otherwise hide it. No real `.env` was created.
- Added `app/models/invoice.py` with two tiers: a lenient `RawInvoiceFields` model that validates only the provider response's shape/types, and a strict `InvoiceData` model whose monetary fields are `Decimal` (serialised as strings in JSON so precision cannot be lost).
- Added `app/services/invoice_extractor.py`, which parses amounts into `Decimal` **after** Pydantic validation (handles `AED 1,000.00`, `1.234,56`, `(50.00)`, negatives, `n/a`), never guesses missing values, keeps non-ISO dates verbatim with a warning, restricts classification to `standard_rated`/`zero_rated`/`exempt`/`unknown`, validates per-field confidence and page numbers, and records every judgement call as a warning.
- Added `POST /documents/{document_no}/extract` (explicit trigger: extraction is deliberately not run during upload, so Phase 2 upload behaviour and key independence stay intact) which stores the result in the registry and returns it. Repeat calls reuse the stored extraction; `force=true` calls the provider again.
- Extended `GET /documents/{document_no}` to expose the stored `invoice` block (fields, per-field evidence, `missing_fields`, `warnings`, provider, model, timestamp) plus notes explaining the LLM provenance and that extraction has not run yet. The Phase 2 rule that page text is withheld unless `include_text=true` is unchanged.
- Mapped failures to safe HTTP responses without secrets or provider payload dumps: `409` needs OCR, `502` provider failure or unusable/invalid model output, `503` missing API key (no request is attempted), `504` timeout. A failed extraction stores nothing.
- Kept Phase 3 in scope: no VAT rules, no audit engine and no `POST /audit/{document_no}` (confirmed by scanning `app/` for `rules_config`, `HUMAN_REVIEW_CONFIDENCE_THRESHOLD` and `/audit`).

## Files created or changed
Note: the Revision 2 refactor above superseded the DeepSeek-specific files listed
here (they were renamed or deleted); the Revision 2 file list is the current state.
- `backend/app/models/invoice.py` (new) — `VatClassification`, `RawInvoiceFields`, `RawFieldEvidence`, `FieldEvidence`, `InvoiceData`, `InvoiceExtractionResponse`, field-name constants.
- `backend/app/services/deepseek_client.py` (new) — system/user prompts, `DeepSeekClient`, `RawExtraction`, typed errors, error sanitisation, JSON fence stripping, completion parsing.
- `backend/app/services/invoice_extractor.py` (new) — Decimal/date/classification/confidence normalisation, `build_invoice_data`, `InvoiceExtractionService`.
- `backend/.env.example` (new) — placeholder provider configuration for copying to `.env`.
- `backend/tests/deepseek_stubs.py` (new) — deterministic settings, response bodies, recording/failure transports and a recording stub client.
- `backend/tests/test_deepseek_config.py` (new) — environment/`.env` configuration unit tests.
- `backend/tests/test_deepseek_client.py` (new) — client tests using mocked HTTP only.
- `backend/tests/test_invoice_extraction.py` (new) — normalisation and service tests.
- `backend/tests/test_documents_extraction_api.py` (new) — API-level extraction tests with DeepSeek mocked.
- `backend/app/config.py` (changed) — added `DeepSeekSettings` and cached `get_deepseek_settings()`; the Phase 2 `Settings` class is untouched.
- `backend/app/main.py` (changed) — module docstring and Swagger description now state the Phase 3 scope. No route, version or behaviour change.
- `backend/app/__init__.py` (changed) — package docstring now states the Phase 3 scope; `__version__` intentionally left at `0.2.0`.
- `backend/app/models/document.py` (changed) — `DocumentDetail` gained an optional `invoice` field (default `None`).
- `backend/app/storage/memory.py` (changed) — `DocumentRecord` gained an optional `invoice` field (default `None`) and the registry gained `set_invoice()`; no Phase 2 behaviour changed.
- `backend/app/api/documents.py` (changed) — extraction route, `get_invoice_extraction_service` dependency, `_require_document` helper, invoice exposure in `GET`. The inline 404 block in `GET` was moved into `_require_document` unchanged (same status, same message).
- `backend/requirements.txt` (changed) — added `pydantic-settings>=2.2`.
- `backend/.gitignore` (changed) — added `!.env.example`.
- `backend/README.md` (changed) — Phase 3 endpoints, `.env` setup, error table, structure, roadmap, limitations.
- `backend/PROGRESS.md` (changed) — this log.

## Commands executed
- `python -c "import importlib.metadata as m; ..."` → dependency check: `pydantic-settings 2.14.0`, `python-dotenv 1.2.2`, `httpx 0.28.1`, `pydantic 2.13.3`, `fastapi 0.136.1` (no install step was needed)
- `python -c "from app.main import app; print(sorted((r.path, tuple(sorted(r.methods))) for r in app.routes ...))"` → routes: `/health`, `/documents/upload`, `/documents/{document_no}`, `/documents/{document_no}/extract`, `/docs`, `/openapi.json`, `/redoc`
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q` → first run `7 failed, 111 passed in 2.67s`; second run `1 failed, 117 passed in 2.39s`; final `118 passed in 1.09s` (failures and fixes listed below)
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q -W error::DeprecationWarning` → `118 passed in 0.73s`, exit code `0` (no warnings)
- uvicorn on port 8013 plus a live `httpx` script piped into `python -` (results below), server stopped afterwards
- Source scans with `Select-String` over `app/*.py`: `logging|print(|logger` → no matches (nothing can log the key); `get_secret_value` → only `config.py:124`; `rules_config|HUMAN_REVIEW_CONFIDENCE_THRESHOLD|/audit` → no matches
- `Get-ChildItem -Recurse -Force -File` on `backend/` → `.env.example` exists and no `.env` was created

## Tests and results
- Command: `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q`
- Result: `118 passed in 1.09s` — the 21 existing Phase 1/2 tests (unchanged, still passing) plus 97 new Phase 3 tests. Re-run with `-W error::DeprecationWarning`: `118 passed in 0.73s`, exit code `0`.
- Phase 3 coverage: `.env`/environment configuration (defaults, overrides, dotenv file loading, blank key treated as missing, secret absent from `repr`, non-positive timeout/cap rejected, settings cache behaviour); client request shape (endpoint URL, `Bearer` header, model, `response_format=json_object`, temperature 0, page-labelled and delimited untrusted text); prompt hardening statements; fenced JSON acceptance; non-JSON, non-object JSON and schema-mismatch rejection; empty choices; timeout, connection error and 401/429/500 handling with key redaction and message truncation; missing key with no HTTP call attempted; long-document truncation warning; money formats (`AED 1,000.00`, `1.234,56`, `1000,00`, `(50.00)`, `1,234`, `5%`, junk text, `nan`, boolean); date normalisation; classification aliases plus unknown handling; confidence units and junk; missing-field behaviour; evidence validation (unknown field, bad page, page beyond document, junk confidence); warning cap; service behaviour (pages sent, needs-OCR refusal without a provider call, error propagation, lazy client creation); API behaviour (stored invoice data, GET exposure, reuse vs `force`, 404, 409, 502, 503, 504, nothing stored on failure, no key or `authorization` string in any response, OpenAPI route and `InvoiceData` classification enum).
- Live server check (uvicorn on port 8013, real HTTP, no API key configured):
  - `LIVE_HEALTH 200 {'status': 'ok'}`
  - `LIVE_UPLOAD 201 DOC-8D97D3B4DBF147A88C5425C614781824 extracted 1`
  - `LIVE_GET_BEFORE invoice= None | note= ['Structured invoice fields have not been extracted yet; ...']`
  - `LIVE_EXTRACT_NO_KEY 503 | The DeepSeek API key is not configured. Copy .env.example to .env in the backend directory and ...`
  - `LIVE_EXTRACT_UNKNOWN_DOC 404`
  - `LIVE_GET_AFTER invoice= None` (a failed extraction stores nothing)
  - `LIVE_PATHS ['/documents/upload', '/documents/{document_no}', '/documents/{document_no}/extract', '/health']`
  - `LIVE_SCHEMAS` include `InvoiceData`, `FieldEvidence`, `InvoiceExtractionResponse` and `DocumentDetail`
  - `LIVE_DOCS 200`
- Failures and fixes (every first-run failure was in new Phase 3 code or new Phase 3 tests):
  1. `7 failed, 111 passed` — `normalise_classification` used the shared missing-value filter, so the literal `"unknown"` was treated as absent and the human-confirmation warning was never added; fixed by treating only `None`/blank as missing in that function and unifying the warning wording. The other failures came from the new API test fixture returning the wrapped service instead of the stub client (`AttributeError: 'InvoiceExtractionService' object has no attribute 'calls'`); fixed by returning the object the test passed in.
  2. `1 failed, 117 passed` — the truncation test asserted that 200 `Z` characters reach the prompt, but the 200-character cap includes the `=== PAGE 1 ===` header, so only 187 remain; the test now asserts the exact truncated document text instead.
- Limitation of this verification: nothing was exercised against the live DeepSeek API, because no key is available in this environment. All provider interaction is covered by mocked transports and the real-HTTP check covers the configuration-error path, so model output quality and the `deepseek-chat` default remain unverified.

## Acceptance criteria
- [x] Existing Phase 1 and Phase 2 tests and routes still work unchanged — all 21 Phase 1/2 tests pass with no edits to their assertions or expectations; the only Phase 2 code touched is additive (optional `invoice` field, `set_invoice()`, and moving the unchanged 404 block into `_require_document`).
- [x] `.env` controls provider URL, model, key and timeout — `DEEPSEEK_BASE_URL`, `DEEPSEEK_MODEL`, `DEEPSEEK_API_KEY` and `DEEPSEEK_TIMEOUT_SECONDS` are read from the environment/`.env`, covered by `tests/test_deepseek_config.py`, with the official DeepSeek URL as the documented default and `.env.example` providing placeholders only.
- [x] Extraction is performed through the DeepSeek API with validated structured output — JSON-mode `chat/completions` call, `RawInvoiceFields` Pydantic validation, `Decimal` amounts parsed after validation, per-field evidence and confidence, warnings for every uncertain value. Exercised with mocked transports because no key is available here.
- [x] No VAT audit decisions are implemented yet — there is no rules engine, no audit service and no `/audit` route.

## Known issues / decisions
- Extraction is triggered by an explicit `POST /documents/{document_no}/extract` rather than during upload or implicitly on `GET`. Reason: the Phase 2 upload path must keep working without an API key, and `GET` must stay read-only. `GET` therefore exposes the stored invoice data (as the Phase 3 requirement asks) and returns `invoice: null` with a guidance note before extraction runs.
- `DEEPSEEK_MAX_INPUT_CHARACTERS` (default 60,000) goes beyond the literal requirement so a long document cannot produce an unbounded, expensive request; truncation is always reported as a warning.
- Amount parsing accepts realistic variants but never guesses: `1,234` and `1.234,56` are parsed and the reading is stated in a warning, while `12.34.56` or `one thousand` become `null` with a warning and are listed in `missing_fields`.
- Non-ISO dates are kept verbatim with a warning because day/month order cannot be inferred; they are not silently rewritten.
- The literal `"unknown"` is a valid `vat_classification`, so it is not treated as a missing value even though other text fields treat it as a placeholder.
- The application version string remains `0.2.0`: bumping it would have required editing the Phase 2 test that pins `0.2.0`, which the plan forbids without asking first, so it is flagged here instead of changed.
- The API key cannot be logged: `app/` contains no `logging` or `print` calls at all, and provider error payloads are whitespace-collapsed, key-redacted and truncated to 200 characters.
- `pydantic-settings`, `httpx` and PyMuPDF were already installed globally, so no dependency installation step was needed.

## Next phase
Phase 4 — Configurable VAT Rules Engine (Revision 2: the ruleset becomes the agent's
authoritative tool input, per the updated master plan). Not started; this session
covered only the Phase 3R refactor and the master plan update.

---

# Phase 2 — PDF Upload and Text Extraction (Completed, archived)

## Status
Completed (verified in Phase 2; superseded by the Phase 3 work log above)

## Completed work
- Added an in-memory document registry (`app/storage/memory.py`) that generates
  unique `DOC-<uuid hex>` document numbers and stores metadata plus page-level
  text. Uploaded PDF bytes are never written to permanent storage, and records
  are lost when the process restarts.
- Added `app/services/pdf_extractor.py`: uploads are validated by content first
  (the `%PDF-` signature is searched in the leading 1024 bytes, so filename and
  declared content type are never trusted) and then a real PyMuPDF read is
  required. Empty, corrupted, truncated, zero-page, and password-protected
  PDFs are rejected with typed errors instead of being stored.
- Added `POST /documents/upload` (one PDF as multipart/form-data in the `file`
  field) returning a generated document number and an extraction summary, with
  `400`/`413`/`415` error semantics for empty, corrupted, oversized, and
  non-PDF uploads.
- Added `GET /documents/{document_no}` returning stored metadata and per-page
  summaries. The full extracted text is withheld by default and returned only
  when `include_text=true`; a length-capped 80-character preview is included.
- Text is extracted page by page with 1-based page numbers. A PDF with no
  extractable text is stored as `extraction_status="needs_ocr"` with an
  explanatory `extraction_error` and a warning in the response, rather than
  being treated as read.
- Added configurable settings in `app/config.py`: maximum upload size 10 MB
  (`VAT_AUDIT_MAX_UPLOAD_BYTES`), upload read chunk size, and the signature scan
  window. Uploads are streamed into memory in chunks, so an oversized file is
  rejected without buffering the whole payload first.
- Kept `GET /health` working and wired the documents router into the app.
- Kept Phase 2 strictly in scope: no invoice field extraction, no VAT rules, and
  no audit endpoint were added.

## Files created or changed
- `backend/app/config.py` (new) — configurable limits with environment overrides.
- `backend/app/models/__init__.py` (new) — models package marker.
- `backend/app/models/document.py` (new) — `ExtractionStatus`, `PageContent`, `DocumentSummary`, `DocumentDetail`.
- `backend/app/services/__init__.py` (new) — services package marker.
- `backend/app/services/pdf_extractor.py` (new) — signature validation, PyMuPDF extraction, typed errors.
- `backend/app/storage/__init__.py` (new) — storage package marker.
- `backend/app/storage/memory.py` (new) — `DocumentRecord`, `InMemoryDocumentRegistry`, `document_registry`, document-number generation.
- `backend/app/api/documents.py` (new) — upload and metadata routes plus response mapping.
- `backend/tests/conftest.py` (new) — `client` fixture and registry isolation between tests.
- `backend/tests/pdf_factory.py` (new) — builds text and image-only PDFs in memory.
- `backend/tests/test_documents.py` (new) — 19 Phase 2 tests.
- `backend/app/main.py` (changed) — version `0.2.0`, Phase 2 description, documents router included.
- `backend/app/__init__.py` (changed) — docstring updated for Phase 2 scope and `__version__` bumped to `0.2.0`.
- `backend/requirements.txt` (changed) — added `PyMuPDF>=1.24` and `python-multipart>=0.0.9`.
- `backend/README.md` (changed) — endpoint reference, upload/retrieve examples, configuration table, project structure, roadmap, limitations.
- `backend/PROGRESS.md` (changed) — this log.

## Commands executed
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q` → first run: `1 failed, 19 passed in 1.40s`; after fixing the test fixture: `21 passed in 0.25s`
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q -W error::DeprecationWarning` → `21 passed in 0.25s`, exit code `0` (no warnings)
- `Start-Process python -ArgumentList '-m','uvicorn','app.main:app','--port','8012','--log-level','warning' -PassThru` followed by a live `httpx` client script piped into `python -`, then `Stop-Process` (see Tests and results)
- `python -c "import fitz; ..."` probe on garbage bytes after a `%PDF-1.4` header → `pymupdf.FileDataError: Failed to open stream`, confirming the corrupted-PDF rejection path is real
- `Get-ChildItem -Recurse -Force -File` on `backend/` → file list verified below

## Tests and results
- Command: `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q`
- Result: `21 passed in 0.25s` (19 new Phase 2 tests in `tests/test_documents.py`
  plus the 2 Phase 1 health tests). Re-run with
  `-W error::DeprecationWarning`: `21 passed in 0.25s`, exit code `0`, no warnings.
- Phase 2 coverage: valid two-page text PDF (document number + summary), unique
  document numbers, content-based validation despite a misleading filename and
  content type, metadata retrieval without full text, `include_text=true` full
  text, case-insensitive lookup, non-PDF content (415), non-PDF bytes named
  `.pdf` (415), corrupted PDF (400), truncated PDF (400), empty file (400),
  missing `file` field (422), image-only PDF → `needs_ocr`, `needs_ocr`
  limitation disclosure, unknown document number (404), oversize upload (413),
  upload exactly at the limit (201), OpenAPI paths, and `GET /health`.
- Live server check (uvicorn on port 8012, real HTTP via an httpx client script):
  - `LIVE_HEALTH 200 {'status': 'ok'}`
  - `LIVE_UPLOAD 201 DOC-08AA1252873D4CE2A3F362D8A46E9B08 1 extracted 1 143`
  - `LIVE_GET 200 raw_text_included= False pages= 1 preview= 'Invoice No: INV-2024-0001 Supplier: Al Noor Trading LLC Supplier TRN: 1001234567...'`
  - `LIVE_GET_FULLTEXT True 'Invoice No: INV-2024-0001\nSupplier: Al Noor T'`
  - `LIVE_404 404`
  - `LIVE_DOCS 200 OPENAPI 200 ['/documents/upload', '/documents/{document_no}', '/health']`
- Failures or limitations: the first run of the new suite reported
  `1 failed, 19 passed in 1.40s`. The failure was in the test, not the
  application: the oversized-upload test assumed a large PDF would exceed 2048
  bytes, but PyMuPDF compresses the content stream, so the payload was 990 bytes
  and the request legitimately succeeded. The test now derives the limit from the
  real payload (`len(pdf) - 1`) and a boundary test was added for an upload
  exactly at the limit. After that fix the suite passes with no warnings.

## Acceptance criteria
- [x] Valid text PDF returns generated `document_no` and extraction summary — verified by tests and by a live HTTP upload (`201`, `DOC-08AA1252873D4CE2A3F362D8A46E9B08`, `extracted`, 1 page).
- [x] GET endpoint can retrieve the uploaded document metadata — `GET /documents/{document_no}` returned `200` with size, page count, extraction status, page summaries and notes.
- [x] No database or permanent file storage — storage is a plain in-memory dictionary; the app never writes uploaded bytes to disk and retains only extracted text.
- [x] All Phase 2 tests pass or blockers are documented — `21 passed in 0.25s`, no blockers.
- [x] Do not implement invoice field extraction or VAT rules yet — no invoice-field extraction, no rules engine and no `POST /audit/{document_no}` route exist (the OpenAPI path list is `/health`, `/documents/upload`, `/documents/{document_no}`).

## Known issues / decisions
- Validation is deliberately content-based: a `.txt` file containing a valid PDF
  is accepted, while a `.pdf` file without a PDF signature is rejected with 415.
  The signature is searched in the leading 1024 bytes rather than at offset 0,
  because some tools emit a few bytes before `%PDF-`.
- A PDF that PyMuPDF opens with zero readable pages is treated as corrupted
  (400), which keeps the "corrupted files are rejected" requirement deterministic
  even when MuPDF is lenient.
- The `needs_ocr` decision is made in the route from the extraction result; the
  extractor itself stays a pure "bytes in, pages out" service. `extraction_error`
  carries the OCR explanation so the limitation is visible in the API response.
- Raw uploaded PDF bytes are intentionally not retained, only the extracted text,
  to keep memory usage bounded. Phase 3 reuses that stored text; if a later phase
  needs to re-parse the original file, the storage layer must change.
- `app/models/document.py` was created in Phase 2 (the plan's tree lists models
  for Phases 3–4). Document schemas are needed now, and the plan explicitly
  allows structural adjustments with a clear reason.
- The `413` status code is written as a literal because Starlette 1.0 deprecated
  `HTTP_413_REQUEST_ENTITY_TOO_LARGE` in favour of `HTTP_413_CONTENT_TOO_LARGE`;
  the literal avoids both the deprecation warning and incompatibility with older
  FastAPI/Starlette releases.
- Starlette may spool a large multipart upload to a temporary file while it is
  being received. This is transient framework behaviour, not application storage,
  and the bytes are read into memory and discarded afterwards.
- Document-number lookups are case-insensitive and whitespace-trimmed for
  convenience, since the number is copied by hand from Swagger.
- No virtual environment was created; the global Python 3.13.7 already provides
  every dependency in `requirements.txt` (fastapi 0.136.1, uvicorn 0.46.0,
  pydantic 2.13.3, pytest 9.1.0, httpx 0.28.1, PyMuPDF 1.27.2.3,
  python-multipart 0.0.27).

## Next phase
Phase 3 — Structured Invoice Field Extraction (this phase was subsequently
completed; see the Phase 3 work log at the top of this file)

---

# Phase 1 — Project Setup and FastAPI (Completed, archived)

## Status
Completed (verified in Phase 1; superseded by the Phase 2 work log above)

## Completed work
- Inspected the workspace before making changes: it contained only
  `UAE_VAT_Audit_MVP_Development_Plan.docx` and a 0-byte file named `docs`
  (no code, no `PROGRESS.md`, no `.git`, no virtual environment). Nothing was
  overwritten.
- Read the entire master plan (all six phases and the master instruction) from
  the plan document before implementing.
- Created the project scaffold under `backend/` with the `app/` package,
  `app/api/` placeholder package, `tests/` package, `requirements.txt`,
  `.gitignore`, `README.md`, and this file.
- Implemented `GET /health` returning `{"status": "ok"}` with a declared
  `HealthResponse` Pydantic response model, so Swagger UI shows a real schema.
- Set a meaningful API title (`UAE VAT Audit API`), version (`0.1.0`), and a
  description that states the MVP scope and the non-certification disclaimer.
- Added tests for the health endpoint and ran them (2 passed).
- Verified the real server: `uvicorn app.main:app` answered `/health`,
  `/docs`, and `/openapi.json` with HTTP 200, and the `--reload` form also
  booted and answered `/health`.
- Kept Phase 1 strictly in scope: no database, ORM, frontend, Docker,
  authentication, PDF handling, or VAT logic was added.

## Files created or changed
- `backend/app/__init__.py` (new) — package docstring and `__version__ = "0.1.0"`.
- `backend/app/main.py` (new) — FastAPI app instance, metadata, `HealthResponse`, `GET /health`.
- `backend/app/api/__init__.py` (new) — empty route package reserved for Phase 2 (`documents.py`) and Phase 5 (`audit.py`).
- `backend/tests/__init__.py` (new) — makes `tests` a package so `app` is importable however pytest is invoked.
- `backend/tests/test_health.py` (new) — two tests for `GET /health`.
- `backend/requirements.txt` (new) — `fastapi>=0.115`, `uvicorn[standard]>=0.30`, `pydantic>=2.7`, `pytest>=8.0`, `httpx>=0.27`.
- `backend/.gitignore` (new) — Python, venv, pytest, and editor ignores.
- `backend/README.md` (new) — scope, requirements, setup, run command, Swagger URL, health example, structure, roadmap, limitations.
- `backend/PROGRESS.md` (new) — this progress log.

No existing file was modified or deleted.

## Commands executed
- `python --version` → `Python 3.13.7` (also available: `py -3.11`)
- `python -m pip list` (filtered) → `fastapi 0.136.1`, `uvicorn 0.46.0`, `pydantic 2.13.3`, `pytest 9.1.0`, `httpx 0.28.1`, `starlette 1.0.0`
- `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q` → `2 passed in 0.49s`
- `Start-Process python -ArgumentList '-m','uvicorn','app.main:app','--port','8010','--log-level','warning' -PassThru` plus `Invoke-WebRequest` on `/health`, `/docs`, `/openapi.json`, then `Stop-Process`
- `Start-Process python -ArgumentList '-m','uvicorn','app.main:app','--reload','--port','8011','--reload-delay','1' -PassThru` plus `Invoke-WebRequest` on `/health`, then termination by PID
- `Get-CimInstance Win32_Process` check for leftover `uvicorn` processes → none remaining
- `Get-ChildItem -Recurse -Force -File` on `backend/` → confirmed the project files below

## Tests and results
- Command: `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q`
- Result: `2 passed in 0.49s` (Python 3.13.7, pytest 9.1.0). The two tests are
  `test_health_returns_200_and_ok_status` and
  `test_health_response_matches_declared_contract`.
- Live server check (no reload), port 8010:
  - `HEALTH_STATUS=200`, `HEALTH_BODY={"status":"ok"}`
  - `DOCS_STATUS=200`, `DOCS_IS_SWAGGER=True` (Swagger UI HTML served)
  - `OPENAPI_TITLE=UAE VAT Audit API`, `OPENAPI_VERSION=0.1.0`, `OPENAPI_PATHS=/health`
- Live server check (`--reload`), port 8011:
  - `RELOAD_HEALTH_STATUS=200`, `RELOAD_HEALTH_BODY={"status":"ok"}`
- Failures or limitations: none. Both smoke-test servers were stopped and no
  leftover `uvicorn` process was found afterwards. Ports 8010/8011 were used
  instead of the default 8000 only to avoid clashing with anything already
  bound there; the documented command remains `uvicorn app.main:app --reload`.
- Final verification rerun after writing the documentation:
  `Set-Location 'd:\Ai\SuperAuditor\backend'; python -m pytest -q` →
  `2 passed in 0.32s`, exit code `0`.
- Source scan for out-of-scope artefacts: searching all `*.py` for
  `fitz|PyMuPDF|multipart|sqlalchemy|UploadFile|File(` returned no matches, and
  the only declared route is `@app.get` (the health check).

## Acceptance criteria
- [x] App starts with the documented command — `uvicorn app.main:app --reload` booted and answered `GET /health` with 200 (verified on port 8011).
- [x] `GET /health` returns 200 and `{"status":"ok"}` — verified by the test suite and against a live server in both runs.
- [x] Swagger loads — `GET /docs` returned 200 with Swagger UI HTML and `GET /openapi.json` returned the schema for `/health`.
- [x] Health test passes — `2 passed in 0.49s`.
- [x] Report actual commands and results — listed above, copied from real executions.
- [x] Do not implement Phase 2 — no PDF, multipart, or storage code was added; `PyMuPDF` and `python-multipart` were not added to `requirements.txt`.

## Known issues / decisions
- The workspace entry `docs` is a 0-byte **file**, not a directory, and the plan
  text lives in `UAE_VAT_Audit_MVP_Development_Plan.docx`. The plan was read by
  extracting the document text in memory. No `docs/` directory was created
  because that is not part of the Phase 1 deliverable.
- The project root is `backend/` (per user instruction) instead of the
  `vat-audit/` name shown in the plan tree; the internal layout matches the
  plan exactly.
- Phase 1 defines `HealthResponse` inside `app/main.py` rather than creating an
  `app/models/` package; the plan's tree introduces typed models in Phase 3, and
  adding that package now would be out of scope.
- `tests/__init__.py` was added (not shown in the plan tree) so pytest puts the
  project root on `sys.path` and `from app.main import app` works both with
  `python -m pytest` and bare `pytest`.
- `httpx` is listed in `requirements.txt` because it is the transport used by
  `fastapi.testclient.TestClient`.
- No virtual environment was created: the required packages were already present
  in the global Python 3.13.7 interpreter, so no install step was needed. The
  README documents the optional `python -m venv .venv` setup for isolated
  installs.
- The workspace is not a git repository, so no commit was made; `.gitignore` is
  provided for when it is initialised.
- Money and VAT calculations do not exist yet, so the `Decimal` requirement does
  not apply in this phase.

## Next phase
Phase 2 — PDF Upload and Text Extraction (this phase was subsequently completed;
see the Phase 2 work log at the top of this file)
