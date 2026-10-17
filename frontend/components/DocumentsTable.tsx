"use client";

/**
 * The main table: one row per document, newest upload first.
 *
 * Each row keeps the whole workflow result — text extraction, invoice extraction and
 * the latest audit status with its counts and confidence — and carries the actions the
 * screen is built around: open the row's output, run (or re-run) the audit and view the
 * stored report.
 */

import {
  AuditStatusBadge,
  ConfidenceMeter,
  ExtractionStatusBadge,
} from "@/components/Badges";
import { formatDateTime, orDash } from "@/lib/format";
import type { DocumentListItem } from "@/lib/types";

export function DocumentsTable({
  documents,
  loading,
  selected,
  busyRow,
  busyAction,
  notes,
  onOpen,
  onExtract,
  onAudit,
  onViewReport,
  onRefresh,
}: {
  documents: DocumentListItem[];
  loading: boolean;
  selected: string | null;
  busyRow: string | null;
  busyAction: "extract" | "audit" | "report" | null;
  notes: string[];
  onOpen: (document: DocumentListItem) => void;
  onExtract: (document: DocumentListItem) => void;
  onAudit: (document: DocumentListItem) => void;
  onViewReport: (document: DocumentListItem) => void;
  onRefresh: () => void;
}) {
  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2 className="card-title">Documents</h2>
          <p className="card-sub">
            {documents.length} document{documents.length === 1 ? "" : "s"} in memory ·
            status, result and actions per row
          </p>
        </div>
        <div className="card-actions">
          {busyRow && busyAction ? (
            <span className="card-sub">
              <span className="spinner" aria-hidden="true" />{" "}
              {busyAction === "extract"
                ? "Extracting invoice fields…"
                : busyAction === "audit"
                  ? "Running audit…"
                  : "Loading report…"}{" "}
              <span className="mono">{busyRow}</span>
            </span>
          ) : null}
          <button
            type="button"
            className="btn btn-sm"
            onClick={onRefresh}
            disabled={loading}
          >
            {loading ? <span className="spinner" aria-hidden="true" /> : null}
            Refresh
          </button>
        </div>
      </header>

      <div className="card-body tight">
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>File / document number</th>
                <th>Pages</th>
                <th>Uploaded</th>
                <th>Text extraction</th>
                <th>Invoice data</th>
                <th>Audit result</th>
                <th className="right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {documents.map((document) => {
                const busy = busyRow === document.document_no;
                return (
                  <tr
                    key={document.document_no}
                    data-selected={selected === document.document_no}
                  >
                    <td>
                      <div className="cell-strong">{document.original_filename}</div>
                      <div className="cell-mono muted">{document.document_no}</div>
                    </td>
                    <td>
                      <div>{document.page_count}</div>
                      <div className="cell-muted">
                        {document.pages_with_text} with text
                      </div>
                    </td>
                    <td className="cell-muted">
                      {formatDateTime(document.uploaded_at)}
                    </td>
                    <td>
                      <div className="stack-tight">
                        <ExtractionStatusBadge status={document.extraction_status} />
                        <span className="cell-muted">
                          {document.total_characters} chars
                        </span>
                      </div>
                    </td>
                    <td>
                      {document.invoice_extracted ? (
                        <div className="stack-tight">
                          <span className="badge badge-good">Extracted</span>
                          <span className="cell-mono">
                            {orDash(document.invoice_number)}
                            {document.invoice_total_including_vat
                              ? ` · ${document.invoice_total_including_vat}`
                              : ""}
                          </span>
                        </div>
                      ) : (
                        <div className="stack-tight">
                          <span className="badge badge-muted">Not extracted</span>
                          <span className="cell-muted">
                            {document.can_extract
                              ? "Run extraction"
                              : "Needs OCR — cannot extract"}
                          </span>
                        </div>
                      )}
                    </td>
                    <td>
                      {document.audited && document.audit_status ? (
                        <div className="stack-tight">
                          <AuditStatusBadge status={document.audit_status} />
                          <span className="cell-muted">
                            {document.audit_summary
                              ? `${document.audit_summary.passed} passed · ` +
                                `${document.audit_summary.failed} failed · ` +
                                `${document.audit_summary.review_required} review`
                              : ""}
                          </span>
                          {document.audit_overall_confidence_percent !== null ? (
                            <ConfidenceMeter
                              value={document.audit_overall_confidence_percent}
                              width={70}
                            />
                          ) : null}
                        </div>
                      ) : (
                        <div className="stack-tight">
                          <span className="badge badge-muted">Not audited</span>
                          <span className="cell-muted">
                            {document.can_audit
                              ? "Ready to audit"
                              : "Extraction needed first"}
                          </span>
                        </div>
                      )}
                    </td>
                    <td>
                      <div className="cell-actions">
                        <button
                          type="button"
                          className="btn btn-sm"
                          onClick={() => onOpen(document)}
                        >
                          Open
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={!document.can_extract || busy}
                          onClick={() => onExtract(document)}
                        >
                          {busy && busyAction === "extract" ? (
                            <span className="spinner" aria-hidden="true" />
                          ) : null}
                          {document.invoice_extracted ? "Re-extract" : "Extract"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm btn-primary"
                          disabled={!document.can_audit || busy}
                          onClick={() => onAudit(document)}
                        >
                          {busy && busyAction === "audit" ? (
                            <span className="spinner" aria-hidden="true" />
                          ) : null}
                          {document.audited ? "Re-run audit" : "Run audit"}
                        </button>
                        <button
                          type="button"
                          className="btn btn-sm"
                          disabled={!document.audited || busy}
                          onClick={() => onViewReport(document)}
                        >
                          {busy && busyAction === "report" ? (
                            <span className="spinner" aria-hidden="true" />
                          ) : null}
                          View report
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}

              {documents.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <div className="empty">
                      {loading
                        ? "Loading documents…"
                        : "No documents yet. Upload an invoice PDF in step 1 to create the first row."}
                    </div>
                  </td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>

        {notes.length > 0 ? (
          <div style={{ padding: "12px 16px", borderTop: "1px solid var(--border)" }}>
            <ul className="notes">
              {notes.map((note) => (
                <li key={note}>{note}</li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </section>
  );
}

