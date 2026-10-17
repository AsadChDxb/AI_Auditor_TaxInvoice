"use client";

/**
 * Step 3: the structured invoice data produced by the LLM extractor.
 *
 * Renders every field of `InvoiceData` with the evidence the model reported for it
 * (confidence, page, quoted excerpt), the fields it could not establish, and the
 * extraction warnings. Nothing here is audited yet: the values are model output that
 * the backend stores for the audit and that a reviewer must verify against the PDF.
 */

import { CLASSIFICATION_LABELS, formatDateTime, humaniseFieldName } from "@/lib/format";
import type { InvoiceData } from "@/lib/types";

const INVOICE_FIELDS: (keyof InvoiceData)[] = [
  "invoice_number",
  "invoice_date",
  "supplier_name",
  "supplier_trn",
  "customer_name",
  "customer_trn",
  "currency",
  "subtotal_excluding_vat",
  "vat_amount",
  "total_including_vat",
  "vat_rate_percent",
  "vat_classification",
];

const MONEY_FIELDS = new Set([
  "subtotal_excluding_vat",
  "vat_amount",
  "total_including_vat",
]);

export function InvoicePanel({
  documentNo,
  invoice,
  busy,
  error,
  canExtract,
  onExtract,
}: {
  documentNo: string | null;
  invoice: InvoiceData | null;
  busy: boolean;
  error: string | null;
  canExtract: boolean;
  onExtract: (force: boolean) => void;
}) {
  const reExtract = Boolean(invoice);

  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2 className="card-title">Step 3 · Extracted invoice data</h2>
          <p className="card-sub">
            Structured fields (LLM) with per-field evidence, missing fields and warnings.
          </p>
        </div>
        <div className="card-actions">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!documentNo || busy || !canExtract}
            onClick={() => onExtract(reExtract)}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            {busy
              ? "Extracting…"
              : reExtract
                ? "Re-extract fields"
                : "Extract invoice fields"}
          </button>
        </div>
      </header>

      <div className="card-body">
        {!documentNo ? (
          <div className="empty">
            Select a document in the table below to extract its invoice fields.
          </div>
        ) : null}

        {documentNo && !canExtract && !busy ? (
          <div className="alert alert-warn">
            This document has no extractable text layer and is flagged as needing OCR.
            pdfplumber found no text and the PaddleOCR fallback (an optional dependency)
            recovered none, so invoice extraction and audit are not available for it.
          </div>
        ) : null}

        {error ? <div className="alert alert-error">{error}</div> : null}

        {documentNo && canExtract && !invoice && !busy ? (
          <div className="alert alert-info">
            Structured fields have not been extracted yet. Run the extraction, then audit
            the result against the enabled VAT rules.
          </div>
        ) : null}

        {invoice ? (
          <>
            <div className="kv-list">
              {INVOICE_FIELDS.map((field) => {
                const raw = invoice[field];
                const value =
                  field === "vat_classification"
                    ? CLASSIFICATION_LABELS[invoice.vat_classification]
                    : typeof raw === "string" && raw.length > 0
                      ? raw
                      : null;
                const missing = invoice.missing_fields.includes(field);
                const evidence = invoice.field_evidence[field];
                const money = MONEY_FIELDS.has(field) && value;

                return (
                  <div className="kv" data-missing={missing} key={field}>
                    <div className="kv-label">{humaniseFieldName(field)}</div>
                    <div className="kv-value" data-money={Boolean(money)}>
                      {value
                        ? `${value}${
                            money && invoice.currency ? ` ${invoice.currency}` : ""
                          }`
                        : "not found"}
                    </div>
                    {evidence ? (
                      <div className="kv-evidence">
                        {evidence.confidence_percent !== null
                          ? `${evidence.confidence_percent}% confidence`
                          : "confidence not reported"}
                        {evidence.page_number !== null
                          ? ` · page ${evidence.page_number}`
                          : ""}
                        {evidence.evidence ? ` · “${evidence.evidence}”` : ""}
                      </div>
                    ) : null}
                  </div>
                );
              })}
            </div>

            {invoice.missing_fields.length > 0 ? (
              <div className="alert alert-warn">
                Not found on the invoice (returned as null, never guessed):{" "}
                {invoice.missing_fields.map(humaniseFieldName).join(", ")}.
              </div>
            ) : null}

            {invoice.warnings.length > 0 ? (
              <div className="alert alert-warn">
                <div>
                  <strong>Extraction warnings</strong>
                  <ul className="notes" style={{ color: "inherit" }}>
                    {invoice.warnings.map((warning) => (
                      <li key={warning}>{warning}</li>
                    ))}
                  </ul>
                </div>
              </div>
            ) : null}

            <div className="meta-grid">
              <div className="meta-item">
                <div className="meta-label">Provider</div>
                <div className="meta-value">{invoice.provider}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Model</div>
                <div className="meta-value">{invoice.model ?? "—"}</div>
              </div>
              <div className="meta-item">
                <div className="meta-label">Extracted at</div>
                <div className="meta-value">{formatDateTime(invoice.extracted_at)}</div>
              </div>
            </div>
          </>
        ) : null}
      </div>
    </section>
  );
}

