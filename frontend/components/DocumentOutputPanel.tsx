"use client";

/**
 * Step 2: the output of the uploaded document.
 *
 * Shows what `POST /documents/upload` + `GET /documents/{document_no}` produced: the
 * document metadata, the per-page text-extraction result, and — when the checkbox is
 * on — the full text layer of every page (`include_text=true`). This is the input the
 * invoice extraction uses, so it is shown before any model is involved.
 */

import { ExtractionStatusBadge } from "@/components/Badges";
import { formatBytes, formatDateTime, orDash } from "@/lib/format";
import type { DocumentDetail } from "@/lib/types";

export function DocumentOutputPanel({
  detail,
  loading,
  includeText,
  onToggleText,
}: {
  detail: DocumentDetail | null;
  loading: boolean;
  includeText: boolean;
  onToggleText: (value: boolean) => void;
}) {
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2 className="card-title">Step 2 · Output of the document</h2>
          <p className="card-sub">
            Metadata and the text layer extracted from the PDF, page by page.
          </p>
        </div>
        <label className="card-sub row-wrap" style={{ gap: 6, cursor: "pointer" }}>
          <input
            type="checkbox"
            checked={includeText}
            disabled={!detail}
            onChange={(event) => onToggleText(event.target.checked)}
          />
          Include full page text
        </label>
      </header>

      <div className="card-body">
        {loading ? (
          <div className="empty">
            <span className="spinner" aria-hidden="true" /> Loading document…
          </div>
        ) : null}

        {!loading && !detail ? (
          <div className="empty">
            Upload a PDF above, or pick a document in the table below to see its output.
          </div>
        ) : null}

        {!loading && detail ? (
          <>
            <div className="meta-grid">
              <div className="meta-item">
                <div className="meta-label">Document number</div>
                <div className="meta-value mono">{detail.document_no}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">File</div>
                <div className="meta-value">{orDash(detail.original_filename)}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Content type</div>
                <div className="meta-value">{orDash(detail.content_type)}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Size</div>
                <div className="meta-value">{formatBytes(detail.size_bytes)}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Uploaded</div>
                <div className="meta-value">{formatDateTime(detail.uploaded_at)}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Pages with text</div>
                <div className="meta-value">
                  {detail.pages_with_text} / {detail.page_count}
                </div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Characters</div>
                <div className="meta-value">{detail.total_characters}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Text extraction</div>
                <div className="meta-value">
                  <ExtractionStatusBadge status={detail.extraction_status} />
                </div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Tables recovered</div>
                <div className="meta-value">{detail.table_count ?? 0}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">OCR fallback</div>
                <div className="meta-value">
                  {detail.used_ocr
                    ? `used on page(s) ${(detail.ocr_pages ?? []).join(", ")}`
                    : "not used"}
                </div>
              </div>
            </div>

            {detail.extraction_error ? (
              <div className="alert alert-warn">{detail.extraction_error}</div>
            ) : null}

            {detail.quality && detail.quality.issues.length > 0 ? (
              <div className="alert alert-warn">
                <strong>Extraction quality check</strong>
                <ul className="notes">
                  {detail.quality.issues.map((issue) => (
                    <li key={issue}>{issue}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <div className="stack-tight">
              {detail.pages.map((page) => (
                <div key={page.page_number}>
                  <div className="row-wrap" style={{ justifyContent: "space-between" }}>
                    <span className="cell-strong small">Page {page.page_number}</span>
                    <span className="row-wrap">
                      <span className="cell-muted">{page.character_count} chars</span>
                      {page.table_count ? (
                        <span className="cell-muted">
                          {page.table_count} table{page.table_count === 1 ? "" : "s"}
                        </span>
                      ) : null}
                      <span className="cell-muted">{page.source ?? "pdfplumber"}</span>
                      <span className={"badge badge-" + (page.has_text ? "good" : "warn")}>
                        {page.has_text ? "Text found" : "No text"}
                      </span>
                    </span>
                  </div>
                  {includeText && page.text ? (
                    <pre className="page-text">{page.text}</pre>
                  ) : (
                    <p className="page-preview">
                      {page.preview || "(no extractable text on this page)"}
                    </p>
                  )}
                </div>
              ))}
              {detail.pages.length === 0 ? (
                <p className="small muted">No pages were stored for this document.</p>
              ) : null}
            </div>

            {detail.notes.length > 0 ? (
              <ul className="notes">
                {detail.notes.map((note) => (
                  <li key={note}>{note}</li>
                ))}
              </ul>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}

