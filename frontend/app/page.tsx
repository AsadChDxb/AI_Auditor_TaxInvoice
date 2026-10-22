"use client";

/**
 * The single screen of the console.
 *
 * It drives the documented workflow against the FastAPI backend and keeps every result
 * on one page:
 *
 * 1. upload a PDF and extract its invoice fields in one request (`POST /analysis`),
 * 2. show the output of the uploaded document (page-level text extraction),
 * 3. show the structured invoice data from that same response,
 * 4. audit the invoice and show the report (`POST /analysis` with the invoice),
 * 5. keep every document in one table row with its status, result and the
 *    Run audit / View report actions.
 *
 * Deployment note: the API keeps documents in process memory, which a serverless host
 * (Vercel) cannot share between requests, so the screen never relies on a document
 * number resolving on the server. The PDF and the extracted invoice fields stay in
 * this browser tab and are sent again for every action; `POST /analysis` returns a
 * self-contained payload (document + invoice + report + table row). The classic
 * three-step endpoints are still used for rows the server itself knows about.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ArchitectureModal } from "@/components/ArchitectureModal";
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
  analyzeDocument,
  auditDocument,
  checkHealth,
  extractInvoice,
  getDocument,
  listDocuments,
  listSamples,
  storedAuditReport,
} from "@/lib/api";
import { AUDIT_STATUS_LABELS, formatPercent } from "@/lib/format";
import type {
  AuditReport,
  DocumentDetail,
  DocumentListItem,
  DocumentSummary,
  InvoiceData,
  SampleInvoice,
} from "@/lib/types";

type BusyAction = "extract" | "audit" | "report" | null;
type Health = "checking" | "ok" | "down";

/**
 * One document whose result lives in this browser tab.
 *
 * `file` is kept so a re-extraction or a re-audit can send the PDF again; nothing here
 * depends on the API still holding the document under its number.
 */
type LocalAnalysis = {
  file: File;
  detail: DocumentDetail;
  report: AuditReport | null;
  row: DocumentListItem;
};

const CLIENT_STORE_NOTE =
  "Results are kept in this browser tab, not on the server: the PDF is sent again " +
  "with every action, so the API does not need shared memory between requests. " +
  "Reloading the page clears the table.";

/** Turn anything thrown by the API layer into a sentence for the user. */
function messageOf(error: unknown): string {
  if (error instanceof ApiError || error instanceof Error) {
    return error.message;
  }
  return "Unexpected error.";
}

/** Merge rows the API still knows about with the rows held in this tab. */
function mergeRows(
  serverRows: DocumentListItem[],
  analyses: Record<string, LocalAnalysis>,
): DocumentListItem[] {
  const rows = new Map<string, DocumentListItem>();

  for (const row of serverRows) {
    rows.set(row.document_no, row);
  }
  // A local row is newer information (it was just produced by this tab), so it wins.
  for (const analysis of Object.values(analyses)) {
    rows.set(analysis.row.document_no, analysis.row);
  }

  return [...rows.values()].sort((left, right) =>
    right.uploaded_at.localeCompare(left.uploaded_at),
  );
}

/** Build the `POST /documents/upload` summary shape from an analysis response. */
function summaryOf(detail: DocumentDetail, message: string): DocumentSummary {
  return {
    document_no: detail.document_no,
    original_filename: detail.original_filename,
    content_type: detail.content_type,
    size_bytes: detail.size_bytes,
    uploaded_at: detail.uploaded_at,
    page_count: detail.page_count,
    extraction_status: detail.extraction_status,
    extraction_error: detail.extraction_error,
    pages_with_text: detail.pages_with_text,
    total_characters: detail.total_characters,
    used_ocr: detail.used_ocr,
    ocr_pages: detail.ocr_pages,
    table_count: detail.table_count,
    quality: detail.quality,
    message,
  };
}

export default function AuditConsolePage() {
  const [health, setHealth] = useState<Health>("checking");

  const [serverDocuments, setServerDocuments] = useState<DocumentListItem[]>([]);
  const [analyses, setAnalyses] = useState<Record<string, LocalAnalysis>>({});
  const [notes, setNotes] = useState<string[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const documents = useMemo(
    () => mergeRows(serverDocuments, analyses),
    [serverDocuments, analyses],
  );

  const [uploadBusy, setUploadBusy] = useState(false);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<DocumentSummary | null>(null);

  // Bundled sample invoices, offered next to the dropzone so the screen is testable
  // without a PDF at hand. They still travel through the normal upload request.
  const [samples, setSamples] = useState<SampleInvoice[]>([]);
  const [samplesLoading, setSamplesLoading] = useState(true);
  const [samplesError, setSamplesError] = useState<string | null>(null);

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
  const [architectureOpen, setArchitectureOpen] = useState(false);

  const reportPanelRef = useRef<HTMLElement | null>(null);

  const refreshList = useCallback(async () => {
    setListLoading(true);
    try {
      const response = await listDocuments();
      setServerDocuments(response.documents);
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

  /** Load one document into the panels, including its report when there is one. */
  const loadDocument = useCallback(
    async (documentNo: string, withText: boolean) => {
      setSelected(documentNo);
      setExtractError(null);
      setAuditError(null);

      const local = analyses[documentNo];
      if (local) {
        // The result already lives in this tab, so no request is needed: on a
        // serverless host the API may no longer know this document number at all.
        setDetail(local.detail);
        setReport(local.report);
        setDetailLoading(false);
        return;
      }

      setDetailLoading(true);
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
    [analyses],
  );

  useEffect(() => {
    void refreshList();
    checkHealth()
      .then(() => setHealth("ok"))
      .catch(() => setHealth("down"));
  }, [refreshList]);

  useEffect(() => {
    let cancelled = false;
    setSamplesLoading(true);
    listSamples()
      .then((response) => {
        if (cancelled) {
          return;
        }
        setSamples(response.samples);
        // An empty folder is a deployment detail, not a failure: show its note instead.
        setSamplesError(
          response.samples.length === 0 ? (response.notes[0] ?? null) : null,
        );
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setSamplesError(messageOf(error));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setSamplesLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // `#architecture` deep-links the explainer: handy for a demo, and it survives a reload.
  useEffect(() => {
    function syncFromHash() {
      setArchitectureOpen(window.location.hash === "#architecture");
    }
    syncFromHash();
    window.addEventListener("hashchange", syncFromHash);
    return () => window.removeEventListener("hashchange", syncFromHash);
  }, []);

  function openArchitecture() {
    history.replaceState(null, "", "#architecture");
    setArchitectureOpen(true);
  }

  function closeArchitecture() {
    setArchitectureOpen(false);
    if (window.location.hash === "#architecture") {
      history.replaceState(
        null,
        "",
        `${window.location.pathname}${window.location.search}`,
      );
    }
  }

  // --- actions --------------------------------------------------------------------

  async function handleUpload(file: File) {
    setUploadBusy(true);
    setUploadError(null);
    setExtractError(null);
    setAuditError(null);
    try {
      // One request reads the PDF and extracts the structured fields. The audit stays
      // a separate click so the more expensive model review only runs on request.
      const result = await analyzeDocument(file, { runAudit: false });
      const local: LocalAnalysis = {
        file,
        detail: result.document,
        report: result.report,
        row: result.row,
      };

      setAnalyses((previous) => ({ ...previous, [local.row.document_no]: local }));
      setSelected(local.row.document_no);
      setDetail(local.detail);
      setReport(local.report);
      setUploadResult(summaryOf(local.detail, result.message));
      setHealth("ok");

      // Best effort: on a single long-running instance the API lists the row too.
      await refreshList();
    } catch (error) {
      setUploadError(messageOf(error));
      setUploadResult(null);
    } finally {
      setUploadBusy(false);
    }
  }

  async function handleExtract(documentNo: string, force: boolean) {
    const local = analyses[documentNo];
    setSelected(documentNo);
    setBusyRow(documentNo);
    setBusyAction("extract");
    setExtractBusy(true);
    setExtractError(null);
    try {
      if (local) {
        // Send the PDF again: the API may not hold this document in memory any more.
        const result = await analyzeDocument(local.file, {
          documentNo,
          runAudit: false,
        });
        const updated: LocalAnalysis = {
          file: local.file,
          detail: result.document,
          report: null,
          row: result.row,
        };
        setAnalyses((previous) => ({ ...previous, [documentNo]: updated }));
        setDetail(updated.detail);
        setReport(null);
        setUploadResult(summaryOf(updated.detail, result.message));
      } else {
        await extractInvoice(documentNo, force);
        await Promise.all([refreshList(), loadDocument(documentNo, includeText)]);
      }
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
    const local = analyses[documentNo];
    setSelected(documentNo);
    setBusyRow(documentNo);
    setBusyAction("audit");
    setAuditBusy(true);
    setAuditError(null);
    try {
      if (local) {
        const result = await analyzeDocument(local.file, {
          documentNo,
          invoice: local.detail.invoice,
        });
        const updated: LocalAnalysis = {
          file: local.file,
          detail: result.document,
          report: result.report,
          row: result.row,
        };
        setAnalyses((previous) => ({ ...previous, [documentNo]: updated }));
        setDetail(updated.detail);
        setReport(updated.report);
        setUploadResult(summaryOf(updated.detail, result.message));
      } else {
        const fresh = await auditDocument(documentNo);
        await Promise.all([refreshList(), loadDocument(documentNo, includeText)]);
        setReport(fresh);
      }
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
      const localReport = analyses[documentNo]?.report;
      setModalReport(localReport ?? (await storedAuditReport(documentNo)));
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
            <button
              type="button"
              className="btn btn-invert btn-sm"
              onClick={openArchitecture}
            >
              Architecture
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
        {listError && documents.length === 0 ? (
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
            samples={samples}
            samplesLoading={samplesLoading}
            samplesError={samplesError}
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
          notes={[...notes, CLIENT_STORE_NOTE]}
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

      {architectureOpen ? (
        <ArchitectureModal onClose={closeArchitecture} />
      ) : null}
      {modalReport ? (
        <ReportModal report={modalReport} onClose={() => setModalReport(null)} />
      ) : null}
    </div>
  );
}



