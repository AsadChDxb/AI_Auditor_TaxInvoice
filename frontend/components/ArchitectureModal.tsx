"use client";

/**
 * "Technical architecture": the header button's explainer.
 *
 * A demo-friendly view of how one invoice actually moves through the system — browser →
 * FastAPI → pdfplumber (+ optional OCR) → OpenAI-compatible extraction → deterministic
 * VAT rules → LangGraph review → one JSON payload. Every stage names the real module it
 * maps to, so the diagram doubles as a map of the repository.
 *
 * The motion is CSS only (a running dashed spine plus travelling packets, and a spark on
 * each horizontal connector) and is switched off under `prefers-reduced-motion`.
 */

import { Fragment, useEffect } from "react";

import { SeverityBadge } from "@/components/Badges";
import type { RuleSeverity } from "@/lib/types";

type HeroStage = {
  icon: string;
  label: string;
  tech: string;
};

type Stage = {
  kicker: string;
  title: string;
  io: string;
  points: string[];
  tech: string[];
  files: string;
};

const HERO: HeroStage[] = [
  { icon: "🖥️", label: "Browser", tech: "Next.js" },
  { icon: "⚡", label: "API", tech: "FastAPI" },
  { icon: "📄", label: "PDF read", tech: "pdfplumber" },
  { icon: "🧠", label: "AI extraction", tech: "LLM" },
  { icon: "⚖️", label: "Audit", tech: "LangGraph" },
  { icon: "📊", label: "Report", tech: "JSON" },
];

const STAGES: Stage[] = [
  {
    kicker: "Step 1 · request",
    title: "The browser posts one PDF",
    io: "File → multipart/form-data",
    points: [
      "The reviewer drops a PDF (10 MB cap) or picks a bundled sample; this tab keeps the File object.",
      "A single multipart request goes to POST /analysis with the PDF, plus document_no/invoice only when a known row is re-run.",
      "Nothing relies on server memory: the PDF is re-sent for every action, which is what keeps a serverless host working.",
    ],
    tech: ["Next.js 15", "React 19", "TypeScript"],
    files: "frontend/app/page.tsx → lib/api.ts · POST /analysis",
  },
  {
    kicker: "Step 2 · API edge",
    title: "FastAPI validates and wires the request",
    io: "upload → DocumentRecord",
    points: [
      "CORS is an explicit allowlist (VAT_AUDIT_CORS_ALLOW_ORIGINS); there are no cookies and no session.",
      "The upload is streamed into memory under the 10 MB limit, then the %PDF- signature is checked, so a renamed file is rejected by content.",
      "Dependencies inject the OCR engine, the extraction service and the auditor, which keeps the route thin and lets tests stub each part.",
    ],
    tech: ["FastAPI", "Pydantic v2", "python-multipart"],
    files: "app/api/analysis.py · app/config.py",
  },
  {
    kicker: "Step 3 · reading the PDF",
    title: "pdfplumber recovers text and tables",
    io: "bytes → pages, text, tables",
    points: [
      "Per page: characters become text with collapsed whitespace, ruled lines become a (rows × cols) cell grid, and image coverage is measured.",
      "Text and tables keep their page numbers, so a later finding can cite the page it came from.",
      "This is the PDF's own text layer, not pixels: the five bundled samples take this path without OCR.",
    ],
    tech: ["pdfplumber", "pdfminer.six", "pypdfium2"],
    files: "app/services/pdf_extractor.py · extract_page, extract_page_tables",
  },
  {
    kicker: "Step 4 · quality gate",
    title: "Thin pages go to OCR, or the run stops",
    io: "pages → readable? yes / no",
    points: [
      "build_extraction_quality probes the required fields and flags any page whose text is below VAT_AUDIT_OCR_MIN_PAGE_CHARACTERS.",
      "A candidate page is rendered at 150 DPI and read by PaddleOCR; its lines are grouped into rows and split into cells to rebuild the table.",
      "If OCR is missing or still finds nothing, the document is marked needs_ocr and returned early - no guessed fields.",
    ],
    tech: ["PaddleOCR (optional)", "150 DPI", "row reconstruction"],
    files: "app/services/ocr_fallback.py · build_extraction_quality, apply_ocr_fallback",
  },
  {
    kicker: "Step 5 · AI layer",
    title: "The model fills a fixed JSON contract",
    io: "text + tables → InvoiceData",
    points: [
      "The prompt carries the page text, the reconstructed tables and the field contract; the provider is any OpenAI-compatible endpoint (base URL, key, model, structured method).",
      "The answer is schema-validated before use: money becomes Decimal, dates are normalised to ISO, and \"not stated\" / \"N/A\" stay null instead of being guessed.",
      "Every field keeps its own evidence and confidence, and each judgement call is recorded as a warning on the invoice.",
    ],
    tech: ["OpenAI-compatible", "JSON mode", "Pydantic validation"],
    files: "app/services/llm.py · app/services/invoice_extractor.py",
  },
  {
    kicker: "Step 6 · deterministic rules",
    title: "VAT arithmetic is recomputed in Python",
    io: "InvoiceData + ruleset → findings",
    points: [
      "Six configured checks run on the extracted values (right-hand panel); each returns pass, fail, warning or review_required with expected vs actual.",
      "Money is Decimal, the rounding policy is configured (ROUND_HALF_UP by default) and comparisons use a 0.01 AED tolerance.",
      "No model is involved here: a wrong total is arithmetic, not an opinion.",
    ],
    tech: ["Decimal", "ROUND_HALF_UP", "0.01 AED tolerance"],
    files: "app/audit/nodes.py · app/audit/rules_config.py · app/audit/tools.py",
  },
  {
    kicker: "Step 7 · agentic review",
    title: "LangGraph coordinates model and tools",
    io: "findings + evidence → AuditReport",
    points: [
      "load_context → select_rules → deterministic_checks → agent_review → guardrail_validate; human_review, cannot_audit and aggregate_report close the run.",
      "The model may only reason over configured rules and deterministic tools (recompute, mask identifiers); a bounded re_extract_fields → select_rules loop can ask for evidence again.",
      "Guardrails reject unknown or disabled rule IDs, clamp confidence, and raise a human-review flag below the configured threshold (80%).",
    ],
    tech: ["LangGraph", "langchain-core", "guardrails"],
    files: "app/audit/graph.py · app/audit/nodes.py · app/audit/tools.py",
  },
  {
    kicker: "Step 8 · payload",
    title: "One response drives the whole screen",
    io: "JSON → panels + table row",
    points: [
      "The response carries the document, the extracted invoice, the report and the table row together, so no follow-up request needs shared memory.",
      "The UI renders page text/tables, the field panel with per-field evidence, findings with expected vs actual, confidence and human-review flags.",
      "The bundled samples are served by GET /samples and take exactly this same path once they are uploaded.",
    ],
    tech: ["Pydantic response", "stateless client"],
    files: "app/models/analysis.py · frontend/components/*Panel.tsx",
  },
];

const STACK: { label: string; value: string }[] = [
  { label: "UI", value: "Next.js 15 · React 19 · TypeScript 5" },
  { label: "API", value: "FastAPI · Uvicorn · Pydantic v2 · pydantic-settings" },
  { label: "PDF", value: "pdfplumber (pdfminer.six, Pillow, pypdfium2)" },
  { label: "OCR (optional)", value: "PaddleOCR + PaddlePaddle, CPU by default" },
  { label: "AI", value: "OpenAI-compatible chat completions, JSON mode" },
  { label: "Agent", value: "LangGraph · langchain-core · langchain-openai" },
  { label: "Rules", value: "Python Decimal · versioned ruleset (v1.0.0)" },
  { label: "Storage", value: "In-process registry; the browser keeps the PDF" },
];

const RULES: { id: string; severity: RuleSeverity; note: string }[] = [
  {
    id: "VAT_RATE_CHECK",
    severity: "high",
    note: "Reported rate vs 5% for standard-rated (0% for zero-rated or exempt).",
  },
  {
    id: "SUPPLIER_TRN_CHECK",
    severity: "medium",
    note: "Supplier TRN present and 15 digits - a format check only.",
  },
  {
    id: "CUSTOMER_TRN_CHECK",
    severity: "low",
    note: "Customer TRN, enforced only when the deployment asks for it.",
  },
  {
    id: "TAXABLE_AMOUNT_VAT_MISMATCH",
    severity: "high",
    note: "Recomputed taxable × rate compared with the printed VAT.",
  },
  {
    id: "INVOICE_TOTAL_CHECK",
    severity: "high",
    note: "Taxable + VAT compared with the printed grand total.",
  },
  {
    id: "VAT_CLASSIFICATION_REVIEW",
    severity: "medium",
    note: "An unknown VAT treatment goes to review, never to a guess.",
  },
];

const GUARDRAILS: string[] = [
  "Monetary arithmetic is recomputed in Python; a model can never set a verdict on its own.",
  "Only configured rule IDs become findings; anything else is recorded as a warning.",
  "Missing values stay null and are listed on the invoice, instead of being filled in.",
  "Confidence below 80% or an unknown VAT treatment raises a human-review flag.",
  "Findings are a review aid, not a VAT compliance certificate.",
];

const FAILURES: { code: string; note: string }[] = [
  {
    code: "413",
    note: "Upload above VAT_AUDIT_MAX_UPLOAD_BYTES (10 MB by default), refused while streaming.",
  },
  {
    code: "415",
    note: "No %PDF- signature: a renamed file is rejected before it is parsed.",
  },
  {
    code: "400",
    note: "Empty or corrupted PDF; the reader's own message is returned as-is.",
  },
  {
    code: "needs_ocr",
    note: "Image-only PDF and OCR unavailable: the run stops instead of guessing fields.",
  },
  {
    code: "409",
    note: "Audit requested without readable text or extracted invoice fields.",
  },
  {
    code: "503 · 502 · 504",
    note: "LLM key missing, provider error or timeout - each with a short sanitised reason.",
  },
];

export function ArchitectureModal({ onClose }: { onClose: () => void }) {
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
      aria-label="Technical architecture and data flow"
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          onClose();
        }
      }}
    >
      <div className="modal arch-modal">
        <header className="modal-head">
          <div>
            <h2 className="card-title">Technical architecture</h2>
            <p className="card-sub">
              One invoice, end to end: request → PDF text → AI extraction → deterministic
              audit → report.
            </p>
          </div>
          <div className="row-wrap">
            <span className="arch-live">
              <span className="arch-live-dot" aria-hidden="true" />
              data in flight
            </span>
            <button type="button" className="btn btn-sm" onClick={onClose} autoFocus>
              Close (Esc)
            </button>
          </div>
        </header>

        <div className="modal-body arch-body">
          <section className="arch-hero">
            <div className="arch-strip">
              {HERO.map((stage, index) => (
                <Fragment key={stage.label}>
                  <span className="arch-chip">
                    <span className="arch-chip-icon" aria-hidden="true">
                      {stage.icon}
                    </span>
                    <span className="arch-chip-label">{stage.label}</span>
                    <span className="arch-chip-tech">{stage.tech}</span>
                  </span>
                  {index < HERO.length - 1 ? (
                    <span className="arch-link" aria-hidden="true">
                      <span
                        className="arch-spark"
                        style={{ animationDelay: `${index * 0.28}s` }}
                      />
                    </span>
                  ) : null}
                </Fragment>
              ))}
            </div>
            <p className="arch-hero-note">
              The same request runs both directions:{" "}
              <span className="mono small">POST /analysis</span> returns the document, the
              extracted invoice, the audit report and the table row in one payload. The
              samples offered in Step 1 travel this exact path.
            </p>
          </section>

          <div className="arch-columns">
            <section className="arch-flow" aria-label="Stage-by-stage data flow">
              <span
                className="arch-packet"
                style={{ animationDelay: "0s" }}
                aria-hidden="true"
              />
              <span
                className="arch-packet"
                style={{ animationDelay: "2.5s" }}
                aria-hidden="true"
              />
              <span
                className="arch-packet"
                style={{ animationDelay: "5s" }}
                aria-hidden="true"
              />

              {STAGES.map((stage, index) => (
                <article className="arch-stage" key={stage.title}>
                  <span className="arch-node" aria-hidden="true">
                    {index + 1}
                  </span>
                  <div className="arch-card">
                    <header className="arch-card-head">
                      <div>
                        <div className="arch-kicker">{stage.kicker}</div>
                        <h3 className="arch-title">{stage.title}</h3>
                      </div>
                      <span className="arch-io">{stage.io}</span>
                    </header>
                    <ul className="arch-points">
                      {stage.points.map((point) => (
                        <li key={point}>{point}</li>
                      ))}
                    </ul>
                    <div className="arch-tech">
                      {stage.tech.map((tech) => (
                        <span key={tech}>{tech}</span>
                      ))}
                    </div>
                    <div className="arch-files">{stage.files}</div>
                  </div>
                </article>
              ))}
            </section>

            <aside className="arch-side">
              <section className="arch-panel">
                <h3>Stack</h3>
                <dl className="arch-stack">
                  {STACK.map((item) => (
                    <div key={item.label}>
                      <dt>{item.label}</dt>
                      <dd>{item.value}</dd>
                    </div>
                  ))}
                </dl>
              </section>

              <section className="arch-panel">
                <h3>Configured VAT rules ({RULES.length})</h3>
                <ul className="arch-rules">
                  {RULES.map((rule) => (
                    <li className="arch-rule" key={rule.id}>
                      <span className="arch-rule-head">
                        <span className="arch-rule-id">{rule.id}</span>
                        <SeverityBadge severity={rule.severity} />
                      </span>
                      <span className="arch-rule-note">{rule.note}</span>
                    </li>
                  ))}
                </ul>
              </section>

              <section className="arch-panel arch-panel-warn">
                <h3>Guardrails</h3>
                <ul className="arch-guardrails">
                  {GUARDRAILS.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </section>

              <section className="arch-panel">
                <h3>Failure modes (as implemented)</h3>
                <ul className="arch-failures">
                  {FAILURES.map((item) => (
                    <li className="arch-failure" key={item.code}>
                      <span className="arch-code">{item.code}</span>
                      <span className="arch-rule-note">{item.note}</span>
                    </li>
                  ))}
                </ul>
              </section>

              <p className="arch-footnote">
                Paths are relative to the repository: <span className="mono">backend/app</span>{" "}
                and <span className="mono">frontend</span>. The ruleset is configuration, not
                law - review it against current UAE FTA guidance before production use.
              </p>
            </aside>
          </div>
        </div>
      </div>
    </div>
  );
}
