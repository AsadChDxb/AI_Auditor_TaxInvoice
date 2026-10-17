/**
 * Small presentation helpers shared by the panels and the table.
 */

import type {
  AuditFinding,
  AuditStatus,
  ExtractionStatus,
  FindingStatus,
  RuleSeverity,
  VatClassification,
} from "./types";

/** Human labels for the four audit statuses returned by the API. */
export const AUDIT_STATUS_LABELS: Record<AuditStatus, string> = {
  completed: "Completed",
  completed_with_warnings: "Completed with warnings",
  requires_review: "Requires review",
  cannot_audit: "Cannot audit",
};

/** Badge tone per audit status. */
export const AUDIT_STATUS_TONES: Record<AuditStatus, BadgeTone> = {
  completed: "good",
  completed_with_warnings: "warn",
  requires_review: "danger",
  cannot_audit: "muted",
};

/** Human labels for the two text-extraction outcomes. */
export const EXTRACTION_STATUS_LABELS: Record<ExtractionStatus, string> = {
  extracted: "Text extracted",
  needs_ocr: "Needs OCR",
};

/** Human labels for one rule outcome. */
export const FINDING_STATUS_LABELS: Record<FindingStatus, string> = {
  pass: "Pass",
  fail: "Fail",
  warning: "Warning",
  not_applicable: "Not applicable",
  review_required: "Review required",
};

/** Badge tone per rule outcome. */
export const FINDING_STATUS_TONES: Record<FindingStatus, BadgeTone> = {
  pass: "good",
  fail: "danger",
  warning: "warn",
  not_applicable: "muted",
  review_required: "warn",
};

/** Human labels for the four VAT treatments. */
export const CLASSIFICATION_LABELS: Record<VatClassification, string> = {
  standard_rated: "Standard rated (5%)",
  zero_rated: "Zero rated (0%)",
  exempt: "Exempt",
  unknown: "Unknown — needs review",
};

/** Severity ordering, worst first, for the findings list. */
export const SEVERITY_ORDER: RuleSeverity[] = ["critical", "high", "medium", "low"];

export type BadgeTone = "good" | "warn" | "danger" | "muted" | "info";

/** `1048576` → `"1 MB"`. */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "—";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[unit]}`;
}

/** ISO timestamp → a short local date and time. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return "—";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** `"supplier_trn"` → `"Supplier TRN"`, except for the acronyms we want to keep. */
export function humaniseFieldName(field: string): string {
  const overrides: Record<string, string> = {
    supplier_trn: "Supplier TRN",
    customer_trn: "Customer TRN",
    vat_amount: "VAT amount",
    vat_rate_percent: "VAT rate (%)",
    vat_classification: "VAT classification",
    total_including_vat: "Total (incl. VAT)",
    subtotal_excluding_vat: "Subtotal (excl. VAT)",
  };
  if (overrides[field]) {
    return overrides[field];
  }
  return field
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

/** `null` and empty strings read as an em dash in tables. */
export function orDash(value: string | null | undefined): string {
  return value === null || value === undefined || value === "" ? "—" : value;
}

/** Format a confidence percentage for display. */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return "—";
  }
  return `${value.toFixed(1)}%`;
}

/** Order findings worst-first so a reviewer sees problems immediately. */
export function sortFindings(findings: AuditFinding[]): AuditFinding[] {
  const statusRank: Record<FindingStatus, number> = {
    fail: 0,
    review_required: 1,
    warning: 2,
    pass: 3,
    not_applicable: 4,
  };
  return [...findings].sort((left, right) => {
    const byStatus = statusRank[left.status] - statusRank[right.status];
    if (byStatus !== 0) {
      return byStatus;
    }
    const bySeverity =
      SEVERITY_ORDER.indexOf(left.severity) - SEVERITY_ORDER.indexOf(right.severity);
    if (bySeverity !== 0) {
      return bySeverity;
    }
    return left.rule_id.localeCompare(right.rule_id);
  });
}

/** Count findings that need a human: failures, warnings and review flags. */
export function findingsNeedingAttention(findings: AuditFinding[]): number {
  return findings.filter((finding) =>
    ["fail", "warning", "review_required"].includes(finding.status),
  ).length;
}
