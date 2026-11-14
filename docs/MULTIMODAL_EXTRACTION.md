# Multimodal extraction v1

Albert's `albert.extraction/v1` contract accepts PNG, JPEG, and scanned PDF artifacts.
The bundled self-hosted adapter invokes Tesseract 5; PDF rendering uses Poppler's
`pdfinfo` and `pdftoppm` executables.

Every segment carries artifact digest and ID, one-based page, pixel region,
extractor/version, confidence, sensitivity, and an explicit `untrusted` label.
`recall_text()` wraps OCR in an `untrusted-ocr` data boundary so image text cannot
become policy or instructions.

## Safety and failure behavior

- Magic-byte MIME detection must match any declared MIME. Mismatches are quarantined.
- File bytes, pages, rendered pixels, elapsed time, and output text are bounded by
  `ExtractionLimits` (25 MiB, 100 pages, 40 million pixels/page, 120 seconds, and
  2 million output characters by default).
- Unsupported, corrupt, encrypted, cancelled, provider-unavailable, and
  resource-limited results are terminal states with no segments.
- `ExtractionIndex.publish()` accepts only complete, internally tenant-consistent
  results and atomically replaces
  one artifact. Stable IDs make unchanged retries idempotent.
- Publication and deletion use organization plus artifact keys. Artifact deletion
  invalidates all derived segments. Database adapters must preserve these keys and
  replacement semantics in one transaction.

Providers implement `OCRAdapter.extract` and declare immutable `name` and `version`.
Extensions must preserve v1 limits, cancellation, MIME checks, tenant scope, and the
untrusted recall boundary.
