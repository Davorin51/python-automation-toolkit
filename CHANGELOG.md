# Changelog

## Unreleased

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
