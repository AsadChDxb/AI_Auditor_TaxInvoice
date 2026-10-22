/**
 * TypeScript mirror of the FastAPI response schemas (`backend/app/models`).
 *
 * Kept hand-written and deliberately narrow: only the fields the UI renders are
 * declared, so a backend addition never breaks the build. Money fields arrive as
 * strings because the API serialises `Decimal` values as text to protect precision.
 */

export type ExtractionStatus = "extracted" | "needs_ocr";

/** Engine that produced a page's text: pdfplumber first, PaddleOCR on fallback. */
export type ExtractionSource = "pdfplumber" | "paddleocr";

/** Report of the extraction quality check that runs before the OCR fallback. */
export interface ExtractionQualityInfo {
  passed: boolean;
  required_fields_found: string[];
  missing_required_fields: string[];
  pages_needing_ocr: number[];
  table_pages: number[];
  ocr_attempted: boolean;
  ocr_pages: number[];
  issues: string[];
  summary: string;
}

export type VatClassification =
  | "standard_rated"
  | "zero_rated"
  | "exempt"
  | "unknown";

export type AuditStatus =
  | "completed"
  | "completed_with_warnings"
  | "requires_review"
  | "cannot_audit";

export type FindingStatus =
  | "pass"
  | "fail"
  | "warning"
  | "not_applicable"
  | "review_required";

export type RuleSeverity = "critical" | "high" | "medium" | "low";

export type FindingSource = "deterministic" | "model" | "guardrail";

export interface HealthResponse {
  status: string;
}

/** Response of `POST /documents/upload`. */
export interface DocumentSummary {
  document_no: string;
  original_filename: string;
  content_type: string | null;
  size_bytes: number;
  uploaded_at: string;
  page_count: number;
  extraction_status: ExtractionStatus;
  extraction_error: string | null;
  pages_with_text: number;
  total_characters: number;
  /** Phase 9: extraction provenance and quality report. */
  used_ocr?: boolean;
  ocr_pages?: number[];
  table_count?: number;
  quality?: ExtractionQualityInfo | null;
  message: string | null;
}

/** One page of the text extraction. */
export interface PageContent {
  page_number: number;
  character_count: number;
  has_text: boolean;
  preview: string;
  /** Full page text; only present when the request asked for `include_text`. */
  text: string | null;
  /** Phase 9: which engine produced this page's text. */
  source?: ExtractionSource;
  table_count?: number;
  /** Recovered table rows; only present when the request asked for `include_text`. */
  tables?: string[][][] | null;
}

/** Model-reported provenance for a single invoice field. */
export interface FieldEvidence {
  value: string | null;
  confidence_percent: number | null;
  page_number: number | null;
  evidence: string | null;
}

/** Validated structured invoice fields (`InvoiceData`). */
export interface InvoiceData {
  invoice_number: string | null;
  invoice_date: string | null;
  supplier_name: string | null;
  supplier_trn: string | null;
  customer_name: string | null;
  customer_trn: string | null;
  currency: string | null;
  subtotal_excluding_vat: string | null;
  vat_amount: string | null;
  total_including_vat: string | null;
  vat_rate_percent: string | null;
  vat_classification: VatClassification;
  field_evidence: Record<string, FieldEvidence>;
  missing_fields: string[];
  warnings: string[];
  provider: string;
  model: string | null;
  extracted_at: string;
}

/** Response of `POST /documents/{document_no}/extract`. */
export interface InvoiceExtractionResponse {
  document_no: string;
  reused_existing: boolean;
  invoice: InvoiceData;
  message: string | null;
}

/** Response of `GET /documents/{document_no}`. */
export interface DocumentDetail {
  document_no: string;
  original_filename: string;
  content_type: string | null;
  size_bytes: number;
  uploaded_at: string;
  page_count: number;
  extraction_status: ExtractionStatus;
  extraction_error: string | null;
  pages_with_text: number;
  total_characters: number;
  raw_text_included: boolean;
  /** Phase 9: extraction provenance and quality report. */
  used_ocr?: boolean;
  ocr_pages?: number[];
  table_count?: number;
  quality?: ExtractionQualityInfo | null;
  pages: PageContent[];
  invoice: InvoiceData | null;
  notes: string[];
}

/** Counts shown in the report header. */
export interface AuditSummary {
  total: number;
  passed: number;
  failed: number;
  warnings: number;
  not_applicable: number;
  review_required: number;
  severity_counts: Record<string, number>;
  ruleset_version: string;
  enabled_rule_count: number;
}

/** One rule outcome. */
export interface AuditFinding {
  rule_id: string;
  rule_title: string | null;
  status: FindingStatus;
  severity: RuleSeverity;
  explanation: string;
  expected_value: string | null;
  actual_value: string | null;
  page_number: number | null;
  evidence: string | null;
  confidence_percent: number;
  human_review_required: boolean;
  human_review_reason: string | null;
  recommended_action: string | null;
  source: FindingSource;
}

/** Compact, secret-free record of how an audit was produced. */
export interface AuditTrace {
  nodes: string[];
  iterations: number;
  model_calls: number;
  tool_calls: string[];
  re_extraction_performed: boolean;
  rules_evaluated: string[];
}

/** Response of `POST /audit/{document_no}` and `GET /audit/{document_no}`. */
export interface AuditReport {
  document_no: string;
  invoice_number: string | null;
  audit_status: AuditStatus;
  overall_assessment: string;
  overall_confidence_percent: number;
  confidence_explanation: string;
  provider: string;
  model: string | null;
  ruleset_version: string;
  audited_at: string;
  findings: AuditFinding[];
  summary: AuditSummary;
  warnings: string[];
  review_reasons: string[];
  human_review_required: boolean;
  trace: AuditTrace;
  message: string | null;
}

/** One row of `GET /documents`: the whole workflow state for one document. */
export interface DocumentListItem {
  document_no: string;
  original_filename: string;
  content_type: string | null;
  size_bytes: number;
  uploaded_at: string;
  page_count: number;
  extraction_status: ExtractionStatus;
  extraction_error: string | null;
  pages_with_text: number;
  total_characters: number;
  invoice_extracted: boolean;
  invoice_number: string | null;
  invoice_total_including_vat: string | null;
  vat_classification: VatClassification | null;
  audited: boolean;
  audit_status: AuditStatus | null;
  audit_overall_confidence_percent: number | null;
  audit_human_review_required: boolean | null;
  audit_summary: AuditSummary | null;
  audit_ruleset_version: string | null;
  audited_at: string | null;
  can_extract: boolean;
  can_audit: boolean;
}

/** Response of `GET /documents`. */
export interface DocumentListResponse {
  total: number;
  documents: DocumentListItem[];
  notes: string[];
}

/**
 * Response of `POST /analysis`: the whole workflow in one self-contained payload.
 *
 * The API keeps documents in process memory, so on a serverless or multi-instance
 * deployment a follow-up request can reach an instance that never saw the upload.
 * `/analysis` therefore returns the document detail, the invoice, the report and the
 * table row together, and the client sends the PDF again for every action.
 */
export interface DocumentAnalysisResponse {
  document: DocumentDetail;
  report: AuditReport | null;
  row: DocumentListItem;
  message: string;
  notes: string[];
}

/** What a bundled sample is built to prove, read from its file-name convention. */
export type SampleExpectedOutcome = "pass" | "fail" | "unknown";

/** One bundled sample invoice served read-only by the API (`GET /samples`). */
export interface SampleInvoice {
  file_name: string;
  title: string;
  supplier_name: string | null;
  size_bytes: number;
  expected_outcome: SampleExpectedOutcome;
  /** Relative API path that streams the PDF, for example `/samples/01_....pdf`. */
  url: string;
}

/** Response of `GET /samples`. */
export interface SampleInvoiceListResponse {
  total: number;
  samples: SampleInvoice[];
  notes: string[];
}
