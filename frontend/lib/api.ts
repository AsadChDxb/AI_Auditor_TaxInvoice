/**
 * Typed client for the UAE VAT Audit API in `backend/`.
 *
 * The base URL defaults to the documented uvicorn address and can be overridden with
 * `NEXT_PUBLIC_API_BASE_URL` (see `.env.local.example`). Every failure is surfaced as an
 * `ApiError` carrying the HTTP status plus the API's own `detail` message, so the UI can
 * show what actually went wrong (409 needs-OCR, 503 missing LLM key, ...) instead of a
 * generic error.
 */

import type {
  AuditReport,
  DocumentAnalysisResponse,
  DocumentDetail,
  DocumentListResponse,
  DocumentSummary,
  HealthResponse,
  InvoiceData,
  InvoiceExtractionResponse,
} from "./types";

export const API_BASE_URL = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000"
).replace(/\/+$/, "");

/** An API call that failed, either because of the API or because it was unreachable. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }

  /** True when the backend could not be reached at all (no HTTP response). */
  get offline(): boolean {
    return this.status === 0;
  }
}

/** Turn a FastAPI error body into a single readable sentence. */
async function describeFailure(response: Response): Promise<string> {
  try {
    const body: unknown = await response.json();
    if (body && typeof body === "object" && "detail" in body) {
      const detail = (body as { detail: unknown }).detail;
      if (typeof detail === "string" && detail.trim()) {
        return detail;
      }
      if (Array.isArray(detail) && detail.length > 0) {
        return detail
          .map((item) =>
            item && typeof item === "object" && "msg" in item
              ? String((item as { msg: unknown }).msg)
              : JSON.stringify(item),
          )
          .join("; ");
      }
    }
    return JSON.stringify(body);
  } catch {
    return `${response.status} ${response.statusText}`.trim();
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;

  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      cache: "no-store",
      ...init,
    });
  } catch {
    throw new ApiError(
      `Cannot reach the API at ${API_BASE_URL}. Start the backend with ` +
        `".\\.venv\\Scripts\\python.exe -m uvicorn app.main:app --reload" in backend/, ` +
        `then reload this page.`,
      0,
    );
  }

  if (!response.ok) {
    throw new ApiError(await describeFailure(response), response.status);
  }

  return (await response.json()) as T;
}

/** `GET /health` — liveness probe used by the header indicator. */
export function checkHealth(): Promise<HealthResponse> {
  return request<HealthResponse>("/health");
}

/** `GET /documents` — one row per document, newest upload first. */
export function listDocuments(): Promise<DocumentListResponse> {
  return request<DocumentListResponse>("/documents");
}

/** `GET /documents/{document_no}` — metadata, page summaries and invoice fields. */
export function getDocument(
  documentNo: string,
  includeText = false,
): Promise<DocumentDetail> {
  const query = includeText ? "?include_text=true" : "";
  return request<DocumentDetail>(
    `/documents/${encodeURIComponent(documentNo)}${query}`,
  );
}

/** `POST /documents/upload` — upload one PDF and receive its document number. */
export function uploadDocument(file: File): Promise<DocumentSummary> {
  const form = new FormData();
  form.append("file", file);

  // No Content-Type header: the browser adds the multipart boundary itself.
  return request<DocumentSummary>("/documents/upload", {
    method: "POST",
    body: form,
  });
}

/** `POST /documents/{document_no}/extract` — run structured field extraction. */
export function extractInvoice(
  documentNo: string,
  force = false,
): Promise<InvoiceExtractionResponse> {
  const query = force ? "?force=true" : "";
  return request<InvoiceExtractionResponse>(
    `/documents/${encodeURIComponent(documentNo)}/extract${query}`,
    { method: "POST" },
  );
}

/**
 * `POST /analysis` — text extraction, invoice extraction and (optionally) the audit
 * in one request, returning a self-contained payload.
 *
 * The PDF is sent again on every call on purpose: a serverless deployment (Vercel)
 * cannot rely on the API remembering an earlier upload, because the next request may
 * be served by a different instance. Re-send `documentNo` to keep the same row and
 * `invoice` to re-audit the already extracted fields without another extraction call.
 */
export function analyzeDocument(
  file: File,
  options: {
    documentNo?: string | null;
    invoice?: InvoiceData | null;
    runAudit?: boolean;
  } = {},
): Promise<DocumentAnalysisResponse> {
  const form = new FormData();
  form.append("file", file);

  if (options.documentNo) {
    form.append("document_no", options.documentNo);
  }
  if (options.invoice) {
    form.append("invoice", JSON.stringify(options.invoice));
  }

  const query = options.runAudit === false ? "?run_audit=false" : "";

  // No Content-Type header: the browser adds the multipart boundary itself.
  return request<DocumentAnalysisResponse>(`/analysis${query}`, {
    method: "POST",
    body: form,
  });
}

/** `POST /audit/{document_no}` — run (or re-run) the VAT audit. */
export function auditDocument(documentNo: string): Promise<AuditReport> {
  return request<AuditReport>(`/audit/${encodeURIComponent(documentNo)}`, {
    method: "POST",
  });
}

/** `GET /audit/{document_no}` — the latest stored report, without re-running the audit. */
export function storedAuditReport(documentNo: string): Promise<AuditReport> {
  return request<AuditReport>(`/audit/${encodeURIComponent(documentNo)}`);
}
