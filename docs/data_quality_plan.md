# Data quality reporting: analysis and implementation plan

Status: proposal, nothing implemented yet. This document records what the current
code does, where the extension points are, and the changes I would make to add a
backward compatible data quality reporting option with validation statistics,
error summaries and a JSON export. Decisions that need your input are collected
at the end.

## 1. What exists today

### Ingestion (`src/automation_toolkit/ingestion/`)

- `load_files(paths, config) -> DataFrame` is the single entry point. It resolves
  paths, rejects duplicates/directories/unknown suffixes, and per file either
  reads XLSX in one piece or iterates CSV chunks from `iter_csv_chunks`.
- Each chunk is normalized (`normalize_columns`: mapping, aliases, required
  columns, ambiguity check), gets `source_file` and `source_row` provenance, and
  blank rows are dropped. The blank-row count per file is only **logged**
  (`Skipped N blank rows in <file>`); it is not returned to the caller.
- `CsvCheckpoint` (`checkpoint.py`) restores already processed chunks when
  `csv_checkpoint_dir` is set, exposes `records`, `blank` and `pieces`, and is
  deleted on `complete()`. The number of chunks restored vs. freshly read is also
  only visible in logs.
- The function returns one concatenated frame with the columns
  `date, product, category, quantity, unit_price[, order_id], source_file, source_row`.

### Validation (`src/automation_toolkit/processing/validation.py`)

- `validate(frame, config) -> ValidationResult` runs vectorized checks via an
  inner `flag(mask, message, column)` helper that appends a free-text reason and
  the column name per row. Checks: missing date/quantity/unit_price/product/
  category/order_id, invalid date (message embeds the configured format),
  quantity rule, price rule, duplicate line (message embeds first occurrence).
- `ValidationResult` holds `transactions`, `errors` (source_file, source_row,
  [raw business columns under `quarantine`], `reason`, `invalid_columns`),
  `input_rows`, `invalid_rows`, `excluded_rows`, `rejected_files`.
- There is **no machine-readable check identifier**. Grouping errors today would
  mean parsing `reason` strings whose text varies with `date_format` and the
  duplicate's first-occurrence location.
- `tests/test_validation.py::test_skip_keeps_only_diagnostics` pins the exact
  column list of `errors`, so adding columns to `errors` is a visible change.

### Pipeline and CLI (`pipeline.py`, `cli.py`, `gui/app.py`)

- `run_pipeline(inputs, output, config, report_format)` validates, raises
  `ValidationError` when no transactions remain, runs analytics, writes reports
  into a staging folder, writes `validation_errors.json` and `manifest.json`,
  then renames staging to `run-<ts>-<id>`. `PipelineResult(output_dir,
  validation, analytics)`.
- The manifest already carries `input_rows`, `accepted_rows`, `invalid_rows`,
  `excluded_rows`, `rejected_files`, `policy`, `sources` (sha256) and `outputs`.
- CLI: `--input --output --format {all,excel,pdf,charts} --config --chunk-size
  --checkpoint-dir --verbose`; prints the run folder and one summary line. The
  GUI offers the same four formats and calls the same pipeline.
- Excel has a Summary sheet with the four row counts and a Validation Errors
  sheet; the PDF has a short "Data quality" paragraph with the same counts.

### Tests

172 tests pass. Relevant pinned behaviours: exact Excel sheet names
(`test_reporting.py`), exact `errors` columns under `skip`, manifest keys and
values (`test_pipeline.py`), whole-file vs. chunked vs. resumed equivalence
(`test_incremental_csv.py`, `test_checkpoint.py`), control totals on the three
fixtures (`test_demo.py`).

## 2. Proposed design

Add a **data quality report** that is computed from the existing
`ValidationResult` plus a small set of ingestion statistics, exported as
`data_quality.json` in every run folder, and available as a validation-only
pipeline mode. The computation lives in `processing` (it is a calculation), the
writer in `reporting` (it is an output adapter), following the existing layering.

### 2.1 Stable check codes in validation

Give every check a stable code and record failures per row in a tidy frame,
without touching the existing `errors` frame or its JSON:

| code | column(s) | existing message |
| --- | --- | --- |
| `missing_value` | date, quantity, unit_price, product, category, order_id | `Missing <column>` |
| `invalid_date` | date | `Invalid date (expected format ...)` |
| `invalid_quantity` | quantity | `Quantity must be a positive integer <= 1000000` |
| `invalid_price` | unit_price | `Price must be non-negative with at most two decimal places` |
| `duplicate_line` | all duplicate-key columns | `Duplicate transaction line (first occurrence: ...)` |

Implementation: `flag()` gains a `code` argument and appends
`(index, code, column)` to a list; `validate()` returns it as a new
`ValidationResult.checks` DataFrame with columns
`source_file, source_row, code, column` (one row per failed check, so a row with
three problems appears three times). The field is appended last with a default,
so positional construction and every existing attribute keep working.
`errors`, `reason` and `invalid_columns` are byte-for-byte unchanged.

### 2.2 Ingestion statistics

Add an `IngestionStats` dataclass (per file: `source_file`, `format`,
`records_read`, `blank_rows`, `data_rows`, `chunks`, `restored_from_checkpoint`
records/chunks, `has_order_id`) and a new function
`load_files_with_stats(paths, config) -> tuple[DataFrame, IngestionStats]`.
`load_files` keeps its signature and becomes a thin wrapper that drops the stats.
This finally exposes the blank-row and checkpoint-resume numbers that are
currently log-only. No change to the checkpoint on-disk format or version.

### 2.3 Quality report model (`processing/quality.py`)

`build_quality_report(validation, ingestion, config, generated_at) -> QualityReport`
with `to_dict()`. Pure function, deterministic ordering, percentages rounded to
two decimals, integer counts. Proposed JSON layout:

```json
{
  "schema_version": 1,
  "toolkit_version": "1.0.0",
  "generated_at": "2026-11-08T10:00:00+00:00",
  "policy": "quarantine",
  "settings": {"date_format": "%Y-%m-%d", "csv_chunk_size": null, "checkpointing": false},
  "summary": {
    "files": 1, "rejected_files": 0,
    "records_read": 357, "blank_rows_skipped": 0, "input_rows": 357,
    "accepted_rows": 350, "invalid_rows": 7, "excluded_rows": 7,
    "accepted_percent": 98.04, "invalid_percent": 1.96, "excluded_percent": 1.96,
    "total_issues": 7, "rows_with_multiple_issues": 0
  },
  "dimensions": {
    "completeness": {"rows": 2, "percent": 0.56},
    "validity":     {"rows": 3, "percent": 0.84},
    "uniqueness":   {"rows": 2, "percent": 0.56}
  },
  "by_check": [
    {"code": "invalid_date", "column": "date", "rows": 1, "percent_of_input": 0.28,
     "message": "Invalid date (expected format %Y-%m-%d)",
     "examples": [{"source_file": "sample_sales_dirty.csv", "source_row": 352}]}
  ],
  "by_column": [
    {"column": "date", "missing": 0, "invalid": 1, "issue_rows": 1, "complete_percent": 100.0}
  ],
  "by_file": [
    {"source_file": "sample_sales_dirty.csv", "format": "csv", "records_read": 357,
     "blank_rows": 0, "input_rows": 357, "accepted": 350, "invalid": 7, "excluded": 7,
     "rejected": false, "chunks": null, "resumed_from_checkpoint": false, "sha256": "..."}
  ],
  "top_reasons": [{"reason": "Duplicate transaction line ...", "rows": 1}]
}
```

Notes on semantics:

- `by_check` and `by_column` come from `checks`; `top_reasons` from the exact
  `reason` text so the two views stay reconcilable with `validation_errors.json`.
- Duplicates count under *uniqueness* and are **not** attributed to each key
  column in `by_column`, otherwise every duplicate would inflate six columns.
- `examples` are capped (default 5, configurable in code) and ordered by file
  then row so the JSON is diff-stable across runs.
- Percentages use `input_rows` (non-blank rows) as the denominator, guarded for
  zero.

### 2.4 Writer (`reporting/quality_report.py`)

`write_quality_json(path, report)` writes `json.dumps(report.to_dict(),
indent=2, ensure_ascii=False)` plus trailing newline. Kept separate so a later
Excel sheet or PDF section can reuse the same model.

### 2.5 Pipeline integration (`pipeline.py`)

- Use `load_files_with_stats`, build the report after validation, write
  `data_quality.json` into staging for **every** format. It therefore appears in
  the manifest's `outputs` list automatically; no existing manifest key changes.
- `PipelineResult` gains `quality: QualityReport | None = None` (trailing,
  defaulted).
- New format value `quality` = validation-only mode: ingest, validate, write
  `validation_errors.json`, `data_quality.json` and `manifest.json`, skip
  analytics and all report writers. In this mode an input with zero valid rows is
  **not** an error (that is exactly when you want the report), so
  `PipelineResult.analytics` becomes `Analytics | None` and the manifest's
  analytics fields are `null`. All other formats keep raising `ValidationError`.
- Optional quality gate (see decisions): `max_invalid_percent` in config /
  `--max-invalid-percent` on the CLI; when exceeded the run still publishes the
  quality report but the CLI exits with a distinct code.

### 2.6 CLI and GUI

- `--format` accepts `quality` in addition to the current four values.
- After the run, print one extra line, e.g.
  `Data quality: 98.04% accepted (350/357); 7 invalid; top issue: invalid_date (1)`.
- Optional `--quality-json PATH` to also copy the report to a fixed path for CI
  or dashboards (the run folder name is not predictable).
- GUI: add `quality` to the format option menu (one line).

## 3. Files to change

New:

| File | Content |
| --- | --- |
| `src/automation_toolkit/processing/quality.py` | `QualityReport`, `CheckSummary`, `ColumnSummary`, `FileSummary` dataclasses; `build_quality_report()`; `to_dict()` |
| `src/automation_toolkit/reporting/quality_report.py` | `write_quality_json()` |
| `tests/test_quality.py` | unit and integration tests (section 5) |
| `examples/generated/data_quality.json` | regenerated demo output, linked from the README |

Modified:

| File | Change |
| --- | --- |
| `ingestion/__init__.py` | `IngestionStats`/`FileStats`, `load_files_with_stats()`, `load_files()` becomes a wrapper; export names in `__all__` |
| `processing/validation.py` | check-code constants, `flag(..., code)`, `ValidationResult.checks` (trailing field, default empty) |
| `pipeline.py` | stats-aware loading, report build and export, `quality` format, optional gate, `PipelineResult.quality` |
| `cli.py` | `quality` choice, summary line, `--quality-json`, optional `--max-invalid-percent`, handle `analytics is None` |
| `gui/app.py` | add `quality` to the format menu |
| `config.py` + `config.yaml` | optional keys `max_invalid_percent: float | None` (and, if wanted, `quality_examples: int`) with validation |
| `tests/test_pipeline.py` | manifest `outputs` includes `data_quality.json`; `quality` format; all-invalid input in quality mode |
| `tests/test_checkpoint.py`, `tests/test_incremental_csv.py` | one test each: chunked and resumed runs produce the identical quality report as a whole-file run |
| `README.md` | capability row, outputs list, "See the result" link, example commands |
| `docs/usage.md` | new "Data quality report" section, outputs tree, exit codes |
| `docs/architecture.md` | flow diagram node, check-code table, report contract |
| `CHANGELOG.md` | Unreleased entries |
| `.github/workflows/tests.yml` | optional extra smoke step `--format quality` |

Not changed in this step: `excel_report.py`, `pdf_report.py`, `charts.py`,
`checkpoint.py`, `csv_reader.py`, `excel_reader.py`, `analytics.py`,
`scripts/*`. A "Data Quality" worksheet and a richer PDF section are listed as
a follow-up because they alter pinned sheet names and the README's "eight
worksheets" wording.

## 4. Backward compatibility guarantees

- `load_files`, `validate`, `run_pipeline`, `main` keep their signatures; new
  parameters are keyword-only or optional with defaults.
- `ValidationResult` and `PipelineResult` only gain trailing fields with
  defaults; existing positional construction and attribute access keep working.
- `validation_errors.json` content and `errors` DataFrame columns are unchanged.
- `manifest.json`: no key removed or renamed; `outputs` lists one more file; in
  `quality` mode analytics fields are `null`.
- Existing `--format` values, config keys and exit codes `0/1/2` behave exactly
  as before. New config keys are optional; unknown keys are still rejected.
- Checkpoint format and `VERSION` unchanged, so existing checkpoints stay valid.
- Excel and PDF outputs unchanged in this step.

## 5. Testing plan (`tests/test_quality.py` unless noted)

- Clean fixture: zero issues, 100% accepted, empty `by_check`, all
  `complete_percent` 100.
- Dirty fixture: exact expectations: 7 invalid, `by_check` = invalid_date 1,
  invalid_quantity 1, missing_value(unit_price) 1, missing_value(product) 1,
  invalid_price 1, duplicate_line 2; dimensions completeness 2 / validity 3 /
  uniqueness 2; `top_reasons` reconciles with `validation_errors.json`.
- One row with four failures: `total_issues` 4, `invalid_rows` 1,
  `rows_with_multiple_issues` 1.
- Policies: `skip` gives the same statistics as `quarantine`; `reject_file` marks
  the file `rejected: true` and `excluded` counts its good rows.
- Blank rows and header-only chunks: `blank_rows_skipped` matches the log count,
  `input_rows` excludes them.
- Multiple files: `by_file` has one entry per file in input order, with sha256.
- Equivalence: whole-file, chunked (several sizes) and interrupted-then-resumed
  runs produce identical `to_dict()` output apart from `resumed_from_checkpoint`
  and `chunks` (parametrized, reusing helpers in `test_checkpoint.py`).
- JSON export: deterministic byte output for the same input, valid UTF-8, round
  trips through `json.loads`, `schema_version` present, all counts `int`.
- Pipeline: `data_quality.json` present for every format and listed in the
  manifest; `quality` format writes no xlsx/pdf/charts and succeeds on an
  all-invalid file; other formats still raise `ValidationError`.
- CLI: `--format quality` exit 0 and summary line; `--quality-json` copy exists;
  gate exceeded returns the agreed exit code; `--version`/existing flags intact.
- Validation: `checks` has one row per failed check and is empty for clean data;
  `test_skip_keeps_only_diagnostics` continues to pass unchanged.
- Coverage stays above the 80% gate; ruff check/format clean.

## 6. Suggested order of work

1. `validation.py` check codes and `checks` field, with tests.
2. `ingestion/__init__.py` statistics and `load_files_with_stats`, with tests.
3. `processing/quality.py` model and `reporting/quality_report.py` writer, with
   fixture-based tests.
4. `pipeline.py`, `cli.py`, `gui/app.py`, config keys, equivalence tests.
5. Docs, changelog, `config.yaml`, regenerated `examples/generated/data_quality.json`,
   CI smoke step.

## 7. Decisions needed from you

1. **Check codes vs. reason parsing.** I recommend codes recorded in a new
   `checks` frame (section 2.1). Alternative: derive summaries by regex from
   `reason`, no validation change, but brittle when messages change.
2. **Validation-only mode name.** `--format quality` (minimal change) or a
   dedicated flag such as `--validate-only`? I recommend `--format quality`.
3. **Quality gate.** Include `max_invalid_percent` now or later? If now, exit
   code `3` for "reports published but gate exceeded"?
4. **Extra CLI copy.** Is `--quality-json PATH` useful for your workflow, or is
   the file inside the run folder enough?
5. **Excel/PDF integration.** Add a "Data Quality" worksheet and a per-check table
   in the PDF in this step (changes pinned sheet names and README wording) or as
   a follow-up?
6. **Example count** in `by_check.examples`: fixed at 5 or configurable?
