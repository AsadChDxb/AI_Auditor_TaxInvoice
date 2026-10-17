"use client";

/**
 * The single screen of the console.
 *
 * It drives the documented workflow against the FastAPI backend and keeps every result
 * on one page:
 *
 * 1. upload a PDF (`POST /documents/upload`) and show the upload response,
 * 2. show the output of the uploaded document (page-level text extraction,
 *    `GET /documents/{document_no}`),
 * 3. extract and show the structured invoice data
 *    (`POST /documents/{document_no}/extract`),
 * 4. audit the invoice and show the report (`POST /audit/{document_no}`), and
 * 5. keep every document in one table row with its status, result and the
 *    Run audit / View report actions (`GET /documents`, `GET /audit/{document_no}`).
 *
 * The selected row is the source of truth for the panels below the table, and the
 * table is refreshed after every action so the row status always matches the API.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { AuditReportView } from "@/components/AuditReportView";
import { AuditStatusBadge } from "@/components/Badges";
import { DocumentOutputPanel } from "@/components/DocumentOutputPanel";
import { DocumentsTable } from "@/components/DocumentsTable";
import { InvoicePanel } from "@/components/InvoicePanel";
import { ReportModal } from "@/components/ReportModal";
import { StepRail, type Step } from "@/components/StepRail";
import { UploadPanel } from "@/components/UploadPanel";
import {
  API_BASE_URL,
  ApiError,
  auditDocument,
  checkHealth,
  extractInvoice,
  getDocument,
  listDocuments,
  storedAuditReport,
  uploadDocument,
} from "@/lib/api";
import { AUDIT_STATUS_LABELS, formatPercent } from "@/lib/format";
import type {
  AuditReport,
  DocumentDetail,
  DocumentListItem,
  DocumentSummary,
  InvoiceData,
} from "@/lib/types";

type BusyAction = "extract" | "audit" | "report" | null;
type Health = "checking" | "ok" | "down";

/** Turn anything thrown by the API layer into a sentence for the user. */
function messageOf(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) {
    return error.message;
  }
  return "Unexpected error.";
}

export default function AuditConsolePage() {
  const [health, setHealth] = useState<Health>("checking");

  const [documents, setDocuments] = useState<DocumentListItem[]>([]);
  const [notes, setNotes] = useState<string[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<DocumentSummary | null>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<DocumentDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [includeText, setIncludeText] = useState(false);

  const [extractBusy, setExtractBusy] = useState(false);
  const [extractError, setExtractError] = useState<string | null>(null);

  const [auditBusy, setAuditBusy] = useState(false);
  const [auditError, setAuditError] = useState<string | null>(null);
  const [report, setReport] = useState<AuditReport | null>(null);

  const [busyRow, setBusyRow] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [modalReport, setModalReport] = useState<AuditReport | null>(null);

  const reportPanelRef = useRef<HTMLElement | null>(null);

  const refreshList = useCallback(async () => {
    setListLoading(true);
    try {
      const response = await listDocuments();
      setDocuments(response.documents);
      setNotes(response.notes);
      setListError(null);
      setHealth("ok");
    } catch (error) {
      setListError(messageOf(error));
      setHealth("down");
    } finally {
      setListLoading(false);
    }
  }, []);

  /** Load one document into the panels, including its stored report when it has one. */
  const loadDocument = useCallback(
    async (documentNo: string, withText: boolean) => {
      setSelected(documentNo);
      setDetailLoading(true);
      setExtractError(null);
      setAuditError(null);
      try {
        const loaded = await getDocument(documentNo, withText);
        setDetail(loaded);

        let stored: AuditReport | null = null;
        if (loaded.invoice) {
          try {
            stored = await storedAuditReport(documentNo);
          } catch (error) {
            // 404 simply means "not audited yet"; anything else is worth showing.
            if (!(error instanceof ApiError) || error.status !== 404) {
              setAuditError(messageOf(error));
            }
          }
        }
        setReport(stored);
      } catch (error) {
        setDetail(null);
        setReport(null);
        setListError(messageOf(error));
      } finally {
        setDetailLoading(false);
      }
    },
    [],
  );

  useEffect(() => {
    void refreshList();
    checkHealth()
      .then(() => setHealth("ok"))
      .catch(() => setHealth("down"));
  }, [refreshList]);

  // --- actions --------------------------------------------------------------------

  async function handleUpload(file: File) {
    setUploadBusy(true);
    setUploadError(null);
    try {
      const summary = await uploadDocument(file);
      setUploadResult(summary);
      await Promise.all([refreshList(), loadDocument(summary.document_no, includeText)]);
    } catch (error) {
      setUploadError(messageOf(error));
    } finally {
      setUploadBusy(false);
    }
  }

  async function handleExtract(documentNo: string, force: boolean) {
    setSelected(documentNo);
    setBusyRow(documentNo);
    setBusyAction("extract");
    setExtractBusy(true);
    setExtractError(null);
    try {
      await extractInvoice(documentNo, force);
      await Promise.all([refreshList(), loadDocument(documentNo, includeText)]);
    } catch (error) {
      setExtractError(messageOf(error));
    } finally {
      setExtractBusy(false);
      setBusyRow(null);
      setBusyAction(null);
    }
  }

  async function handleAudit(documentNo: string) {
    const isNewSelection = selected !== documentNo;
    setSelected(documentNo);
    setBusyRow(documentNo);
    setBusyAction("audit");
    setAuditBusy(true);
    setAuditError(null);
    try {
      const fresh = await auditDocument(documentNo);
      await Promise.all([refreshList(), loadDocument(documentNo, includeText)]);
      setReport(fresh);
      if (isNewSelection) {
        reportPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    } catch (error) {
      setAuditError(messageOf(error));
      setReport(null);
    } finally {
      setAuditBusy(false);
      setBusyRow(null);
      setBusyAction(null);
    }
  }

  async function handleViewReport(documentNo: string) {
    setBusyRow(documentNo);
    setBusyAction("report");
    setAuditError(null);
    try {
      setModalReport(await storedAuditReport(documentNo));
    } catch (error) {
      setAuditError(messageOf(error));
    } finally {
      setBusyRow(null);
      setBusyAction(null);
    }
  }

  function handleOpen(document: DocumentListItem) {
    void loadDocument(document.document_no, includeText);
    reportPanelRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function handleToggleText(next: boolean) {
    setIncludeText(next);
    if (selected) {
      void loadDocument(selected, next);
    }
  }

  // --- derived state --------------------------------------------------------------

  const selectedRow = useMemo(
    () => documents.find((row) => row.document_no === selected) ?? null,
    [documents, selected],
  );

  const canExtract = selectedRow
    ? selectedRow.can_extract
    : detail?.extraction_status === "extracted";
  const canAudit = selectedRow ? selectedRow.can_audit : Boolean(detail?.invoice);

  const steps: Step[] = useMemo(() => {
    const uploaded = Boolean(selected);
    const invoiceExtracted = selectedRow
      ? selectedRow.invoice_extracted
      : Boolean(detail?.invoice);
    const audited = selectedRow ? selectedRow.audited : Boolean(report);
    const auditStatus = selectedRow?.audit_status ?? report?.audit_status ?? null;
    const pages = detail?.page_count ?? selectedRow?.page_count ?? 0;
    const characters = detail?.total_characters ?? selectedRow?.total_characters ?? 0;

    return [
      {
        title: "1 · Upload PDF",
        note: uploaded ? (selected ?? "") : "Waiting for a document",
        state: uploaded ? "done" : "active",
      },
      {
        title: "2 · Document output",
        note: uploaded
          ? `${pages} page(s) · ${characters} characters extracted`
          : "Text layer of the uploaded PDF",
        state: uploaded ? "done" : "idle",
      },
      {
        title: "3 · Extracted invoice data",
        note: invoiceExtracted
          ? "Structured fields stored"
          : !canExtract && uploaded
            ? "Needs OCR — cannot extract"
            : uploaded
              ? "Not extracted yet"
              : "Runs after the upload",
        state: invoiceExtracted
          ? "done"
          : !canExtract && uploaded
            ? "blocked"
            : uploaded
              ? "active"
              : "idle",
      },
      {
        title: "4 · Audit",
        note: audited && auditStatus
          ? AUDIT_STATUS_LABELS[auditStatus]
          : invoiceExtracted
            ? "Ready to run"
            : "Extraction comes first",
        state: audited ? "done" : invoiceExtracted ? "active" : "idle",
      },
    ];
  }, [selected, selectedRow, detail, report, canExtract]);

  return (
    <div className="app">
      <header className="app-header">
        <div className="app-header-inner">
          <div>
            <div className="brand-title">UAE VAT Audit Console</div>
            <div className="brand-sub">
              Upload → document output → invoice data → audit. One row per document;
              findings are a review aid, not a VAT compliance certificate.
            </div>
          </div>
          <div className="header-actions">
            <span className="health-chip" data-state={health}>
              <span className="health-dot" aria-hidden="true" />
              {health === "ok"
                ? "API online"
                : health === "down"
                  ? "API unreachable"
                  : "Checking API…"}
            </span>
            <span className="api-url">{API_BASE_URL}</span>
            <button
              type="button"
              className="btn btn-invert btn-sm"
              onClick={() => void refreshList()}
              disabled={listLoading}
            >
              {listLoading ? <span className="spinner" aria-hidden="true" /> : null}
              Reload documents
            </button>
            <a
              className="btn btn-invert btn-sm"
              href={`${API_BASE_URL}/docs`}
              target="_blank"
              rel="noreferrer"
            >
              Swagger UI
            </a>
          </div>
        </div>
      </header>

      <main className="page">
        {listError ? (
          <div className="alert alert-error">
            <div>
              <strong>Could not load data.</strong> {listError}
            </div>
          </div>
        ) : null}

        <StepRail steps={steps} />

        <div className="grid-2">
          <UploadPanel
            busy={uploadBusy}
            error={uploadError}
            result={uploadResult}
            onUpload={handleUpload}
          />
          <DocumentOutputPanel
            detail={detail}
            loading={detailLoading}
            includeText={includeText}
            onToggleText={handleToggleText}
          />
        </div>

        <DocumentsTable
          documents={documents}
          loading={listLoading}
          selected={selected}
          busyRow={busyRow}
          busyAction={busyAction}
          notes={notes}
          onOpen={handleOpen}
          onExtract={(document) => void handleExtract(document.document_no, document.invoice_extracted)}
          onAudit={(document) => void handleAudit(document.document_no)}
          onViewReport={(document) => void handleViewReport(document.document_no)}
          onRefresh={() => void refreshList()}
        />

        <div className="grid-2">
          <InvoicePanel
            documentNo={selected}
            invoice={detail?.invoice ?? null}
            busy={extractBusy || busyAction === "extract"}
            error={extractError}
            canExtract={Boolean(canExtract)}
            onExtract={(force) => {
              if (selected) {
                void handleExtract(selected, force);
              }
            }}
          />

          <section className="card" ref={reportPanelRef} id="audit-result">
            <header className="card-head">
              <div>
                <h2 className="card-title">Step 4 · Audit result</h2>
                <p className="card-sub">
                  Deterministic VAT checks plus the model review, with per-finding
                  evidence and human-review flags.
                </p>
              </div>
              <div className="card-actions">
                {report ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={() => setModalReport(report)}
                  >
                    Open as page
                  </button>
                ) : null}
                <button
                  type="button"
                  className="btn btn-primary btn-sm"
                  disabled={!selected || auditBusy || !canAudit}
                  onClick={() => {
                    if (selected) {
                      void handleAudit(selected);
                    }
                  }}
                >
                  {auditBusy ? <span className="spinner" aria-hidden="true" /> : null}
                  {auditBusy
                    ? "Auditing…"
                    : report
                      ? "Re-run audit"
                      : "Run audit"}
                </button>
              </div>
            </header>

            <div className="card-body">
              {selected ? (
                <div className="row-wrap" style={{ justifyContent: "space-between" }}>
                  <span className="row-wrap">
                    <span className="cell-muted">Selected document</span>
                    <span className="mono small">{selected}</span>
                  </span>
                  <span className="row-wrap">
                    {report ? (
                      <>
                        <AuditStatusBadge status={report.audit_status} />
                        <span className="cell-muted">
                          confidence {formatPercent(report.overall_confidence_percent)}
                        </span>
                      </>
                    ) : (
                      <span className="badge badge-muted">No stored report yet</span>
                    )}
                  </span>
                </div>
              ) : null}

              {auditError ? <div className="alert alert-error">{auditError}</div> : null}

              {!selected ? (
                <div className="empty">
                  Upload a PDF or open a document row to audit it.
                </div>
              ) : null}

              {selected && !canAudit && !auditBusy ? (
                <div className="alert alert-info">
                  Auditing needs the structured invoice fields. Run the extraction in step
                  3 first (a document that needs OCR cannot be audited at all).
                </div>
              ) : null}

              {report ? (
                <AuditReportView report={report} />
              ) : selected && canAudit && !auditBusy ? (
                <div className="empty">
                  No audit has been run for this document yet. Use “Run audit”.
                </div>
              ) : null}
            </div>
          </section>
        </div>
      </main>

      {modalReport ? (
        <ReportModal report={modalReport} onClose={() => setModalReport(null)} />
      ) : null}
    </div>
  );
}



