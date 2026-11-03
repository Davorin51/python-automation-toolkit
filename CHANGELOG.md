# Changelog

## Unreleased

- Add incremental CSV processing: the `csv_chunk_size` setting and `--chunk-size`
  CLI flag read CSV inputs in fixed-size chunks through the new
  `iter_csv_chunks` generator, producing the same frame, `source_row` numbers and
  validation results as a whole-file read; invalid sizes are configuration errors
  and parse or decoding failures in any chunk are reported as ingestion errors.
- Add checkpoint and resume for incremental CSV loading: `csv_checkpoint_dir`
  and `--checkpoint-dir` persist each processed chunk and a checksummed manifest
  so an interrupted run resumes after the last completed chunk with identical
  results; unreadable, altered or stale checkpoints are discarded with a warning
  and the file is processed from the start. Requires `csv_chunk_size`.
- Normalize, drop extra columns and skip blank rows per chunk during incremental
  CSV loading, so peak memory is bounded by one raw chunk plus the final table of
  required fields instead of the whole raw file.
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
