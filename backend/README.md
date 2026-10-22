# UAE VAT Audit API

Minimal FastAPI service for auditing UAE VAT invoices. The MVP workflow is:
**upload an invoice PDF → receive a generated document number → run an audit by
document number → inspect the structured JSON findings in Swagger UI.**

This repository currently contains **Phases 1–3** (Revision 2): the project scaffold,
a working `GET /health` endpoint, in-memory PDF upload with page-level text
extraction, and structured invoice field extraction through **any
OpenAI-compatible LLM** selected purely through `.env`. The configurable VAT rules
engine and the LangGraph agentic audit are implemented in later phases.

## Scope of the MVP

Implemented so far:

- `GET /health` liveness check.
- `POST /documents/upload`: PDF validation by content, an in-memory upload with
  a configurable size limit, page-by-page text extraction, and a generated
  `DOC-<uuid>` document number.
- `POST /documents/{document_no}/extract`: sends the stored page text to the
  configured LLM (DeepSeek, OpenAI, Groq, Together, OpenRouter, Ollama, vLLM,
  LM Studio, …) and stores schema-validated invoice fields (invoice number, date,
  supplier and customer details and TRNs, currency, amounts, VAT rate and
  classification) with per-field evidence, confidence percentages and extraction
  warnings.
- `GET /documents/{document_no}`: stored metadata, per-page extraction summaries
  and the structured `invoice` block, with the full extracted text withheld unless
  explicitly requested.
- A configurable, versioned **VAT rules engine** (`app/audit/rules_config.py`) that
  validates the ruleset and exposes compact tool payloads for the auditor.
- An **agentic audit engine** (`app/audit/`) that evaluates an extracted invoice
  against the enabled rules, recomputes every VAT amount in Python with `Decimal`,
  validates all model output, and stores one report per document in memory. It exposes
  no HTTP endpoint yet: that is Phase 6.
- `POST /analysis`: text extraction, structured invoice extraction and the VAT audit in
  one self-contained request, so the API works on serverless hosts (Vercel, Lambda,
  Cloud Run) where process-local memory cannot be shared between requests. See
  [Serverless / multi-instance deployments](#serverless--multi-instance-deployments-vercel-lambda-cloud-run).

Planned: nothing — every phase in the roadmap below is implemented.

Deliberately out of scope: database, ORM, frontend, ERP integration,
authentication, and Docker. An LLM is used for *field extraction and explanation
only*; VAT decisions stay in deterministic code. PaddleOCR is the single optional OCR
dependency, used only for scanned pages that have no usable text layer.

## Requirements

- Python 3.11 or newer
- FastAPI, Uvicorn, Pydantic, pydantic-settings, pytest, httpx, pdfplumber,
  python-multipart, and the LLM layer (LangGraph, LangChain core and
  langchain-openai) — see `requirements.txt`
- Optional: PaddleOCR for the scanned-page fallback —
  `.\.venv\Scripts\python.exe -m pip install -r requirements-ocr.txt`
- An API key for an OpenAI-compatible provider, needed **only** by
  `POST /documents/{document_no}/extract`
- Node.js 18.18 or newer — needed **only** for the optional Next.js UI in `frontend/`

## Configuration

Application limits (Phase 2):

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VAT_AUDIT_MAX_UPLOAD_BYTES` | `10485760` (10 MB) | Maximum accepted upload size. |
| `VAT_AUDIT_READ_CHUNK_SIZE_BYTES` | `1048576` (1 MB) | Chunk size used while streaming an upload into memory. |
| `VAT_AUDIT_PDF_SIGNATURE_SCAN_BYTES` | `1024` | Leading bytes scanned for the `%PDF-` signature. |
| `VAT_AUDIT_CORS_ALLOW_ORIGINS` | `http://localhost:3000,http://127.0.0.1:3000` | Comma-separated browser origins allowed to call the API (the Next.js UI). `*` allows any origin; credentials stay disabled because the API has no sessions. |

OCR fallback for scanned pages (Phase 9). The fallback runs **only** on pages whose
`pdfplumber` text layer is unusable, and it is skipped entirely when PaddleOCR is not
installed (the pages are then reported as `needs_ocr`).

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VAT_AUDIT_OCR_ENABLED` | `true` | Allow the PaddleOCR fallback to run. |
| `VAT_AUDIT_OCR_LANGUAGE` | `en` | PaddleOCR language code (ignored once a model name is set). |
| `VAT_AUDIT_OCR_DEVICE` | `cpu` | Device PaddleOCR runs on: `cpu` (no GPU required), `gpu:0`, `npu`, … |
| `VAT_AUDIT_OCR_ENABLE_MKLDNN` | `false` | oneDNN acceleration. Off because PaddlePaddle 3.x raises `NotImplementedError` inside oneDNN on some CPU builds. |
| `VAT_AUDIT_OCR_CPU_THREADS` | `4` | Threads for the CPU kernels; PaddleOCR's own default of 10 oversubscribes a laptop CPU. |
| `VAT_AUDIT_OCR_DET_MODEL` | `PP-OCRv5_mobile_det` | Detection model. Empty means "let PaddleOCR choose" (its PP-OCRv6 medium default). |
| `VAT_AUDIT_OCR_REC_MODEL` | `en_PP-OCRv5_mobile_rec` | Recognition model; language-specific, so change it with the language or clear both. |
| `VAT_AUDIT_OCR_DPI` | `150` | Resolution used to render a page for OCR; 200 is more accurate for small print and about twice as slow on a CPU. |
| `VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS` | `12` | A page whose collapsed text is shorter than this counts as having no usable text layer and becomes an OCR candidate. |
| `VAT_AUDIT_OCR_MAX_PAGES` | `25` | Upper bound on how many pages one upload may OCR. |

The defaults are a **CPU-only laptop profile**: they were measured on a real scanned
invoice (1 page, 367 KB) that PDF extraction alone could not read. With PaddleOCR 3.7.0
and PaddlePaddle 3.3.1 (CPU build) the page went from ~166 s to ~28 s, recovering the
invoice number, both TRNs, the dates, the taxable amount, the VAT and the total with no
quality findings. What each part of the profile buys, on that same page:

| Change | Effect |
| ------ | ------ |
| `device=cpu` instead of `use_gpu` | PaddleOCR 3.x removed `use_gpu`; sending it fails with `ValueError: Unknown argument: use_gpu`. |
| `enable_mkldnn=false` | oneDNN fails on that PaddlePaddle build (`NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support`), which lost the whole page. |
| mobile models instead of PP-OCRv6 medium | ~4× faster per page. |
| `cpu_threads=4` instead of 10 | ~1.5× faster on a laptop CPU. |
| `DPI=150` instead of 200 | ~2× faster, same fields recovered. |
| document pre-processing off | Skips two extra models that only help a photographed, tilted page — we OCR a page we rasterised ourselves. |

The first OCR run downloads the selected models (once, needs internet); later runs use the
PaddleOCR cache (`%USERPROFILE%\.paddlex\official_models` on Windows).

A page is also an OCR candidate when it is image-dominated (an embedded scan covers
60% or more of it) and its text stays below ten times that threshold — which catches
scans whose text layer holds only a scanner stamp. OCR never replaces a page's text
when it reads *less* than the text layer already had.

Invalid or non-positive values fall back to the defaults.

LLM provider settings (Phase 3, Revision 2), read from the environment first and
from `backend/.env` second. **No vendor is hardcoded**: switching provider is a
configuration change only.

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `LLM_API_KEY` | *(none)* | Provider credential. Required before extraction runs; a missing value produces `503` with a clear configuration error. |
| `LLM_MODEL` | *(none)* | Model identifier, for example `deepseek-chat`, `gpt-4o-mini`, `llama3.1`. Required before extraction runs. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | Provider base URL; the client posts to `<base>/chat/completions`. |
| `LLM_TIMEOUT_SECONDS` | `60` | Per-request timeout. |
| `LLM_MAX_INPUT_CHARACTERS` | `60000` | Safety cap on invoice text sent per request; longer documents are truncated and an extraction warning is recorded. |
| `LLM_MAX_RETRIES` | `2` | Transport-level retries. |
| `LLM_STRUCTURED_METHOD` | `json_mode` | `json_mode`, `json_schema`, `function_calling` or `plain`. Unsupported strategies automatically fall back to prompt-based JSON parsing. |
| `LLM_PROVIDER_LABEL` | `openai-compatible` | Label recorded on extraction results, for example `deepseek` or `ollama-local`. |

Common provider combinations:

| Provider | `LLM_BASE_URL` | `LLM_MODEL` |
| -------- | -------------- | ----------- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-flash` or `deepseek-v4-pro` (provider model names change over time; the API rejects unknown ones with a clear 400 message) |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.1-70b-versatile` |
| OpenRouter | `https://openrouter.ai/api/v1` | `deepseek/deepseek-chat` |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1` |

Secrets are never hardcoded, logged or returned: the key is held in a `SecretStr`,
handed to the chat model, and provider failures are reduced to a short sanitised
message (key redacted, provider text truncated to 200 characters) before reaching
the API response.

### Configuring the LLM provider

```powershell
cd backend
Copy-Item .env.example .env
# then edit .env and set LLM_API_KEY and LLM_MODEL (and LLM_BASE_URL if not OpenAI)
```

`.env` is git-ignored (`.env.example` is committed with placeholders only). Never
paste a real key into chat, source control, or API documentation.

## VAT rules engine

The ruleset is **authoritative configuration**, never model output: deterministic
checks and the auditor read it, and any rule ID a model returns that is not
configured (or is disabled) is rejected and recorded as a warning.

| Rule ID | Check | Severity | Applies to |
| ------- | ----- | -------- | ---------- |
| `VAT_RATE_CHECK` | Reported rate vs expected rate | high | standard-rated, unknown |
| `SUPPLIER_TRN_CHECK` | Supplier TRN presence and 15-digit format | medium | all treatments |
| `CUSTOMER_TRN_CHECK` | Customer TRN presence and format | low | all treatments |
| `TAXABLE_AMOUNT_VAT_MISMATCH` | VAT vs taxable amount x applicable rate | high | standard, zero, exempt |
| `INVOICE_TOTAL_CHECK` | Total vs subtotal + VAT (+ supported adjustments) | high | all treatments |
| `VAT_CLASSIFICATION_REVIEW` | Unknown/ambiguous or unsupported treatment | medium | unknown, zero, exempt |

Deliberate behaviour:

- The 5% standard rate is **never** applied to zero-rated or exempt supplies, and an
  unknown treatment assumes no rate at all: such cases become `review_required`
  rather than guesses or confirmed violations.
- A missing customer TRN is a violation only when the configured scenario requires
  one; the rule is enabled but reports `not_applicable` otherwise.
- TRN checks validate format only. They do not establish that a TRN is registered
  with the Federal Tax Authority.
- Monetary comparisons use `Decimal`, an explicit rounding policy and the configured
  AED tolerance (default `0.01`).
- **Review these rules against current UAE FTA legislation and guidance before
  production use.**

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `VAT_AUDIT_MONETARY_TOLERANCE` | `0.01` | AED tolerance for monetary comparisons. |
| `VAT_AUDIT_HUMAN_REVIEW_CONFIDENCE_THRESHOLD` | `80` | Confidence (0-100) below which a finding must be reviewed. |
| `VAT_AUDIT_RULESET_VERSION` | `uae-vat-rules-1.0.0` | Version identifier recorded with every audit run. |
| `VAT_AUDIT_ROUNDING_POLICY` | `ROUND_HALF_UP` | `ROUND_HALF_UP`, `ROUND_HALF_EVEN`, `ROUND_DOWN` or `ROUND_UP`. |
| `VAT_AUDIT_REQUIRES_SUPPLIER_TRN` | `true` | Whether the configured scenario requires a supplier TRN. |
| `VAT_AUDIT_REQUIRES_CUSTOMER_TRN` | `false` | Whether a customer TRN is required. |
| `VAT_AUDIT_DISABLED_RULES` | *(none)* | Comma-separated rule IDs switched off for this deployment. |
| `VAT_AUDIT_MAX_AGENT_ITERATIONS` | `2` | Maximum model/re-extraction passes per audit (`1`-`5`). |

Invalid values fall back to the documented defaults. Rules are also validated:
duplicate IDs, a rate rule that targets zero-rated/exempt supplies, or a TRN rule
without a role are rejected at configuration time.

## Agentic audit engine

`app/audit/ai_auditor.py` runs a LangGraph state machine per audit and returns an
`AuditReport`. No HTTP endpoint is exposed yet (Phase 6).

```
START -> load_context -> select_rules -> deterministic_checks -> agent_review
      -> guardrail_validate -> (re_extract_fields -> select_rules
                              | human_review | aggregate_report | cannot_audit)
      -> aggregate_report -> END
```

| Module | Responsibility |
| ------ | -------------- |
| `state.py` | JSON-safe audit state (the invoice travels as a JSON dump; page text never enters the state) |
| `tools.py` | Deterministic tools: `list_rules`, `get_rule`, `get_invoice_field`, `get_page_evidence`, `compute_expected_vat`, `check_invoice_totals`, `flag_human_review` |
| `nodes.py` | The nine nodes, the Decimal checks, the guardrails, the confidence model |
| `graph.py` | LangGraph wiring, routing and loop bounds |
| `ai_auditor.py` | The service, plus the in-memory report store keyed by document number |

Divide of responsibility:

- **Arithmetic is ours, never the model's.** Rate, VAT, total and TRN checks are
  recomputed in Python with `Decimal`, the configured rounding policy and the AED
  tolerance. The model may explain a calculation and may call the arithmetic tools,
  but the deterministic result is authoritative.
- **Rules cannot be invented.** Model findings are validated against the ruleset:
  unknown, disabled or inapplicable rule IDs are dropped and recorded as warnings. A
  rule with no result from the model falls back to the deterministic result.
- **Conflicts require a human.** If the model disagrees decisively with our
  arithmetic, the deterministic values are kept, the finding becomes
  `review_required`, and the conflict is recorded as a warning and a review reason.
- **Uncertainty is never a pass.** Missing evidence, ambiguous treatment, an
  invalid status, missing confidence, unusable JSON, a timeout or a provider error all
  end in `review_required`, never in a clean pass.

### Statuses

| Status | Meaning |
| ------ | ------- |
| `completed` | Every applicable rule was evaluated with no warnings and no review flags. |
| `completed_with_warnings` | Evaluated, but something a reviewer should read (for example a dropped model finding). |
| `review_required` | At least one finding is flagged: low confidence, uncertain treatment, missing evidence, a model/deterministic conflict or a model failure. |
| `cannot_audit` | No audit was performed (needs OCR, no extracted fields, or no applicable rule). |

### Overall confidence

`overall_confidence_percent` is a **severity-weighted mean** of the finding
confidences (failures, warnings and `review_required` findings count double), then
capped:

| Cap | Applied when |
| --- | ------------ |
| `threshold - 1` | Any finding is below the review threshold or is flagged for human review |
| `70` | The model disagreed with the deterministic calculation |
| `70` | Key evidence is missing |
| `40` | The model could not be used |

`confidence_explanation` on the report states the mean and the caps that applied, so
a low-confidence critical finding can never be hidden behind an unexplained average.

### Bounds and cost

- `VAT_AUDIT_MAX_AGENT_ITERATIONS` (default `2`, range 1-5) limits passes through the
  model/re-extraction loop.
- At most 3 tool-calling rounds per model invocation; the final answer is then
  requested with tools disabled.
- `RECURSION_LIMIT = 50` LangGraph steps as a backstop.

### Data handling

- Full page text is never stored in the audit state, the trace or the report; page
  evidence is returned as a bounded excerpt.
- TRNs and other long digit runs are masked in evidence snippets, excerpt previews and
  stored values (for example `100****0003`). Field *values* used by the TRN format
  check remain readable to the model, because the 15-digit check needs them.
- The API key is never stored on a report, and provider errors are sanitised before
  they reach the report.
- Findings never assert fraud or definitive legal non-compliance; the report states
  that it is a review aid.


## Setup

The backend ships with a ready-to-use virtual environment at `backend/.venv`
(Python 3.13.7) that already contains every dependency from `requirements.txt`.
Activate it (or call its interpreter directly) before running anything:

```powershell
cd backend
.\.venv\Scripts\Activate.ps1                  # or: .\.venv\Scripts\python.exe <command>
python -m pip install -r requirements.txt     # already satisfied; re-run after editing requirements.txt
```

To rebuild the environment from scratch (another machine, or after upgrading
Python):

```powershell
cd backend
Remove-Item -Recurse -Force .venv
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`.venv/` is git-ignored, so the environment is local-only: nothing about it is
committed and no dependency is installed into the global Python. Every command
in this README can be run either after activation or with the explicit
`.\.venv\Scripts\python.exe` prefix.

## Run the API

```powershell
cd backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The same command after activation is `uvicorn app.main:app --reload`; invoking
uvicorn with the environment interpreter keeps the API on the project's pinned
dependencies instead of the global Python.

The API is then available at `http://127.0.0.1:8000` and Swagger UI at
<http://127.0.0.1:8000/docs>. The raw OpenAPI schema is at
<http://127.0.0.1:8000/openapi.json>.

### Testing the whole workflow in Swagger UI

A ready-to-upload example invoice ships with the project as `sample_invoice.pdf`. Run
the five calls in this order (each one: expand the row → **Try it out** → **Execute**):

| # | Call | What to enter | What you should see |
| - | ---- | ------------- | ------------------- |
| 1 | `GET /health` | nothing | `200 {"status": "ok"}` |
| 2 | `POST /documents/upload` | `file` = `sample_invoice.pdf`, content type `application/pdf` | `201` with `document_no` (for example `DOC-492145CF010B4D85AA2CF07A90AFFFEE`), `extraction_status: "extracted"`, `page_count: 1` |
| 3 | `POST /documents/{document_no}/extract` | `document_no` from step 2 | `200` after a few seconds (this call uses the configured provider) with the invoice fields: `invoice_number`, `subtotal_excluding_vat: "1000.00"`, `vat_amount: "50.00"`, `total_including_vat: "1050.00"`, `confidence` per field, `provider` and `model` |
| 4 | `GET /documents/{document_no}` | same `document_no` | `200` with metadata, the stored invoice fields and, once audited, the audit status |
| 5 | `POST /audit/{document_no}` | same `document_no` | `200` with `audit_status`, `overall_confidence_percent`, the per-rule `findings` (status, expected/actual values, confidence, human-review flags), `warnings`, `review_reasons` and a `trace` |

A compliant standard-rated example like `sample_invoice.pdf` ends as `completed` with
confidence around 94 and four passing findings (the optional customer-TRN check is
`not_applicable` in the default configuration). Change the amounts in the PDF to see the
review path: a wrong VAT amount, rate or total produces `requires_review` with the
deterministic values kept, and a PDF without a text layer produces `409 needs OCR`.

Steps 3-5 need the provider configuration from `.env`; a missing key returns `503` with
the exact variables to set, and a wrong model name returns `502` with the provider's own
message. Nothing is persisted: restarting the API clears every document number.

### Troubleshooting the OCR fallback

A scanned PDF (`extraction_status: "extracted"` with `used_ocr: true`, or `"needs_ocr"`
when OCR is unavailable) is the only part of the pipeline that depends on an optional,
platform-specific stack. The quality report in the upload response says what happened; the
cases seen while validating this on a CPU-only laptop:

| Quality issue you may see | Cause and fix |
| ------------------------- | ------------- |
| `PaddleOCR could not be initialised (ValueError: Unknown argument: use_gpu)` | Fixed: the adapter now derives its keywords from the installed signature. On an older build of this repo, PaddleOCR 3.x was sent the 2.x flag `use_gpu`. |
| `PaddleOCR failed (NotImplementedError: … ConvertPirAttribute2RuntimeAttribute not support … onednn_instruction.cc)` | PaddlePaddle's oneDNN kernels are broken for this CPU build. Keep `VAT_AUDIT_OCR_ENABLE_MKLDNN=false` (the default). |
| `PaddleOCR fallback is not installed` although `pip show paddleocr` works | `VAT_AUDIT_OCR_ENABLED=false`, or PaddleOCR cannot be imported in this interpreter — run the command below with `.\.venv\Scripts\python.exe`. |
| The first scanned upload takes a minute or more | The first run downloads the OCR models (once). Later uploads reuse the cache. |
| `quality=flagged … pages_needing_ocr=[1]` on a scan | OCR read nothing usable: raise `VAT_AUDIT_OCR_DPI` to `200`, or set the models to empty to use PaddleOCR's larger defaults. |

Check the fallback with the same interpreter the API uses, without starting the server:

```powershell
cd backend
.\.venv\Scripts\python.exe -c "from app.services.ocr_fallback import get_ocr_engine; e = get_ocr_engine(); print('engine:', e, '| device:', getattr(e, 'device', None))"
.\.venv\Scripts\python.exe -c "import paddle; print('cuda build:', paddle.device.is_compiled_with_cuda())"
```

`cuda build: False` is fine — the fallback runs on the CPU device by default and needs no
GPU. See the OCR table in [Configuration](#configuration) for the CPU profile and what each
setting buys.


## Endpoints

| Method | Endpoint | Purpose |
| ------ | -------- | ------- |
| GET | `/health` | Check that the API is running. |
| GET | `/documents` | List every uploaded document with its extraction and audit status (one row per document, newest first). |
| POST | `/documents/upload` | Upload a PDF, extract text page by page, and receive a `document_no`. |
| POST | `/documents/{document_no}/extract` | Extract structured invoice fields with the configured LLM and store them. |
| GET | `/documents/{document_no}` | Retrieve document metadata, extraction status and invoice fields. |
| POST | `/audit/{document_no}` | Audit the extracted invoice against the enabled VAT rules and return the report. |
| GET | `/audit/{document_no}` | Read the latest stored report without re-running the audit (404 until one exists). |
| POST | `/analysis` | **Serverless workflow.** Upload one PDF and run text extraction, invoice extraction and the VAT audit inside a single request, returning a self-contained payload (document + invoice + report + table row). |
| GET | `/samples` | List the bundled sample invoice PDFs (title, supplier, size, expected audit outcome) so a UI can offer them for testing. |
| GET | `/samples/{file_name}` | Stream one sample PDF with `Content-Disposition: inline`. Only PDF file names inside the samples folder are served; anything else is a 404. |

### Serverless / multi-instance deployments (Vercel, Lambda, Cloud Run)

The three-step workflow above resolves a document number through
`app/storage/memory.py`, which is **process-local**. On a serverless platform every
request may be served by a different (or freshly started) instance, so the second call
can answer:

```
404 Document 'DOC-…' was not found. Documents are held in memory only, so records
are lost when the service restarts.
```

Use `POST /analysis` there instead. It takes the PDF on every call and returns
everything in one response, so no request depends on another request's memory:

```powershell
curl.exe -X POST "$API/analysis" -F "file=@sample_invoice.pdf"
curl.exe -X POST "$API/analysis?run_audit=false" -F "file=@sample_invoice.pdf"
# re-audit the fields the client already holds, without a second extraction call:
curl.exe -X POST "$API/analysis" -F "file=@sample_invoice.pdf" `
  -F "document_no=DOC-…" -F "invoice={…InvoiceData JSON…}"
```

| Field | Where | Meaning |
| ----- | ----- | ------- |
| `file` | form | The PDF. Required on every call. |
| `invoice` | form | Optional `InvoiceData` JSON from an earlier response; skips the LLM extraction and audits these fields. |
| `document_no` | form | Optional number from an earlier response, so a re-extraction/re-audit updates the same row. |
| `run_audit` | query | `false` stops after invoice extraction and returns `report: null`. |

A document that needs OCR is returned with `200`, `invoice: null` and `report: null`
(nothing failed: the PDF simply has no readable text). Provider problems still fail
loudly with `502`/`503`/`504`, and an invalid `invoice` or `document_no` is `422`.

Two hosting limits are worth knowing: Vercel caps the request body at **4.5 MB**
(lower than `VAT_AUDIT_MAX_UPLOAD_BYTES`), and a function has a maximum duration, so a
large scanned PDF that needs PaddleOCR may time out before the audit finishes. Lower
`VAT_AUDIT_MAX_UPLOAD_BYTES` accordingly and prefer text-based PDFs.

Example response from `GET /health`:

```json
{"status": "ok"}
```

### Uploading an invoice PDF

`POST /documents/upload` accepts one file in a `multipart/form-data` field named
`file`. Validation is by content, never by filename or declared content type:

1. the `%PDF-` signature must appear in the leading bytes, and
2. pdfplumber must actually open and read the document.

The extraction pipeline then runs in four steps:

1. **pdfplumber** extracts the text and the tables of every page,
2. the **extraction quality check** looks for the required invoice fields (number,
   date, TRN, taxable amount, VAT amount, total) and for pages with ruling lines whose
   table could not be rebuilt, and marks every page without a usable text layer,
3. the **PaddleOCR fallback** (when installed) renders those pages in memory, reads
   them and recovers their layout/table structure from the line geometry,
4. the resulting page text, the recovered tables and the quality report are stored and
   returned; the LLM field extraction and the VAT audit consume the same page text.

Example with `curl` (PowerShell):

```powershell
curl.exe -X POST "http://127.0.0.1:8000/documents/upload" -F "file=@sample_invoice.pdf"
```

A ready-to-upload text invoice is included as `sample_invoice.pdf` in this directory.

Example response (`201 Created`):

```json
{
  "document_no": "DOC-08AA1252873D4CE2A3F362D8A46E9B08",
  "original_filename": "invoice.pdf",
  "content_type": "application/pdf",
  "size_bytes": 990,
  "uploaded_at": "2026-01-01T09:15:32.123456Z",
  "page_count": 1,
  "extraction_status": "extracted",
  "extraction_error": null,
  "pages_with_text": 1,
  "total_characters": 143,
  "used_ocr": false,
  "ocr_pages": [],
  "table_count": 1,
  "quality": {
    "passed": true,
    "required_fields_found": [
      "invoice_number",
      "invoice_date",
      "supplier_trn",
      "taxable_amount",
      "vat_amount",
      "total_amount"
    ],
    "missing_required_fields": [],
    "pages_needing_ocr": [],
    "table_pages": [1],
    "ocr_attempted": false,
    "ocr_pages": [],
    "issues": [],
    "summary": "quality=passed fields_found=invoice_number, invoice_date, supplier_trn, taxable_amount, vat_amount, total_amount fields_missing=none pages_needing_ocr=0 table_pages=1 ocr_pages=none issues=0"
  },
  "message": "Text extracted successfully. Continue with POST /documents/{document_no}/extract to extract structured invoice fields, then POST /audit/{document_no} to audit the invoice, or GET /documents/{document_no} to inspect extraction metadata."
}
```

Possible errors: `400` (empty, corrupted, truncated or encrypted PDF), `413`
(over the configured size limit), `415` (content is not a PDF). When the PaddleOCR
fallback reads a page, `message` says so and `used_ocr`/`ocr_pages` name the pages;
when it is not installed, the quality report says so and scanned pages are stored with
`extraction_status: "needs_ocr"`.

`GET /documents/{document_no}?include_text=true` additionally returns each page's
`source` (`pdfplumber` or `paddleocr`) and its recovered `tables` (list of tables,
each a list of rows of cells) — without `include_text=true` only the `table_count` is
returned, so the tables never leak into a default response.

### Extracting structured invoice fields

`POST /documents/{document_no}/extract` sends the page text stored by the upload
step to the configured LLM and stores the validated result. Extraction is triggered
explicitly (not during upload), so uploading a PDF never needs an API key.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/documents/DOC-8D97D3B4DBF147A88C5425C614781824/extract"
```

Example response (`200 OK`):

```json
{
  "document_no": "DOC-8D97D3B4DBF147A88C5425C614781824",
  "reused_existing": false,
  "invoice": {
    "invoice_number": "INV-2024-0001",
    "invoice_date": "2024-05-03",
    "supplier_name": "Al Noor Trading LLC",
    "supplier_trn": "100123456700003",
    "customer_name": "Gulf Retail FZE",
    "customer_trn": "100987654300003",
    "currency": "AED",
    "subtotal_excluding_vat": "1000.00",
    "vat_amount": "50.00",
    "total_including_vat": "1050.00",
    "vat_rate_percent": "5.00",
    "vat_classification": "standard_rated",
    "field_evidence": {
      "supplier_trn": {
        "value": "100123456700003",
        "confidence_percent": 95.0,
        "page_number": 1,
        "evidence": "Supplier TRN: 100123456700003"
      }
    },
    "missing_fields": [],
    "warnings": ["model: The due date was not printed on the invoice."],
    "provider": "deepseek",
    "model": "deepseek-chat",
    "extracted_at": "2026-01-01T09:20:11.482913Z"
  },
  "message": "Structured invoice fields extracted with the configured LLM. ..."
}
```

Behaviour and error handling:

| Status | Meaning |
| ------ | ------- |
| 404 | Unknown document number. |
| 409 | The document has no extractable text (it needs OCR), so extraction is refused. |
| 502 | Provider error, or the response could not be parsed against the invoice schema. |
| 503 | The LLM is not configured (`LLM_API_KEY` / `LLM_MODEL` are missing); no provider call is attempted. |
| 504 | The provider did not answer within `LLM_TIMEOUT_SECONDS`. |

- Monetary amounts are `Decimal` internally and serialised as **strings** so JSON
  cannot lose precision. `missing_fields` lists everything left `null`, and
  warnings explain every value that could not be parsed or needed a judgement call.
- A stored result is returned again on repeat calls; pass `force=true` to call the
  provider again. Failed calls never store a partial invoice.
- `vat_classification` is one of `standard_rated`, `zero_rated`, `exempt`,
  `unknown`. Unknown or ambiguous classifications are recorded as `unknown` with a
  human-confirmation warning — they are never guessed.
- Invoice text is treated as untrusted data: the system prompt forbids the model
  from following instructions found inside the document.
- **No VAT audit rules run at this stage.** Amounts, rates and classification are
  reported as extracted, not validated.

### Retrieving document metadata and invoice fields

`GET /documents/{document_no}` returns the stored metadata plus a per-page
summary (`page_number`, `character_count`, `has_text`, `preview`) and, once
extraction has run, the structured `invoice` block described above (`invoice` is
`null` before that). The full extracted text is **not** returned by default; pass
`include_text=true` to include it. An unknown document number returns `404`.
Lookups are case-insensitive. When an extraction exists, the `notes` list points at
`POST /audit/{document_no}` as the next step.

### Auditing a document

`POST /audit/{document_no}` audits the extracted invoice fields against the enabled
ruleset. The path document number is the only input.

```powershell
curl.exe -X POST "http://127.0.0.1:8000/audit/DOC-8D97D3B4DBF147A88C5425C614781824"
```

Example response (`200 OK`), abbreviated:

```json
{
  "document_no": "DOC-8D97D3B4DBF147A88C5425C614781824",
  "invoice_number": "INV-2024-0001",
  "audit_status": "completed",
  "overall_assessment": "The invoice is consistent with the configured rules. This report is a review aid ...",
  "overall_confidence_percent": 94.1,
  "confidence_explanation": "Overall confidence: severity-weighted mean of 4 finding(s) ...",
  "provider": "deepseek",
  "model": "deepseek-chat",
  "ruleset_version": "uae-vat-rules-1.0.0",
  "audited_at": "2026-01-01T09:22:03.775412Z",
  "human_review_required": false,
  "findings": [
    {
      "rule_id": "TAXABLE_AMOUNT_VAT_MISMATCH",
      "rule_title": "Reported VAT against taxable amount x rate",
      "status": "pass",
      "severity": "high",
      "explanation": "Reported VAT equals 1000.00 x 5% within the AED 0.01 tolerance.",
      "expected_value": "50.00",
      "actual_value": "50.00",
      "page_number": null,
      "evidence": null,
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": null,
      "source": "deterministic"
    }
  ],
  "summary": {
    "total": 5, "passed": 4, "failed": 0, "warnings": 0,
    "not_applicable": 1, "review_required": 0,
    "severity_counts": {"high": 3, "medium": 1, "low": 1},
    "ruleset_version": "uae-vat-rules-1.0.0", "enabled_rule_count": 6
  },
  "warnings": [],
  "review_reasons": [],
  "trace": {
    "nodes": ["load_context", "select_rules", "deterministic_checks", "agent_review", "guardrail_validate", "aggregate_report"],
    "iterations": 0, "model_calls": 1, "tool_calls": [],
    "re_extraction_performed": false,
    "rules_evaluated": ["VAT_RATE_CHECK", "SUPPLIER_TRN_CHECK", "CUSTOMER_TRN_CHECK", "TAXABLE_AMOUNT_VAT_MISMATCH", "INVOICE_TOTAL_CHECK"]
  },
  "message": "Audit completed from the extracted invoice fields and the enabled rules. ..."
}
```

Behaviour and error handling:

| Status | Meaning |
| ------ | ------- |
| 200 | A report was produced. `audit_status` states how far it got (see the status table above). |
| 404 | Unknown document number (records are in memory only). |
| 409 | The document cannot be audited: it needs OCR, or `POST /documents/{document_no}/extract` has not run yet. |
| 500 | Unexpected server error; no stack trace, secret or invoice text is returned. |

- Findings are ordered by severity, then by ruleset order, and every applicable rule
  appears exactly once.
- A provider outage, timeout or malformed model response is reported as
  `requires_review` with a sanitised warning — the endpoint never claims a completed
  audit when the model failed.
- Only the latest report per document number is kept in memory. A repeated call re-runs
  the audit, replaces the stored report and says so in `message`.
- The response never contains the API key, full page text, or unmasked TRNs
  (`100****0003`); tool *names* are traced, never their arguments.

## Tests

Run the suite from the `backend` directory (329 tests as of Phase 9):

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

Every Phase 3 test scripts the LLM (a fake chat model or a stub extractor), so no
API key is required and no network call is made. The Phase 4 ruleset tests are pure
configuration tests, the Phase 5 audit tests script the chat model (including a fake
that requests tools), and the Phase 6 endpoint tests replace the audit and extraction
dependencies with offline services — so no LLM, network or API key is required
anywhere. Phase 9 needs neither PaddleOCR nor a real scan: the OCR fallback is driven
by `tests/ocr_stubs.py`, and the PDF fixtures are authored in memory by
`tests/pdf_factory.py` (`build_text_pdf`, `build_blank_pdf`, `build_table_pdf`,
`build_scanned_pdf`, `build_encrypted_pdf`).

`tests/test_pdf_extractor.py` (Phase 9) covers the extraction pipeline: content
validation, pdfplumber text and ruled-table extraction, the quality check (required
invoice fields, OCR candidates, incomplete tables), the OCR fallback with a stub
engine (text replacement, page rendering, `used_ocr`, the page budget, the disabled
flag, engine failures) and the page-level rules. `tests/test_ocr_fallback.py` covers
the PaddleOCR adapter (2.x and 3.x result shapes, constructor fallbacks, error
mapping, availability) and the row/column recovery.

`tests/test_end_to_end.py` (Phase 7) walks the whole documented workflow over HTTP for
every scenario in the plan: a correct standard-rated invoice, a wrong VAT amount, rate or
total, a missing supplier TRN, a missing customer TRN (with and without the requirement),
zero-rated and exempt invoices, an unknown treatment, missing amounts, a low-confidence
finding, model output that contradicts the arithmetic, a scanned PDF, corrupted and
non-PDF uploads, unknown document numbers, missing keys, provider timeouts/rate limits,
malformed model JSON, invented rule IDs, environment-only configuration changes, the
bounded agent loop and repository hygiene (placeholder-only `.env.example`, no committed
secrets, no database/frontend/auth dependencies, and PaddleOCR as the only — optional —
OCR engine).

### Live smoke test (needs a real key)

`scripts/live_smoke.py` starts uvicorn, runs `GET /health` →
`POST /documents/upload` → `POST /documents/{document_no}/extract` → `POST /audit/{document_no}`
over real HTTP against the provider from `.env`, and prints a secret-free summary:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts/live_smoke.py --port 8123 --report sample_report.json
```

The output of the Phase 7 live run is recorded in `SAMPLE_AUDIT_REPORT.md`.

### Sample invoices

`scripts/generate_sample_invoices.py` authors five A4 tax invoices into `sample_inv/`
(two valid, three with a documented defect). Each one has a supplier letterhead, a billed
party, a ruled nine-column line-item grid, a VAT summary and an amount in words. They are
text PDFs, so they exercise the normal `pdfplumber` path instead of the OCR fallback:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\generate_sample_invoices.py
```

`sample_inv/README.md` lists every invoice, its amounts and the audit result it is meant
to produce, so a manual run has a known expected outcome.

Those PDFs are also served by the API, which is what the UI's "Sample invoices" list
uses — no second copy of the files ships with the frontend:

- `GET /samples` lists the folder (file name, title and supplier from the PDF metadata,
  size, and the expected outcome read from the `_valid_`/`_invalid_` file-name token);
- `GET /samples/{file_name}` streams one PDF with `Content-Disposition: inline`, so a
  browser can open it in a tab. Only `*.pdf` names inside the folder are served, the
  resolved path must stay inside it, and the listing never contains page text;
- serving a sample does **not** register a document: the UI downloads the PDF and sends it
  through the normal upload request, so extraction and audit behave exactly as for a
  picked file (a document that needs OCR still reports `needs_ocr`, a defective sample
  still fails its rules).

Point `VAT_AUDIT_SAMPLES_DIR` at another folder to serve different PDFs, and include
`sample_inv/` in the deployment if the host bundles only `app/` — otherwise `GET /samples`
returns an empty list with an explanatory note instead of failing.

## Web UI (Phase 8)

`frontend/` is a single-screen Next.js console for this API. One page drives the whole
workflow and keeps every result on screen:

1. **Step 1 — Upload** a PDF (`POST /documents/upload`) and show the upload response.
2. **Step 2 — Output of the document**: metadata plus the page-by-page text layer
   (`GET /documents/{document_no}`, optionally with `include_text=true`).
3. **Step 3 — Extracted invoice data**: every structured field with its per-field
   evidence, the fields that could not be established and the extraction warnings
   (`POST /documents/{document_no}/extract`).
4. **Step 4 — Audit result**: status, confidence, counts and every finding with its
   expected/actual values, evidence and human-review flags
   (`POST /audit/{document_no}`).
5. **Documents table**: one row per document with its text-extraction status, its
   invoice-extraction status and the latest audit result, plus `Open`,
   `Extract`/`Re-extract`, `Run audit` and `View report` actions
   (`GET /documents`, `GET /audit/{document_no}`).

`View report` reads the stored report through `GET /audit/{document_no}`: it never
re-runs the audit and never calls the model.

### Running the UI

Start the API first (see [Run the API](#run-the-api)), then:

```powershell
cd frontend
npm install
npm run dev            # http://localhost:3000
```

The UI defaults to `http://127.0.0.1:8000`. Copy `frontend/.env.local.example` to
`frontend/.env.local` to point it at another API address, and set
`VAT_AUDIT_CORS_ALLOW_ORIGINS` on the API when the UI is served from a different origin
(only a browser needs this; the default already allows `http://localhost:3000`).

Other scripts: `npm run build` (production build), `npm start` (serve that build),
`npm run typecheck` (`tsc --noEmit`).

### Notes

- The UI keeps no document state of its own: every panel is read back from the API, so
  the table always shows the backend's view.
- Documents and reports live in memory, so a backend restart empties the table and
  invalidates every document number.
- The API's own messages are shown as-is (needs OCR, missing LLM key, provider
  failure), because those messages state exactly which step is blocked and why.


## Project structure

```
backend/
├── .env.example               # LLM placeholders + provider examples (copy to .env, git-ignored)
├── .venv/                     # local virtual environment, Python 3.13.7 (git-ignored)
├── app/
│   ├── __init__.py
│   ├── config.py              # upload limits + provider-agnostic LLM settings (.env)
│   ├── audit/
│   │   ├── __init__.py        # audit package re-exports
│   │   ├── rules_config.py    # versioned VAT ruleset + agent tool payloads
│   │   ├── state.py           # JSON-safe LangGraph audit state
│   │   ├── tools.py           # deterministic auditor tools (rules, fields, arithmetic)
│   │   ├── prompts.py         # auditor system/user prompts, untrusted-data handling
│   │   ├── nodes.py           # nodes, Decimal checks, guardrails, confidence model
│   │   ├── graph.py           # LangGraph wiring, routing and loop bounds
│   │   └── ai_auditor.py      # audit service + in-memory report store
│   ├── main.py                # FastAPI application, GET /health, router wiring
│   ├── api/
│   │   ├── __init__.py
│   │   ├── documents.py       # upload, extract and metadata routes
│   │   └── audits.py          # POST /audit/{document_no} (Phase 6)
│   ├── models/
│   │   ├── __init__.py
│   │   ├── audit.py           # rule/ruleset schemas + agent tool payloads
│   │   ├── document.py        # document response schemas
│   │   └── invoice.py         # raw/validated invoice schemas
│   ├── services/
│   │   ├── __init__.py
│   │   ├── pdf_extractor.py   # validation, pdfplumber text/tables, quality check, OCR
│   │   ├── ocr_fallback.py    # optional PaddleOCR adapter + layout/table recovery
│   │   ├── llm.py             # provider-agnostic LLM extractor (any OpenAI-compatible API)
│   │   ├── llm_prompts.py     # prompts, untrusted-text delimiters, truncation
│   │   ├── llm_errors.py      # neutral error types + message sanitisation
│   │   └── invoice_extractor.py  # normalisation into validated InvoiceData
│   └── storage/
│       ├── __init__.py
│       └── memory.py          # in-memory document registry
├── sample_inv/                 # generated sample invoices (2 valid, 3 defective)
│   ├── README.md              # what each file proves + expected audit result
│   └── *.pdf                  # the five A4 tax invoices themselves
├── scripts/
│   ├── generate_sample_invoices.py  # authors the five sample invoices (PyMuPDF)
│   └── live_smoke.py          # live end-to-end run against the configured provider
├── tests/
│   ├── __init__.py
│   ├── conftest.py            # test client and registry isolation
│   ├── pdf_factory.py         # builds small PDFs in memory (text, table, scan)
│   ├── ocr_stubs.py           # deterministic OCR engine stub (offline)
│   ├── llm_stubs.py           # scripted chat models and stub extractor (offline)
│   ├── audit_stubs.py         # tool-calling fake model + audit fixtures (offline)
│   ├── test_audit_api.py
│   ├── test_audit_graph.py
│   ├── test_documents.py
│   ├── test_documents_extraction_api.py
│   ├── test_end_to_end.py     # Phase 7 workflow and scenario matrix (offline)
│   ├── test_health.py
│   ├── test_invoice_extraction.py
│   ├── test_llm_config.py
│   ├── test_llm_extractor.py
│   ├── test_ocr_fallback.py   # PaddleOCR adapter + layout recovery (offline)
│   ├── test_pdf_extractor.py  # pdfplumber pipeline + quality check + OCR fallback
│   └── test_rules_config.py
├── requirements.txt
├── requirements-ocr.txt       # optional PaddleOCR fallback stack
├── SAMPLE_AUDIT_REPORT.md     # Phase 7 sample report from a live run
├── README.md
└── PROGRESS.md
```

## Roadmap

| Phase | Description | Status |
| ----- | ----------- | ------ |
| 1 | Project setup and FastAPI (`GET /health`) | Completed |
| 2 | PDF upload and text extraction | Completed |
| 3 | Structured invoice field extraction (any OpenAI-compatible LLM via .env) | Completed (Revision 2) |
| 4 | Configurable VAT rules engine (ruleset + agent tool payloads) | Completed |
| 5 | Agentic audit engine (LangGraph state machine, deterministic guardrails) | Completed |
| 6 | Audit API (`POST /audit/{document_no}`) | Completed |
| 7 | End-to-end testing and cleanup | Completed |
| 8 | Browser UI (Next.js documents table, `GET /documents`, CORS) | Completed |
| 9 | pdfplumber text/table extraction, quality check and PaddleOCR fallback | Completed |

## Final deliverables (Phase 7)

1. **Structure** – see [Project structure](#project-structure): FastAPI app, `app/api`
   routers, provider-agnostic `app/services`, configurable `app/audit` engine,
   in-memory `app/storage`, offline `tests/`, plus `scripts/live_smoke.py`.
2. **Endpoints** – see [Endpoints](#endpoints): `GET /health`,
   `POST /documents/upload`, `POST /documents/{document_no}/extract`,
   `GET /documents/{document_no}`, `POST /audit/{document_no}`.
3. **Environment setup** – copy `.env.example` to `.env` (git-ignored) and set
   `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `LLM_PROVIDER_LABEL`; the VAT rules
   section of the same file tunes tolerance, review threshold, TRN requirements, the
   ruleset version and the agent-loop bound.
4. **Swagger workflow** – open `http://127.0.0.1:8000/docs` and run the four calls in
   order; every endpoint documents its schema, statuses and error codes, and the app
   description states the workflow and the disclaimer.
5. **Test results** – `.\.venv\Scripts\python.exe -m pytest -q` → **329 passed**; the
   same run with `-W error::DeprecationWarning` → **329 passed**. All tests are offline
   and run inside `backend/.venv` (253 tests when this list was written, before Phase 9
   added the extraction-pipeline, table and OCR-fallback tests).
6. **Sample report** – `SAMPLE_AUDIT_REPORT.md` contains two real live runs (a
   `completed` audit and a `requires_review` audit) including the provider label, model
   identifier and ruleset version.
7. **Known limitations** – see [Limitations](#limitations): in-memory storage, a
   best-effort OCR fallback, LLM-based extraction and narrative findings, format-only
   TRN checks, bounded agent loop, review-aid (not certification) semantics.
8. **Setting the real key without sharing it** – never paste a key into chat, logs or a
   commit:

   ```powershell
   cd backend
   Copy-Item .env.example .env
   notepad .env      # set LLM_API_KEY=<your key>, LLM_MODEL and LLM_BASE_URL
   .\.venv\Scripts\python.exe scripts/live_smoke.py   # verifies your key locally, prints no secrets
   ```

   `.env` is git-ignored (`!.env.example` keeps the example tracked), the key is held in
   a `SecretStr`, and neither the reports nor any API response echo it. If a key is ever
   exposed, rotate it with the provider and only update `.env`.

## Limitations

- In-memory storage is temporary: uploaded documents, extracted text and invoice
  fields are lost when the process restarts, and PDF bytes are never written to
  permanent storage. (Starlette may spool a large upload to a temporary file while
  it is being received; nothing persists after the request completes.)
- Only PDFs are accepted, and the text layer comes first: pdfplumber reads it, and the
  pages that have none (scanned, image-only or a scanner stamp on a full-page image)
  are read by the optional PaddleOCR fallback. Documents whose pages cannot be read
  even after the fallback are stored with `extraction_status: "needs_ocr"` and refuse
  field extraction (`409`). OCR text is a best-effort reading of an image and must be
  verified against the source invoice; the recovered table grid is a geometry-based
  reconstruction, not a semantic model of the invoice.
- PDF validation is content-based but not a security scan.
- Field extraction is performed by an LLM and is **not deterministic**: values can
  be wrong or incomplete, so `field_evidence`, `confidence_percent`,
  `missing_fields` and `warnings` must be reviewed and the values must be verified
  against the source invoice. Missing or ambiguous values are `null`, never
  guessed, and ambiguous VAT classification is recorded as `unknown`.
- Invoice text is sent to the configured provider whenever extraction is run, which
  means the document text leaves the machine and provider token costs apply.
  Truncation beyond `LLM_MAX_INPUT_CHARACTERS` is reported as a warning.
- Extraction quality depends entirely on the configured provider and model. The
  layer prefers native structured output (`LLM_STRUCTURED_METHOD`) and otherwise
  falls back to prompt-based JSON parsing, which is less reliable; pin the provider
  and model per deployment and record them (`provider` / `model` on every result).
- Provider failures are never silently converted into a successful extraction: the
  endpoint returns `502`/`503`/`504` and stores nothing.
- The audit report is **agentic, not authoritative**: monetary arithmetic is recomputed
  in Python with `Decimal` and always wins a conflict with the model, but explanations,
  severities, page references and recommended actions come from an LLM and can be
  wrong. The agent loop is bounded (`VAT_AUDIT_MAX_AGENT_ITERATIONS`, max 3 tool rounds,
  50 graph steps), so a session that never converges ends in `requires_review` rather
  than looping.
- The report never asserts fraud or definitive legal non-compliance, TRN checks are
  format-only (15 digits, not an FTA registry lookup), and zero-rated/exempt treatment
  is always routed to a human because the extracted fields cannot prove the condition
  for that treatment.
- Audit reports are stored in memory only, one per document number (the latest), and
  are lost on restart.
- Audit findings are review aids: a failed rule is an audit finding, not proof of tax
  non-compliance, and passing all implemented checks must not be presented as full VAT
  compliance certification.

See `PROGRESS.md` for the current phase status and the results of the tests
that were actually executed.
