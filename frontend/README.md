# UAE VAT Audit Console (Next.js UI)

A single-screen web console for the FastAPI service in [`../backend`](../backend). It
consumes the API as documented in `../backend/README.md` — no mock data, no duplicated
business logic: every status, amount and finding on screen comes from the API.

```
Upload PDF ─▶ Output of the document ─▶ Extracted invoice data ─▶ Audit result
                              │
                              └─▶ Documents table: one row per document,
                                  status + Run audit + View report
```

The screen calls `POST /analysis` (not the three-step
`upload → extract → audit` workflow) so it also works on a serverless host such as
Vercel. That endpoint takes the PDF again on every call and returns the document, its
invoice fields, the report and the table row in one response, so the UI never depends
on the API still holding a document number from an earlier request. The file and the
results stay in the browser tab; reloading the page clears the table.

## Requirements

- Node.js 18.18 or newer (developed on Node 22 with Next.js 15 and React 19)
- The backend running and reachable, for example:

```powershell
cd ..\backend
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

## Quick start

```powershell
npm install
npm run dev            # http://localhost:3000
```

The header shows whether the API is reachable; when it is not, the banner repeats the
exact command to start it.

## Configuration

| Variable | Default | Purpose |
| -------- | ------- | ------- |
| `NEXT_PUBLIC_API_BASE_URL` | `http://127.0.0.1:8000` | Base URL of the API. |

Copy `.env.local.example` to `.env.local` to override it. The value is baked in at build
time, so re-run `npm run dev` / `npm run build` after changing it.

If the UI is served from an origin other than `http://localhost:3000`, allow that origin
on the API with `VAT_AUDIT_CORS_ALLOW_ORIGINS` (see the backend README).

## What the screen does

| UI block | Endpoint | Notes |
| -------- | -------- | ----- |
| Step 1 · Upload document | `POST /documents/upload` | Drag & drop or browse; the API validates the PDF by content. The upload response (document number, pages, characters, extraction status) is shown verbatim. |
| Step 2 · Output of the document | `GET /documents/{document_no}` | Metadata plus the page-by-page text layer. "Include full page text" re-fetches with `include_text=true`. |
| Step 3 · Extracted invoice data | `POST /documents/{document_no}/extract` | Sends the stored text to the configured LLM and shows every field with its per-field evidence, missing fields and warnings. "Re-extract" passes `force=true`. |
| Step 4 · Audit result | `POST /audit/{document_no}` | Status, confidence, counts and every finding (status, severity, expected/actual, evidence, recommended action, human-review flag) plus the trace. |
| Documents table | `GET /documents` | One row per document: text extraction, invoice extraction and the latest audit result with its counts and confidence. |
| Run audit / Extract buttons | `POST /audit/{document_no}`, `POST /documents/{document_no}/extract` | Per row; disabled when the step is not allowed (no text layer, or no extracted fields yet). |
| View report | `GET /audit/{document_no}` | Opens the stored report in a full-screen reader. It never re-runs the audit and never calls the model. |
| Sample invoices (in Step 1) | `GET /samples`, `GET /samples/{file_name}` | Lists the sample PDFs the API ships with, each with its expected outcome. **View PDF** opens the file in a new tab; **Use sample** downloads it and uploads it through the same request a picked file uses. |

The step rail and the table are refreshed after every action, so a row's status always
matches the API. `Open` loads a row into the panels; `Run audit` also scrolls to the
audit panel.

## Scripts

| Script | Purpose |
| ------ | ------- |
| `npm run dev` | Development server on port 3000. |
| `npm run build` | Production build. |
| `npm start` | Serve the production build on port 3000. |
| `npm run typecheck` | `tsc --noEmit`. |

## Project structure

```
frontend/
├── app/
│   ├── globals.css       # design tokens, layout, table, badges, modal
│   ├── layout.tsx        # HTML shell and metadata
│   └── page.tsx          # the single screen: state, API calls, layout
├── components/
│   ├── AuditReportView.tsx    # status, counts, findings table, trace
│   ├── Badges.tsx             # status badges + confidence meter
│   ├── DocumentOutputPanel.tsx# step 2: metadata + page text output
│   ├── DocumentsTable.tsx     # the main table with per-row actions
│   ├── InvoicePanel.tsx       # step 3: structured invoice fields
│   ├── ReportModal.tsx        # "View report" reader
│   ├── StepRail.tsx           # the four-step progress rail
│   └── UploadPanel.tsx        # step 1: drag & drop upload + sample invoices
└── lib/
    ├── api.ts            # typed fetch client (base URL, ApiError)
    ├── format.ts         # labels, dates, money, sorting helpers
    └── types.ts          # TypeScript mirror of the API schemas
```

## Notes and limitations

- The backend stores documents and reports in process memory, so restarting it empties
  the table the API reports. The UI keeps its own rows for the current tab and re-sends
  the PDF for every action, so a serverless deployment (Vercel) works without shared
  memory; a page reload clears the rows.
- `NEXT_PUBLIC_API_BASE_URL` is public by design: the UI talks to the API directly from
  the browser, and the API needs no credential of its own (the LLM key stays in the
  backend's `.env`).
- Money values are rendered as the strings the API returns, so no decimal precision is
  lost in the browser.
- The sample invoices are served by the API (`GET /samples`) instead of being bundled with
  this app. If the API is deployed without `backend/sample_inv/` (or
  `VAT_AUDIT_SAMPLES_DIR` points nowhere), Step 1 shows the API's note instead of a list,
  and normal uploads keep working.
- The screen is deliberately a review aid: findings are shown with their evidence and
  human-review flags, and the copy never claims compliance or non-compliance.

