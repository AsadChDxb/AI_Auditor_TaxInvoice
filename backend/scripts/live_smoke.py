"""Live smoke test for the UAE VAT Audit MVP.

Starts the API with uvicorn, runs the documented workflow over real HTTP (upload a PDF,
extract the invoice fields, audit the document), prints a compact summary — including the
Phase 9 extraction result (pages with text, recovered tables, whether the OCR fallback
ran and whether the quality check passed) — and optionally writes the full audit report
JSON.

Usage, from the ``backend`` directory:

    python scripts/live_smoke.py
    python scripts/live_smoke.py --port 8123 --report ..\\sample_report.json

The provider is whatever ``.env`` configures (any OpenAI-compatible endpoint). The API
key is never printed: only the provider label, the model identifier and the ruleset
version are shown, and the report itself never contains the key or full page text.
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import fitz  # PyMuPDF
import httpx

BACKEND_DIR = Path(__file__).resolve().parents[1]
HEALTH_TIMEOUT_SECONDS = 40.0
REQUEST_TIMEOUT_SECONDS = 120.0
INVOICE_PAGE = (
    "TAX INVOICE INV-2024-0001\n"
    "Invoice date: 03/05/2024\n"
    "Supplier: Al Noor Trading LLC TRN 100123456700003\n"
    "Customer: Gulf Retail FZE TRN 100987654300003\n"
    "VAT treatment: Standard Rated (5%)\n"
    "Subtotal AED 1,000.00 | VAT AED 50.00 | Total AED 1,050.00\n"
)


def build_invoice_pdf() -> bytes:
    """A small, text-based invoice PDF, built in memory."""

    document = fitz.open()
    try:
        page = document.new_page()
        page.insert_text((72, 96), INVOICE_PAGE, fontsize=11)
        return document.tobytes()
    finally:
        document.close()


def wait_for_health(base_url: str) -> bool:
    """Poll ``GET /health`` until the API answers or the timeout expires."""

    deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.4)
    return False


def summarise(report: dict) -> str:
    """A compact, secret-free summary of an audit report."""

    lines = [
        f"status            : {report['audit_status']}",
        f"human review      : {report['human_review_required']}",
        f"confidence        : {report['overall_confidence_percent']}",
        f"provider / model  : {report['provider']} / {report['model']}",
        f"ruleset version   : {report['ruleset_version']}",
        f"audited at        : {report['audited_at']}",
        f"summary           : {json.dumps(report['summary'])}",
    ]
    for finding in report["findings"]:
        lines.append(
            f"  - {finding['rule_id']:<30} {finding['status']:<16} "
            f"confidence {finding['confidence_percent']}"
        )
    if report["warnings"]:
        lines.append("warnings          :")
        lines.extend(f"  - {warning}" for warning in report["warnings"])
    if report["review_reasons"]:
        lines.append("review reasons    :")
        lines.extend(f"  - {reason}" for reason in report["review_reasons"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the live smoke test."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--skip-extraction-check", action="store_true")
    args = parser.parse_args(argv)
    base_url = f"http://127.0.0.1:{args.port}"

    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(args.port),
            "--log-level",
            "warning",
        ],
        cwd=BACKEND_DIR,
    )
    try:
        if not wait_for_health(base_url):
            print("FAIL: the API did not become healthy")
            return 1
        print(f"1) health          : OK on {base_url}")

        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            upload = client.post(
                f"{base_url}/documents/upload",
                files={"file": ("invoice.pdf", build_invoice_pdf(), "application/pdf")},
            )
            upload.raise_for_status()
            upload_body = upload.json()
            document_no = upload_body["document_no"]
            quality = upload_body.get("quality") or {}
            print(
                f"2) upload          : {upload.status_code} "
                f"{upload_body['extraction_status']} {document_no}"
            )
            print(
                f"   extraction      : pdfplumber pages="
                f"{upload_body['pages_with_text']}/{upload_body['page_count']} "
                f"tables={upload_body.get('table_count', 0)} "
                f"used_ocr={upload_body.get('used_ocr', False)} "
                f"quality={'passed' if quality.get('passed') else 'flagged'}"
            )
            for issue in quality.get("issues", []):
                print(f"   quality issue   : {issue}")

            extraction = client.post(f"{base_url}/documents/{document_no}/extract")
            if extraction.status_code != 200:
                print(f"3) extract         : FAILED {extraction.status_code}")
                print(f"   detail          : {extraction.json().get('detail')}")
                print(
                    "   hint            : check LLM_API_KEY / LLM_MODEL / LLM_BASE_URL "
                    "in backend/.env, then re-run"
                )
                return 2
            invoice = extraction.json()["invoice"]
            print(
                f"3) extract         : {extraction.status_code} "
                f"invoice={invoice['invoice_number']} total={invoice['total_including_vat']}"
            )

            audit = client.post(f"{base_url}/audit/{document_no}")
            if audit.status_code != 200:
                print(f"4) audit           : FAILED {audit.status_code}")
                print(f"   detail          : {audit.json().get('detail')}")
                return 3
            report = audit.json()
            print("4) audit           : 200")
            print(summarise(report))

            if args.report is not None:
                args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(f"   report written to {args.report}")
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            server.kill()


if __name__ == "__main__":
    raise SystemExit(main())
