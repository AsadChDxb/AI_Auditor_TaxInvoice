# Sample tax invoices (generated)

Five UAE tax-invoice PDFs for manual testing: two fully compliant and three carrying a
deliberate defect, so the audit result is known before the file is uploaded.

They are **text** PDFs (real fonts plus ruled line-item tables), not scans, so
`pdfplumber` reads them without the OCR fallback. Every TRN, bank account and company is
fictitious: these are test documents, not tax records.

Regenerate them with:

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\generate_sample_invoices.py
```

The API serves this folder read-only, so the upload screen can offer the samples:
`GET /samples` lists them (with the expected outcome taken from the file name) and
`GET /samples/{file_name}` streams one PDF. The UI's **Step 1** card therefore has a
"Sample invoices" list with *View PDF* and *Use sample* buttons; *Use sample* downloads
the PDF and sends it through exactly the same upload request as a picked file.

## What each file proves

| # | File | Supplier to Customer | Invoice No. | Date | Taxable | VAT | Total | Expected audit |
|---|------|----------------------|-------------|------|---------|-----|-------|----------------|
| 1 | `01_valid_goods_gulf_star_GST-2026-0142.pdf` | Gulf Star Trading LLC to Al Noor Contracting LLC | GST-2026-0142 | 12/09/2026 | 12,850.00 | 642.50 (5%) | 13,492.50 | every rule passes |
| 2 | `02_valid_services_shiny_home_SH02739.pdf` | Shiny Home Cleaning Services to The Carlisle Contracting LLC | SH02739 | 30/09/2026 | 5,788.00 | 289.40 (5%) | 6,077.40 | every rule passes |
| 3 | `03_invalid_vat_amount_al_madina_AMD-2026-5510.pdf` | Al Madina Auto Spare Parts LLC to Desert Rose Logistics LLC | AMD-2026-5510 | 18/08/2026 | 11,400.00 | **1,026.00** (9% amount, 5% label) | 12,426.00 | `TAXABLE_AMOUNT_VAT_MISMATCH` fails |
| 4 | `04_invalid_missing_trn_bright_future_BFT-2026-0093.pdf` | Bright Future Technical Services to Marina Heights Facilities Management LLC | BFT-2026-0093 | 05/09/2026 | 5,510.00 | 275.50 (5%) | 5,785.50 | `SUPPLIER_TRN_CHECK` fails |
| 5 | `05_invalid_rate_and_trn_horizon_digital_HDM-2026-1187.pdf` | Horizon Digital Marketing FZE to Palm Jumeirah Hospitality LLC | HDM-2026-1187 | 08/09/2026 | 9,900.00 | 990.00 (**10%**) | 10,890.00 | `VAT_RATE_CHECK`, `TAXABLE_AMOUNT_VAT_MISMATCH` and `SUPPLIER_TRN_CHECK` fail |

The expectations above were verified twice: `extract_pdf_text()` reads each file without
OCR (about 1,800-1,980 characters, one 9-column table, quality report `passed`), and
`run_deterministic_checks()` returns exactly the listed statuses when the printed values
are supplied as `InvoiceData`.

## The three defects in detail

- **#3 - VAT computed at the wrong rate.** Every line shows the correct 5% VAT (lines add
  up to 570.00) but the summary prints 1,026.00, which is 9% of 11,400.00, and the grand
  total is consistent with that wrong amount. `INVOICE_TOTAL_CHECK` therefore passes
  while `TAXABLE_AMOUNT_VAT_MISMATCH` fails (expected 570.00, actual 1,026.00, tolerance
  0.01 AED).
- **#4 - supplier TRN missing.** The supplier block prints `TRN: Not Registered` and VAT
  is still charged. The arithmetic is correct, so only `SUPPLIER_TRN_CHECK` fails
  (`15-digit TRN vs missing`).
- **#5 - wrong rate and malformed TRN.** VAT is charged at 10% (990.00 instead of 495.00)
  and the supplier TRN has 8 digits (`10045678`) instead of 15, so the rate check, the
  taxable/VAT reconciliation and the TRN check all fail.

## Notes for whoever uses them

- Dates are printed as `dd/mm/yyyy` (UAE convention) on purpose, so the files also
  exercise the extractor's date normalisation to `YYYY-MM-DD`.
- In sample #4 the TRN is absent as a *value*, but the words "TRN: Not Registered" appear
  on the page, so `ExtractionQuality.required_fields_found` still lists `supplier_trn`:
  that check only proves a field is *mentioned*. The deterministic rule works on the
  extracted value and reports `missing`, which is the outcome this sample is for.
- Sample #2 uses per-line discounts, so `Taxable Amount` is already net of the discount
  (gross 6,050.00 - 262.00 = 5,788.00) and the total still reconciles as taxable + VAT.
- Each file is a single A4 page with a 9-column ruled grid, so `pdfplumber` recovers
  `(rows x 9)` cells including the header row (`#`, `DESCRIPTION`, `QTY`, `UNIT PRICE`,
  `DISC.`, `TAXABLE`, `VAT %`, `VAT`, `TOTAL`).
