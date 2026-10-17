"use client";

/**
 * Full-screen reader for a stored audit report.
 *
 * Used by the table's "View report" action, which loads the latest report through
 * `GET /audit/{document_no}` so nothing is re-run and no model call is made.
 */

import { useEffect } from "react";

import { AuditReportView } from "@/components/AuditReportView";
import type { AuditReport } from "@/lib/types";

export function ReportModal({
  report,
  onClose,
}: {
  report: AuditReport;
  onClose: () => void;
}) {
  useEffect(() => {
    function handleKey(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onClose();
      }
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return (
    <div
      className="modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label={`Audit report for ${report.document_no}`}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div className="modal">
        <header className="modal-head">
          <div>
            <h2 className="card-title">
              Audit report · {report.invoice_number ?? report.document_no}
            </h2>
            <p className="card-sub mono">{report.document_no}</p>
          </div>
          <button type="button" className="btn btn-sm" onClick={onClose} autoFocus>
            Close (Esc)
          </button>
        </header>
        <div className="modal-body">
          <AuditReportView report={report} />
        </div>
      </div>
    </div>
  );
}
