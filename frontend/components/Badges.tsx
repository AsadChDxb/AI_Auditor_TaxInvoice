/**
 * Status badges and the confidence meter, shared by the table and the report view.
 */

import type { ReactNode } from "react";

import {
  AUDIT_STATUS_LABELS,
  AUDIT_STATUS_TONES,
  EXTRACTION_STATUS_LABELS,
  FINDING_STATUS_LABELS,
  FINDING_STATUS_TONES,
  type BadgeTone,
} from "@/lib/format";
import type {
  AuditStatus,
  ExtractionStatus,
  FindingStatus,
  RuleSeverity,
} from "@/lib/types";

export function Badge({
  tone = "muted",
  children,
}: {
  tone?: BadgeTone;
  children: ReactNode;
}) {
  return <span className={`badge badge-${tone}`}>{children}</span>;
}

/** One of the four audit statuses returned by the API. */
export function AuditStatusBadge({ status }: { status: AuditStatus }) {
  return <Badge tone={AUDIT_STATUS_TONES[status]}>{AUDIT_STATUS_LABELS[status]}</Badge>;
}

/** Text-extraction outcome of an uploaded PDF. */
export function ExtractionStatusBadge({ status }: { status: ExtractionStatus }) {
  return (
    <Badge tone={status === "extracted" ? "good" : "warn"}>
      {EXTRACTION_STATUS_LABELS[status]}
    </Badge>
  );
}

/** Outcome of a single rule. */
export function FindingStatusBadge({ status }: { status: FindingStatus }) {
  return <Badge tone={FINDING_STATUS_TONES[status]}>{FINDING_STATUS_LABELS[status]}</Badge>;
}

/** Rule severity, toned by how much attention it deserves. */
export function SeverityBadge({ severity }: { severity: RuleSeverity }) {
  const tone: BadgeTone =
    severity === "critical" || severity === "high"
      ? "danger"
      : severity === "medium"
        ? "warn"
        : "muted";
  return <Badge tone={tone}>{severity}</Badge>;
}

/** Horizontal confidence bar; the tone follows the review threshold. */
export function ConfidenceMeter({
  value,
  threshold = 80,
  width = 90,
}: {
  value: number;
  threshold?: number;
  width?: number;
}) {
  const clamped = Math.max(0, Math.min(100, value));
  const tone = clamped >= threshold ? "good" : clamped >= 60 ? "warn" : "danger";
  return (
    <span className="row-wrap" style={{ gap: 6 }}>
      <span
        className="confidence-bar"
        style={{ width }}
        role="img"
        aria-label={`Overall confidence ${clamped.toFixed(1)} percent`}
      >
        <span
          className="confidence-fill"
          data-tone={tone}
          style={{ width: `${clamped}%` }}
        />
      </span>
      <span className="cell-mono">{clamped.toFixed(1)}%</span>
    </span>
  );
}
