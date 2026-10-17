"use client";

/**
 * Step 1: upload an invoice PDF.
 *
 * Sends the file to `POST /documents/upload` (multipart field `file`). Content
 * validation is left to the API, which checks the PDF by content, so the client only
 * pre-filters obvious mismatches and always shows the API's own message on failure.
 */

import { useRef, useState } from "react";

import { ExtractionStatusBadge } from "@/components/Badges";
import { formatBytes, formatDateTime, orDash } from "@/lib/format";
import type { DocumentSummary } from "@/lib/types";

export function UploadPanel({
  busy,
  error,
  result,
  onUpload,
}: {
  busy: boolean;
  error: string | null;
  result: DocumentSummary | null;
  onUpload: (file: File) => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [selected, setSelected] = useState<File | null>(null);
  const [localError, setLocalError] = useState<string | null>(null);

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
      </div>
    </section>
  );
}
