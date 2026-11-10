# Changelog

## Unreleased

- Add dataset profiling: `processing/profiling.py` (`profile_csv`, `profile_frame`)
  summarizes any CSV regardless of schema - row/column counts, missing cells,
  duplicate, blank and short rows, per-column inferred type, placeholder and
  whitespace counts, unique and most frequent values, numeric/date/text statistics
  and warnings (duplicate/blank headers, empty, constant or mostly-missing columns,
  type outliers). The new `automation-toolkit-profile` command prints the summary and
  writes JSON (`--json`), with `--strict` returning exit status 2 on warnings. Empty,
  missing, undecodable and malformed files raise `IngestionError` (exit status 1); a
  header-only file is an empty dataset. Existing commands and the pipeline are unchanged.
- Make the profiler stream large CSVs: records are processed in chunks (`--chunk-size`,
  `csv_chunk_size`, default 50,000) by the new incremental `DatasetProfiler`, with
  results independent of the chunk size. Memory is bounded by one chunk plus 8-byte
  hashes for distinct rows and values; top values beyond 10,000 distinct values and
  medians beyond 200,000 numbers per column are flagged as approximate. Previously the
  whole file was read up to three times.
- Profiler: duplicate headers get unique names (`a`, `a_2`, ...) with the original kept
  in `header`, and headers differing only in case or spacing are flagged. Missing values
  are split into empty, whitespace-only, absent and (with `--null-as-missing`)
  placeholder cells, with `rows_with_missing`/`complete_rows`; `--null-token` adds
  placeholders. Columns with no type reaching 90% are inferred as `mixed`, and every
  column reports its dominant type, share and per-type examples; integers with leading
  zeros are flagged. The JSON `schema_version` is now 2 (new keys only, except that
  duplicate column `name`s are now unique). A blank first line is reported as a
  malformed header.
- Profiler reliability and memory fixes:
  - Fix a crash when profiling an in-memory frame whose index has repeated labels (for
    example the result of `pd.concat`): type classification is now positional.
  - Replace the vectorized reservoir median sample with a positional bottom-k sketch.
    The old sample was biased towards the rows seen last (on sorted input it estimated
    2859 where the true median was 2499.5) and depended on the random draw order; the
    sketch is a uniform sample and is provably identical at any chunk size.
  - Report a bare blank line like an all-empty line (`,,,`): a blank row of empty cells,
    rather than a short row of absent fields.
  - Strip each chunk once instead of twice and hash rows column by column, removing a
    full extra copy of every chunk; compact the hash sets when many small chunks arrive.
  - Size the default chunk by cells as well as rows (about 500,000 cells), so a very
    wide file no longer holds millions of strings at once.
  - Warn about values containing NUL or other control characters, which Python's CSV
    reader accepts as ordinary data.
  - Add `scripts/benchmark_profiling.py`: a 100,000-row messy-CSV benchmark reporting
    wall-clock time, tracemalloc peak and process RSS per chunk size, which fails if the
    chunk size changes the result. Time and memory are measured in separate passes
    because tracemalloc slows the run by about ten times.
- Profiler fixes from stress testing (1M-row high-cardinality, 400-column and 100 KB-field
  files, unusual values, repeated runs for leaks):
  - Fix a crash on date columns mixing UTC offsets (`+02:00` next to `Z`); such dates
    are compared in UTC.
  - Integer minimum and maximum are now exact: values above 2**53 were rounded through
    float, and values beyond int64 lost digits.
  - Mean and standard deviation are accumulated relative to the column's first value,
    so they no longer drift with the chunk size for large values with a small spread
    (std 2.887031 instead of 2.886896 at chunk size 1). When they exceed the float range
    they are `null` with a warning instead of silently `null`.
  - Do string work once per distinct value per chunk instead of once per cell: about
    60% faster on the 100,000-row benchmark and 2.4 times faster on wide files.
  - Default chunks are also capped at about 32 million characters: a file of 100 KB
    fields went from 452 MB to 134 MB peak RSS.
  - Repeated runs were checked with tracemalloc and retain no memory.
- Profiler numerical accuracy and top values:
  - Top values are now exact for any number of distinct values. Beyond 10,000 distinct
    values a frequent value seen late was missing entirely (500 occurrences after 15,000
    unique ids were not listed). Each distinct value now costs 16 bytes and only the text
    of the 64 best-ranked values is kept; `top_values_exact` is always true and the
    "top values are approximate" warning is gone.
  - Large integers: the standard deviation uses exact differences (`10**20 + 1` and
    `10**20 + 2` gave 0.0, now 0.707107), the median is exact beyond 2**53 (was
    9007199254740996.0 instead of ...995), the mean comes from an exact sum, and integers
    longer than a float can hold are integers instead of text. Integers beyond the
    float range next to floats in another chunk crashed; that is fixed.
  - Extreme floats: `1e-9` and other tiny values were reported as 0.0; values near the
    float limit gave a `null` mean (the mean of `1e308, -1e308` is 0); tiny spreads gave
    a standard deviation of 0. A result that truly exceeds the float range is `null`
    with a warning in every case.
  - Results no longer change in the last digit between chunk sizes for large values, and
    a float column reports float extremes whether or not a chunk held only integers.
  - Dates of different resolutions are combined as timestamps; the previous `fillna`
    merge raised a pandas deprecation warning and is slated to fail in a future pandas.
  - Median samples share a 64 MB budget per file, and the text held between count
    compactions is shared by all columns, which bounds memory on very wide files.
  - The approximate median was checked on sorted, lognormal and bimodal columns of
    600,000 values: its rank was within 0.13% of the true median at every chunk size.
- Add `workflow.py`, a minimal sequential workflow engine: a `Workflow` is an
  ordered list of uniquely named `Step` callables sharing one `WorkflowContext`
  (free-form `data` plus the `outputs` of completed steps). A failing required step
  skips the remaining steps and raises the new `WorkflowError` (a `ToolkitError`,
  so the CLI's error handling applies) with the partial `WorkflowResult` attached;
  steps may instead be declared `continue_on_error` or given `retries`.
  `KeyboardInterrupt` is never swallowed. The existing `run_pipeline` is unchanged.
- Make workflow steps more resilient: `retry_delay`, `backoff` (exponential by
  default, `backoff=1` for a constant wait) and `max_delay` configure the wait between
  retries; `timeout` bounds a single attempt and abandons it with the new
  `StepTimeoutError` (a `WorkflowError` and a `TimeoutError`), which is retried like
  any other failure. `StepResult` now records the error of every attempt, the
  formatted traceback of the last one, the delays waited and start/finish times;
  `WorkflowResult.report()` renders a per-step, per-attempt text report, and a failed
  run's `WorkflowError` names the step position, elapsed time, earlier attempt errors
  and skipped steps and exposes the failing step as `error.step`. Defaults keep the
  previous immediate-retry, no-timeout behavior; `Workflow(..., sleep=...)` lets tests
  replace `time.sleep`.
- Add workflow checkpointing and recovery: `Workflow.run(checkpoint=path)` writes a
  checksummed JSON checkpoint after every succeeded step (temporary file, fsync,
  atomic rename) recording the completed prefix of outputs and the shared data, and a
  later run of the same workflow restores that prefix and continues with the first
  unfinished step; a finished run removes the file. Checkpoints that are unreadable,
  corrupted, checksum-mismatched, from another version or workflow, or whose step list
  differs are discarded with a warning and the workflow starts over. Outputs and data
  must be JSON serializable; otherwise the run stops with an explicit `WorkflowError`
  and the earlier checkpoint is kept. `StepResult.restored`, `WorkflowResult.restored`
  and `WorkflowResult.resumed` expose what was skipped.
- Fix: a timed-out step attempt could keep writing into the shared context after the
  engine had moved on. Timed attempts now run against a private copy of the context
  whose changes are merged back only when the attempt finishes in time, so abandoned
  attempts cannot alter later steps, the result or a checkpoint.
- Add JSON workflow reports: `WorkflowResult.to_dict`, `to_json` and `write_json` render
  a versioned record (`report_version` 1) with the run summary and counts and, per step,
  the status, attempts and retries, every attempt's error type and message with the
  delay waited before the retry, the last traceback, timing, whether the step timed out
  or was restored from a checkpoint, and its output (non-serializable outputs are
  replaced by their `repr` and flagged). `StepResult.to_dict` is available on its own.
- Add the `automation-toolkit-workflow` console script (`automation_toolkit.workflow_cli`)
  to run and resume workflows from the terminal: `--workflow module:attr` or
  `path/file.py:attr` (a `Workflow` or a factory), `--checkpoint FILE` to save progress
  and resume automatically, `--restart` to discard it, `--data KEY=VALUE` and
  `--data-json FILE` for initial context values, `--json-report FILE|-`, `--describe`
  and `--traceback`. Exit status 0 on success, 2 when only optional steps failed, 1 on
  failure or usage errors and 130 when interrupted (the checkpoint is kept).
- Harden the workflow engine for production use: `Workflow.run` rejects non-mapping
  `initial_data` and invalid `checkpoint` arguments with a `WorkflowError`; checkpoint
  and JSON report writes fsync the directory after the atomic rename and the JSON report
  is written atomically as well; checkpoints with an invalid attempt count or
  pathologically nested JSON are discarded like other corruption; JSON reports reject
  `NaN`/infinite outputs (replaced by their `repr` and flagged) so they stay strictly
  valid; `automation-toolkit-workflow --restart` without `--checkpoint` is a usage
  error. New end-to-end tests cover toolkit workflows with checkpoint recovery, real
  timeouts and backoff waits, abandoned attempts, the CLI in-process and as a
  subprocess killed mid-step.
- Add stable validation check identifiers (`missing_value`, `invalid_date`,
  `invalid_quantity`, `invalid_price`, `duplicate_line`) and a new
  `ValidationResult.checks` frame with one row per failed check
  (`source_file`, `source_row`, `code`, `column`), so statistics no longer depend on
  parsing the human-readable reason text. Existing validation behavior, the `errors`
  frame and `validation_errors.json` are unchanged.
- Add `ValidationResult.file_rows` recording the validated input rows per source file.
- Add `processing/quality.py` with a `QualityReport` model and `build_quality_report`,
  aggregating validation statistics by check, by business column, by quality dimension
  (completeness, validity, uniqueness) and by source file, plus run-level counts,
  percentages and the most common reasons. `QualityReport.to_dict` renders a
  deterministic, JSON-ready mapping.
- Add the optional `quality_report` setting and `--quality-report` CLI flag: when
  enabled, every run folder also contains `data_quality.json` (listed in the manifest's
  `outputs`) and the console prints a one-line quality summary. The report is built
  from the run's existing validation result without rereading inputs; it is off by
  default and does not change validation, the other outputs or exit codes.
- Add `load_files_with_stats`, returning the same frame as `load_files` plus
  `IngestionStats`: per file the raw records read, blank rows skipped, data rows,
  whether it was read in chunks, chunks processed in this run, and chunks and records
  restored from a checkpoint. The quality report gains an `ingestion` section and a
  per-file `ingestion` object; the console summary mentions skipped blank rows and
  resumed files. Whole-file, chunked and resumed reads report identical record, blank
  and data-row counts.

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
