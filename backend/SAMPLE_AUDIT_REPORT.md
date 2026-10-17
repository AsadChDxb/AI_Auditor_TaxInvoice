# Sample audit report (live run)

This is the Phase 7 sample report. It was produced by a real live run of the API against
the provider configured in `backend/.env` (any OpenAI-compatible endpoint), driven by the
reusable smoke script `scripts/live_smoke.py`.

Nothing here is mocked: the PDF was uploaded over HTTP, the invoice fields were extracted
by the configured model, and `POST /audit/{document_no}` returned the reports below. The
API key appears nowhere in this file, in the reports or in the service logs; TRNs are
masked and no full page text is stored.

## Run metadata

| Item | Value |
| ---- | ----- |
| Provider label | `deepseek` (`LLM_PROVIDER_LABEL`) |
| Model identifier | `deepseek-flash` (`LLM_MODEL`) |
| Base URL | `https://api.deepseek.com/v1` (`LLM_BASE_URL`) |
| Ruleset version | `uae-vat-rules-1.0.0` (`VAT_AUDIT_RULESET_VERSION`) |
| Endpoints | `GET /health` -> `POST /documents/upload` -> `POST /documents/{no}/extract` -> `POST /audit/{no}` |
| Date | 2026-10-16 (UTC) |

Reproduce it with your own key - never paste the key into chat or a commit:

```powershell
cd backend
Copy-Item .env.example .env      # then edit .env and set the real LLM_API_KEY
python scripts/live_smoke.py --report sample_report.json
python -m pytest -q              # 253 tests, all offline, no key needed
```

## Run 1 - compliant standard-rated invoice (`completed`)

```text
1) health          : OK on http://127.0.0.1:8126
2) upload          : 201 extracted DOC-4E7C495E711C4A11B9C3B0F38FC27115
3) extract         : 200 invoice=INV-2024-0001 total=1050.00
4) audit           : 200
status            : completed
human review      : False
confidence        : 94.1
provider / model  : deepseek / deepseek-flash
ruleset version   : uae-vat-rules-1.0.0
audited at        : 2026-10-16T06:12:59.313825Z
summary           : {"total": 5, "passed": 4, "failed": 0, "warnings": 0, "not_applicable": 1, "review_required": 0, "severity_counts": {"high": 3, "medium": 1, "low": 1}, "ruleset_version": "uae-vat-rules-1.0.0", "enabled_rule_count": 6}
  - VAT_RATE_CHECK                 pass             confidence 95.0
  - TAXABLE_AMOUNT_VAT_MISMATCH    pass             confidence 95.0
  - INVOICE_TOTAL_CHECK            pass             confidence 95.0
  - SUPPLIER_TRN_CHECK             pass             confidence 90.0
  - CUSTOMER_TRN_CHECK             not_applicable   confidence 95.0
```

### Rule-level results

| Rule | Status | Severity | Confidence | Human review |
| ---- | ------ | -------- | ---------- | ------------ |
| `VAT_RATE_CHECK` | pass | high | 95.0 | False |
| `TAXABLE_AMOUNT_VAT_MISMATCH` | pass | high | 95.0 | False |
| `INVOICE_TOTAL_CHECK` | pass | high | 95.0 | False |
| `SUPPLIER_TRN_CHECK` | pass | medium | 90.0 | False |
| `CUSTOMER_TRN_CHECK` | not_applicable | low | 95.0 | False |

### Report metadata

| Field | Value |
| ----- | ----- |
| document_no | `DOC-4E7C495E711C4A11B9C3B0F38FC27115` |
| invoice_number | `INV-2024-0001` |
| audit_status | `completed` |
| human_review_required | `False` |
| overall_confidence_percent | `94.1` |
| provider | `deepseek` |
| model | `deepseek-flash` |
| ruleset_version | `uae-vat-rules-1.0.0` |
| audited_at | `2026-10-16T06:12:59.313825Z` |
| findings | `5` |

### Full JSON response

```json
{
  "document_no": "DOC-4E7C495E711C4A11B9C3B0F38FC27115",
  "invoice_number": "INV-2024-0001",
  "audit_status": "completed",
  "overall_assessment": "All five configured rules were evaluated for this standard-rated AED invoice. The reported 5% rate, the taxable-amount-to-VAT calculation (50.00), and the invoice total (1050.00) all reconcile within the AED 0.01 tolerance, and the supplier TRN is present in a valid 15-digit format; the customer TRN check is not applicable because the configured scenario does not require one. No exceptions or uncertainties were identified, so no human review is flagged; verification of TRN registration with the FTA is outside the scope of these format and arithmetic checks. This report is a review aid produced by deterministic checks and a language model. It does not assert fraud or definitive legal non-compliance; check the cited evidence and the current FTA guidance before acting on it.",
  "overall_confidence_percent": 94.1,
  "confidence_explanation": "Overall confidence: severity-weighted mean of 4 finding(s), with failures, warnings and review_required findings weighted double. No capping was necessary.",
  "provider": "deepseek",
  "model": "deepseek-flash",
  "ruleset_version": "uae-vat-rules-1.0.0",
  "audited_at": "2026-10-16T06:12:59.313825Z",
  "findings": [
    {
      "rule_id": "VAT_RATE_CHECK",
      "rule_title": "Expected VAT rate",
      "status": "pass",
      "severity": "high",
      "explanation": "The printed rate matches the expected rate for a standard_rated supply.",
      "expected_value": "5",
      "actual_value": "5.00",
      "page_number": 1,
      "evidence": "VAT treatment: Standard Rated (5%)",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; rate is consistent with the stated standard-rated treatment.",
      "source": "deterministic"
    },
    {
      "rule_id": "TAXABLE_AMOUNT_VAT_MISMATCH",
      "rule_title": "Taxable amount and VAT reconciliation",
      "status": "pass",
      "severity": "high",
      "explanation": "Reported VAT equals 1000.00 x 5% within the AED 0.01 tolerance.",
      "expected_value": "50.00",
      "actual_value": "50.00",
      "page_number": 1,
      "evidence": "Subtotal AED 1,000.00 | VAT AED 50.00",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; VAT amount reconciles with the taxable amount at 5%.",
      "source": "deterministic"
    },
    {
      "rule_id": "INVOICE_TOTAL_CHECK",
      "rule_title": "Invoice total reconciliation",
      "status": "pass",
      "severity": "high",
      "explanation": "Total equals subtotal + VAT (1000.00 + 50.00) within the AED 0.01 tolerance; no separately extracted discounts or charges were applied.",
      "expected_value": "1050.00",
      "actual_value": "1050.00",
      "page_number": 1,
      "evidence": "Subtotal AED 1,000.00 | VAT AED 50.00 | Total AED 1,050.00",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; invoice total reconciles with subtotal plus VAT.",
      "source": "deterministic"
    },
    {
      "rule_id": "SUPPLIER_TRN_CHECK",
      "rule_title": "Supplier TRN presence and format",
      "status": "pass",
      "severity": "medium",
      "explanation": "A 15-digit supplier TRN is present. Format only: registration with the Federal Tax Authority is not verified.",
      "expected_value": "15 digits",
      "actual_value": "100****0003",
      "page_number": 1,
      "evidence": "TRN 100****0003",
      "confidence_percent": 90.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; optionally verify the supplier TRN against the FTA register outside this check.",
      "source": "deterministic"
    },
    {
      "rule_id": "CUSTOMER_TRN_CHECK",
      "rule_title": "Customer TRN presence and format",
      "status": "not_applicable",
      "severity": "low",
      "explanation": "The configured scenario does not require a customer TRN, so a missing one is not treated as a finding.",
      "expected_value": null,
      "actual_value": "100****0003",
      "page_number": 1,
      "evidence": "TRN 100****0003",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; customer TRN is not mandated by the configured scenario.",
      "source": "deterministic"
    }
  ],
  "summary": {
    "total": 5,
    "passed": 4,
    "failed": 0,
    "warnings": 0,
    "not_applicable": 1,
    "review_required": 0,
    "severity_counts": {
      "high": 3,
      "medium": 1,
      "low": 1
    },
    "ruleset_version": "uae-vat-rules-1.0.0",
    "enabled_rule_count": 6
  },
  "warnings": [],
  "review_reasons": [],
  "human_review_required": false,
  "trace": {
    "nodes": [
      "load_context",
      "select_rules",
      "deterministic_checks",
      "agent_review",
      "guardrail_validate",
      "aggregate_report"
    ],
    "iterations": 0,
    "model_calls": 1,
    "tool_calls": [
      "compute_expected_vat",
      "check_invoice_totals",
      "list_rules"
    ],
    "re_extraction_performed": false,
    "rules_evaluated": [
      "VAT_RATE_CHECK",
      "SUPPLIER_TRN_CHECK",
      "CUSTOMER_TRN_CHECK",
      "TAXABLE_AMOUNT_VAT_MISMATCH",
      "INVOICE_TOTAL_CHECK"
    ]
  },
  "message": "Audit completed from the extracted invoice fields and the enabled rules. Read the review reasons and the per-finding evidence before relying on the result."
}
```

## Run 2 - invoice with no stated VAT treatment (`requires_review`)

The same pipeline, driven by a live invoice text that does not state a VAT treatment. The
engine refuses to guess the treatment and asks for a human instead of passing:

```text
1) health          : OK on http://127.0.0.1:8125
2) upload          : 201 extracted DOC-F80BAF17580C4311992E3EE1A455C22E
3) extract         : 200 invoice=INV-2024-0001 total=1050.00
4) audit           : 200
status            : requires_review
human review      : True
confidence        : 70.0
provider / model  : deepseek / deepseek-flash
ruleset version   : uae-vat-rules-1.0.0
summary           : {"total": 5, "passed": 2, "failed": 0, "warnings": 0, "not_applicable": 1, "review_required": 2, "severity_counts": {"high": 2, "medium": 2, "low": 1}, "ruleset_version": "uae-vat-rules-1.0.0", "enabled_rule_count": 6}
  - VAT_RATE_CHECK                 review_required  confidence 60.0
  - INVOICE_TOTAL_CHECK            pass             confidence 95.0
  - SUPPLIER_TRN_CHECK             pass             confidence 90.0
  - VAT_CLASSIFICATION_REVIEW      review_required  confidence 80.0
  - CUSTOMER_TRN_CHECK             not_applicable   confidence 95.0
```

Review reasons recorded by the engine:

```text
VAT treatment is extracted as unknown/ambiguous (only "VAT 5%" shown, no classification statement), so the 5% rate cannot be confirmed as appropriate and the VAT_RATE_CHECK finding is review_required. The invoice date is also missing from extraction, which a reviewer should confirm against the sourc...
VAT_RATE_CHECK: The VAT treatment is unknown, so no rate could be checked.
VAT_CLASSIFICATION_REVIEW: The VAT treatment is unknown or ambiguous.
The missing fields were not found by a second extraction pass.
Missing or unreadable fields: invoice_date.
```

### Rule-level results

| Rule | Status | Severity | Confidence | Human review |
| ---- | ------ | -------- | ---------- | ------------ |
| `VAT_RATE_CHECK` | review_required | high | 60.0 | True |
| `INVOICE_TOTAL_CHECK` | pass | high | 95.0 | False |
| `SUPPLIER_TRN_CHECK` | pass | medium | 90.0 | False |
| `VAT_CLASSIFICATION_REVIEW` | review_required | medium | 80.0 | True |
| `CUSTOMER_TRN_CHECK` | not_applicable | low | 95.0 | False |

### Full JSON response

```json
{
  "document_no": "DOC-F80BAF17580C4311992E3EE1A455C22E",
  "invoice_number": "INV-2024-0001",
  "audit_status": "requires_review",
  "overall_assessment": "The invoice from Al Noor Trading LLC to Gulf Retail FZE for AED 1,000.00 is arithmetically consistent: 5% VAT of AED 50.00 gives the printed total of AED 1,050.00 within tolerance, and the supplier TRN is present in 15-digit format while no customer TRN is required for this scenario. The outstanding item is the VAT treatment, which extraction records as \"unknown\" with only \"VAT 5%\" as evidence, so the rate check and the classification review are both routed to a human reviewer; the missing invoice date from extraction and the 60% confidence on the rate check reinforce that routing. This report is a review aid produced by deterministic checks and a language model. It does not assert fraud or definitive legal non-compliance; check the cited evidence and the current FTA guidance before acting on it.",
  "overall_confidence_percent": 70.0,
  "confidence_explanation": "Overall confidence: severity-weighted mean of 4 finding(s), with failures, warnings and review_required findings weighted double. Capping applied: below the 80% review threshold because 1 finding(s) are below it (lowest 60%); below the review threshold because 2 finding(s) are flagged for human review; at 70% because key evidence is missing",
  "provider": "deepseek",
  "model": "deepseek-flash",
  "ruleset_version": "uae-vat-rules-1.0.0",
  "audited_at": "2026-10-16T06:12:24.076014Z",
  "findings": [
    {
      "rule_id": "VAT_RATE_CHECK",
      "rule_title": "Expected VAT rate",
      "status": "review_required",
      "severity": "high",
      "explanation": "The VAT treatment could not be established, so no expected rate was assumed: a 5% rate is never applied to an unknown treatment.",
      "expected_value": null,
      "actual_value": "5.00",
      "page_number": 1,
      "evidence": "vat_rate_percent: VAT 5% | vat_amount: VAT 5% AED 50.00",
      "confidence_percent": 60.0,
      "human_review_required": true,
      "human_review_reason": "The VAT treatment is unknown, so no rate could be checked.",
      "recommended_action": "Confirm the VAT treatment from the invoice and its supporting evidence.",
      "source": "deterministic"
    },
    {
      "rule_id": "INVOICE_TOTAL_CHECK",
      "rule_title": "Invoice total reconciliation",
      "status": "pass",
      "severity": "high",
      "explanation": "Total equals subtotal + VAT (1000.00 + 50.00) within the AED 0.01 tolerance; no separately extracted discounts or charges were applied.",
      "expected_value": "1050.00",
      "actual_value": "1050.00",
      "page_number": 1,
      "evidence": "subtotal_excluding_vat: Subtotal AED 1,000.00 | vat_amount: VAT 5% AED 50.00 | total_including_vat: Total AED 1,050.00",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; totals reconcile within tolerance.",
      "source": "deterministic"
    },
    {
      "rule_id": "SUPPLIER_TRN_CHECK",
      "rule_title": "Supplier TRN presence and format",
      "status": "pass",
      "severity": "medium",
      "explanation": "A 15-digit supplier TRN is present. Format only: registration with the Federal Tax Authority is not verified.",
      "expected_value": "15 digits",
      "actual_value": "100****0003",
      "page_number": 1,
      "evidence": "supplier_trn: TRN 100****0003",
      "confidence_percent": 90.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action needed for format; optionally verify the supplier TRN against FTA records if the audit scope requires registration confirmation.",
      "source": "deterministic"
    },
    {
      "rule_id": "VAT_CLASSIFICATION_REVIEW",
      "rule_title": "VAT treatment review",
      "status": "review_required",
      "severity": "medium",
      "explanation": "The invoice does not state a VAT treatment that could be established, so the treatment is ambiguous. No rate or treatment was assumed.",
      "expected_value": null,
      "actual_value": "unknown",
      "page_number": 1,
      "evidence": "vat_classification: VAT 5%",
      "confidence_percent": 80.0,
      "human_review_required": true,
      "human_review_reason": "The VAT treatment is unknown or ambiguous.",
      "recommended_action": "Confirm the treatment from the invoice wording and the nature of the supply.",
      "source": "deterministic"
    },
    {
      "rule_id": "CUSTOMER_TRN_CHECK",
      "rule_title": "Customer TRN presence and format",
      "status": "not_applicable",
      "severity": "low",
      "explanation": "The configured scenario does not require a customer TRN, so a missing one is not treated as a finding.",
      "expected_value": null,
      "actual_value": "100****0003",
      "page_number": 1,
      "evidence": "customer_trn: TRN 100****0003",
      "confidence_percent": 95.0,
      "human_review_required": false,
      "human_review_reason": null,
      "recommended_action": "No action required; no customer TRN is required for this scenario.",
      "source": "deterministic"
    }
  ],
  "summary": {
    "total": 5,
    "passed": 2,
    "failed": 0,
    "warnings": 0,
    "not_applicable": 1,
    "review_required": 2,
    "severity_counts": {
      "high": 2,
      "medium": 2,
      "low": 1
    },
    "ruleset_version": "uae-vat-rules-1.0.0",
    "enabled_rule_count": 6
  },
  "warnings": [
    "A second extraction pass found no additional values for the missing fields."
  ],
  "review_reasons": [
    "VAT treatment is extracted as unknown/ambiguous (only \"VAT 5%\" shown, no classification statement), so the 5% rate cannot be confirmed as appropriate and the VAT_RATE_CHECK finding is review_required. The invoice date is also missing from extraction, which a reviewer should confirm against the sourc...",
    "VAT_RATE_CHECK: The VAT treatment is unknown, so no rate could be checked.",
    "VAT_CLASSIFICATION_REVIEW: The VAT treatment is unknown or ambiguous.",
    "The missing fields were not found by a second extraction pass.",
    "Missing or unreadable fields: invoice_date."
  ],
  "human_review_required": true,
  "trace": {
    "nodes": [
      "load_context",
      "select_rules",
      "deterministic_checks",
      "agent_review",
      "guardrail_validate",
      "re_extract_fields",
      "select_rules",
      "deterministic_checks",
      "agent_review",
      "guardrail_validate",
      "re_extract_fields",
      "select_rules",
      "deterministic_checks",
      "agent_review",
      "guardrail_validate",
      "human_review",
      "aggregate_report"
    ],
    "iterations": 2,
    "model_calls": 5,
    "tool_calls": [
      "compute_expected_vat",
      "check_invoice_totals",
      "compute_expected_vat",
      "check_invoice_totals",
      "get_invoice_field",
      "get_invoice_field",
      "flag_human_review",
      "compute_expected_vat",
      "check_invoice_totals",
      "get_invoice_field",
      "get_invoice_field",
      "get_invoice_field",
      "flag_human_review"
    ],
    "re_extraction_performed": true,
    "rules_evaluated": [
      "VAT_RATE_CHECK",
      "SUPPLIER_TRN_CHECK",
      "CUSTOMER_TRN_CHECK",
      "INVOICE_TOTAL_CHECK",
      "VAT_CLASSIFICATION_REVIEW"
    ]
  },
  "message": "Audit completed from the extracted invoice fields and the enabled rules. Read the review reasons and the per-finding evidence before relying on the result."
}
```

## How to read these reports

* `audit_status` is `completed`, `completed_with_warnings`, `requires_review` or
  `cannot_audit`; a report with a review point never reads as a confident pass.
* Monetary arithmetic is recomputed in Python with `Decimal` (tolerance
  `VAT_AUDIT_MONETARY_TOLERANCE=0.01`) and always wins a conflict with the model.
* A failing rule is an audit finding, **not** proof of tax non-compliance, and passing
  every implemented check is **not** a VAT compliance certification.
* No response contains the API key, full page text or unmasked TRNs.
