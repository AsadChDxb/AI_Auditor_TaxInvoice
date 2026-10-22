"use client";

/**
 * Step 1: upload an invoice PDF.
 *
 * Sends the file to `POST /documents/upload` (multipart field `file`). Content
 * validation is left to the API, which checks the PDF by content, so the client only
 * pre-filters obvious mismatches and always shows the API's own message on failure.
 *
 * The same card offers the API's bundled sample invoices (`GET /samples`): a reviewer can
 * open one in a new tab, or load it and let it run through exactly the `onUpload` call a
 * picked file uses. The samples exist only to make the screen testable; the upload
 * contract itself is unchanged.
 */

import { useRef, useState } from "react";

import { Badge, ExtractionStatusBadge } from "@/components/Badges";
import { fetchSampleInvoice, sampleInvoiceUrl } from "@/lib/api";
import { formatBytes, formatDateTime, orDash, type BadgeTone } from "@/lib/format";
import type { DocumentSummary, SampleInvoice } from "@/lib/types";

/** Badge tone and label describing what a bundled sample is built to prove. */
function sampleOutcomeBadge(outcome: SampleInvoice["expected_outcome"]): {
  tone: BadgeTone;
  label: string;
} {
  if (outcome === "pass") {
    return { tone: "good", label: "expected: pass" };
  }
  if (outcome === "fail") {
    return { tone: "danger", label: "expected: fail" };
  }
  return { tone: "muted", label: "not labelled" };
}

export function UploadPanel({
  busy,
  error,
  result,
  samples,
  samplesLoading,
  samplesError,
  onUpload,
}: {
  busy: boolean;
  error: string | null;
  result: DocumentSummary | null;
  samples: SampleInvoice[];
  samplesLoading: boolean;
  samplesError: string | null;
  onUpload: (file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [selected, setSelected] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);
  const [loadingSample, setLoadingSample] = useState<string | null>(null);
  const [pickedSample, setPickedSample] = useState<string | null>(null);

  function choose(file: File | undefined) {
    if (!file) {
      return;
    }
    const looksLikePdf =
      file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
    if (!looksLikePdf) {
      setSelected(null);
      setLocalError(`"${file.name}" is not a PDF. Upload the invoice as a PDF file.`);
      return;
    }
    setLocalError(null);
    setSelected(file);
  }

  /**
   * Download a sample invoice and run it through the normal upload path.
   *
   * The fetched blob becomes a real `File`, so the request a sample produces is byte for
   * byte the same as dragging the PDF onto the dropzone.
   */
  async function useSample(sample: SampleInvoice) {
    setLoadingSample(sample.file_name);
    setLocalError(null);
    try {
      const file = await fetchSampleInvoice(sample);
      choose(file);
      setPickedSample(sample.file_name);
      onUpload(file);
    } catch (caught) {
      setLocalError(
        caught instanceof Error ? caught.message : "Could not load the sample invoice.",
      );
    } finally {
      setLoadingSample(null);
    }
  }

  return (
    <section className="card">
      <header className="card-head">
        <div>
          <h2 className="card-title">Step 1 · Upload document</h2>
          <p className="card-sub">
            One invoice PDF. Text and structured fields are extracted in a single
            request, and the result stays in this tab.
          </p>
        </div>
        <span className="card-sub">Max 10 MB</span>
      </header>

      <div className="card-body">
        <div
          className="dropzone"
          data-active={dragging}
          onClick={() => inputRef.current?.click()}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(event) => {
            event.preventDefault();
            setDragging(false);
            choose(event.dataTransfer.files?.[0]);
          }}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              inputRef.current?.click();
            }
          }}
        >
          <div className="dropzone-icon" aria-hidden="true">
            📄
          </div>
          <div className="dropzone-title">
            Drop an invoice PDF here or click to browse
          </div>
          <div className="dropzone-hint">
            The API validates the file by content, so a renamed file is rejected.
          </div>
        </div>

        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          hidden
          onChange={(event) => {
            choose(event.target.files?.[0]);
            event.target.value = "";
          }}
        />

        <div className="row-wrap">
          {selected ? (
            <span className="file-chip">
              <strong>{selected.name}</strong>
              <span className="muted">{formatBytes(selected.size)}</span>
            </span>
          ) : (
            <span className="card-sub">No file selected.</span>
          )}
          <span style={{ flex: 1 }} />
          <button
            type="button"
            className="btn btn-primary"
            disabled={!selected || busy}
            onClick={() => {
              if (selected) {
                onUpload(selected);
              }
            }}
          >
            {busy ? <span className="spinner" aria-hidden="true" /> : null}
            {busy ? "Extracting…" : "Upload & extract"}
          </button>
        </div>

        {localError ? <div className="alert alert-error">{localError}</div> : null}
        {error ? <div className="alert alert-error">{error}</div> : null}

        <div className="sample-box">
          <div className="sample-head">
            <span className="sample-heading">Sample invoices</span>
            {samples.length > 0 ? (
              <Badge tone="info">{samples.length} available</Badge>
            ) : null}
          </div>
          <p className="card-sub">
            Generated test documents: two valid and three with a known defect. Use one to
            run the whole workflow without a file at hand - the PDF is uploaded exactly
            like a file you pick yourself.
          </p>

          {samplesLoading ? <p className="card-sub">Loading samples…</p> : null}
          {samplesError ? (
            <p className="card-sub">Samples unavailable: {samplesError}</p>
          ) : null}
          {!samplesLoading && !samplesError && samples.length === 0 ? (
            <p className="card-sub">No sample invoices are available on this deployment.</p>
          ) : null}

          {samples.length > 0 ? (
            <ul className="sample-list">
              {samples.map((sample) => {
                const outcome = sampleOutcomeBadge(sample.expected_outcome);
                const loading = loadingSample === sample.file_name;
                return (
                  <li
                    key={sample.file_name}
                    className="sample-row"
                    data-picked={pickedSample === sample.file_name}
                  >
                    <span className="sample-copy">
                      <span className="sample-name" title={sample.file_name}>
                        {sample.title}
                      </span>
                      <span className="card-sub">
                        {sample.supplier_name ?? sample.file_name} ·{" "}
                        {formatBytes(sample.size_bytes)}
                      </span>
                    </span>
                    <span className="sample-actions">
                      <Badge tone={outcome.tone}>{outcome.label}</Badge>
                      <a
                        className="btn btn-ghost btn-sm"
                        href={sampleInvoiceUrl(sample)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        View PDF
                      </a>
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={busy || loadingSample !== null}
                        onClick={() => {
                          void useSample(sample);
                        }}
                      >
                        {loading ? <span className="spinner" aria-hidden="true" /> : null}
                        {loading ? "Loading…" : "Use sample"}
                      </button>
                    </span>
                  </li>
                );
              })}
            </ul>
          ) : null}
        </div>
      </div>
    </section>
  );
}
