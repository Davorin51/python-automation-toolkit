# Changelog

## Unreleased

- Validation errors now include an `invalid_columns` field naming the affected
  columns, blank cells are reported as `Missing <column>`, invalid dates name the
  configured format, and duplicates point to the file and row of the first
  occurrence.
- Skip completely blank rows and blank header columns during ingestion while
  keeping `source_row` aligned with the original file; trim whitespace around
  dates and numbers before validation.
- Ingestion errors name the file and the headers involved in ambiguous or missing
  column problems; a directory passed as input is rejected with a clear message.
- Write native Excel dates in `validation_errors.json` as ISO text instead of
  epoch milliseconds.
- Add a `test` extra (pytest, pytest-cov, pypdf) that the `dev` extra builds on,
  and fail test collection with a clear install hint when `pypdf` is missing.
- Document that a plain `pip install -e .` does not include the test dependencies.

## 1.0.0 - 2026-10-12

- Add CSV/XLSX ingestion, header aliases and explicit column mapping.
- Add row-level validation with quarantine, skip and reject-file policies.
- Add integer-cent revenue calculations and explicit order-count semantics.
- Add category/product analytics, calendar trends and period comparisons.
- Add styled Excel workbooks, three-page PDF reports and four PNG charts.
- Add CLI and optional CustomTkinter desktop interface sharing one pipeline.
- Publish each report run through staging, with a manifest and source checksums.
- Add synthetic fixtures, downloadable demo reports and a benchmark runner.
- Add automated tests, an 80% coverage gate and GitHub Actions checks.
