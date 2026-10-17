"use client";

/**
 * Renders an `AuditReport` from `POST /audit/{document_no}` or
 * `GET /audit/{document_no}`.
 *
 * Shared by the audit panel and the "View report" modal so both always show exactly
 * the same thing. The report is presented as a review aid: the header carries the
 * status, confidence and provider, the summary carries the counts, and every finding
 * shows its own explanation, expected/actual values, evidence and recommended action.
 */

import {
  AuditStatusBadge,
  ConfidenceMeter,
  FindingStatusBadge,
  SeverityBadge,
} from "@/components/Badges";
import { formatDateTime, orDash, sortFindings } from "@/lib/format";
import type { AuditReport } from "@/lib/types";

export function AuditReportView({ report }: { report: AuditReport }) {
  const findings = sortFindings(report.findings);

  return (
    <div className="stack">
      <div className="row-wrap" style={{ justifyContent: "space-between" }}>
        <div className="row-wrap">
          <AuditStatusBadge status={report.audit_status} />
          {report.human_review_required ? (
            <span className="badge badge-danger">Human review required</span>
          ) : (
            <span className="badge badge-good">No human review needed</span>
          )}
        </div>
        <ConfidenceMeter
          value={report.overall_confidence_percent}
          width={140}
          threshold={80}
        />
      </div>

      <p>{report.overall_assessment}</p>
      {report.confidence_explanation ? (
        <p className="small muted">{report.confidence_explanation}</p>
      ) : null}
      {report.message ? <p className="small muted">{report.message}</p> : null}

      <div className="summary-chips">
        <span className="badge badge-muted">{report.summary.total} findings</span>
        <span className="badge badge-good">{report.summary.passed} passed</span>
        <span className="badge badge-danger">{report.summary.failed} failed</span>
        <span className="badge badge-warn">{report.summary.warnings} warnings</span>
        <span className="badge badge-warn">
          {report.summary.review_required} need review
        </span>
        <span className="badge badge-muted">
          {report.summary.not_applicable} not applicable
        </span>
        <span className="badge badge-info">
          {report.summary.enabled_rule_count} rules enabled
        </span>
      </div>

      <div className="meta-grid">
        <div className="meta-item">
          <div className="meta-label">Document</div>
          <div className="meta-value mono">{report.document_no}</div>
        </div>
        <div className="meta-item">
          <div className="meta-label">Invoice number</div>
          <div className="meta-value">{orDash(report.invoice_number)}</div>
        </div>
        <div className="meta-item">
          <div className="meta-label">Audited at</div>
          <div className="meta-value">{formatDateTime(report.audited_at)}</div>
        </div>
        <div className="meta-item">
          <div className="meta-label">Provider / model</div>
          <div className="meta-value">
            {report.provider} / {report.model ?? "—"}
          </div>
        </div>
        <div className="meta-item">
          <div className="meta-label">Ruleset version</div>
          <div className="meta-value mono">{report.ruleset_version}</div>
        </div>
        <div className="meta-item">
          <div className="meta-label">Model calls</div>
          <div className="meta-value">
            {report.trace.model_calls} ({report.trace.iterations} iteration
            {report.trace.iterations === 1 ? "" : "s"})
          </div>
        </div>
      </div>

      {report.review_reasons.length > 0 ? (
        <div className="alert alert-error">
          <div>
            <strong>Why a human must look at this</strong>
            <ul className="notes" style={{ color: "inherit" }}>
              {report.review_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {report.warnings.length > 0 ? (
        <div className="alert alert-warn">
          <div>
            <strong>Audit warnings</strong>
            <ul className="notes" style={{ color: "inherit" }}>
              {report.warnings.map((warning) => (
                <li key={warning}>{warning}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      {findings.length > 0 ? (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Rule</th>
                <th>Status</th>
                <th>Severity</th>
                <th className="right">Confidence</th>
                <th>Explanation</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {findings.map((finding) => (
                <tr key={`${finding.rule_id}-${finding.source}`}>
                  <td>
                    <div className="cell-mono cell-strong">{finding.rule_id}</div>
                    <div className="cell-muted">
                      {finding.rule_title ?? "—"}
                      {finding.page_number !== null
                        ? ` · page ${finding.page_number}`
                        : ""}
                    </div>
                  </td>
                  <td>
                    <div className="stack-tight">
                      <FindingStatusBadge status={finding.status} />
                      {finding.human_review_required ? (
                        <span className="cell-muted">needs review</span>
                      ) : null}
                    </div>
                  </td>
                  <td>
                    <SeverityBadge severity={finding.severity} />
                  </td>
                  <td className="right cell-mono">
                    {finding.confidence_percent.toFixed(1)}%
                  </td>
                  <td>
                    <div className="finding-explanation">
                      <div>{finding.explanation}</div>
                      {finding.expected_value || finding.actual_value ? (
                        <div className="finding-values small">
                          expected {orDash(finding.expected_value)} · actual{" "}
                          {orDash(finding.actual_value)}
                        </div>
                      ) : null}
                      {finding.evidence ? (
                        <div className="cell-muted">Evidence: {finding.evidence}</div>
                      ) : null}
                      {finding.recommended_action ? (
                        <div className="cell-muted">
                          Action: {finding.recommended_action}
                        </div>
                      ) : null}
                      {finding.human_review_reason ? (
                        <div className="cell-muted">
                          Review because: {finding.human_review_reason}
                        </div>
                      ) : null}
                    </div>
                  </td>
                  <td className="cell-muted">{finding.source}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="alert alert-warn">
          The engine produced no findings for this document, so nothing was verified
          against the ruleset.
        </div>
      )}

      <details className="small muted">
        <summary>How this report was produced (trace)</summary>
        <div className="meta-grid" style={{ marginTop: 10 }}>
          <div className="meta-item">
            <div className="meta-label">Nodes</div>
            <div className="meta-value mono small">
              {report.trace.nodes.join(" → ") || "—"}
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-label">Tools requested</div>
            <div className="meta-value mono small">
              {report.trace.tool_calls.join(", ") || "—"}
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-label">Re-extraction performed</div>
            <div className="meta-value">
              {report.trace.re_extraction_performed ? "yes" : "no"}
            </div>
          </div>
          <div className="meta-item">
            <div className="meta-label">Rules evaluated</div>
            <div className="meta-value mono small">
              {report.trace.rules_evaluated.join(", ") || "—"}
            </div>
          </div>
        </div>
      </details>
    </div>
  );
}

