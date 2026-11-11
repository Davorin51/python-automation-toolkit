# Usage reference

## Install and run

Python 3.11 or newer is required. From the repository root:

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell instead:
# .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'   # or plain `-e .` if you only need the CLI
python -m automation_toolkit --input examples/sample_sales.csv --output reports --format all --config config.yaml
```

Running `pytest` requires the `test` extra (`pip install -e '.[test]'`), which
adds pytest and pypdf on top of the runtime dependencies. The `dev` extra already
includes it.

The installed `automation-toolkit` command is equivalent to
`python -m automation_toolkit`. Paths with spaces must be quoted.

```bash
automation-toolkit --input january.csv february.xlsx --output reports --format excel
automation-toolkit --input examples/sample_sales_dirty.csv --output reports --format pdf
automation-toolkit --input examples/sample_sales.csv --output reports --format charts --verbose
```

Do not select the CSV and XLSX demo together: they contain the same transactions.
The dirty demo adds five invalid lines and two duplicate lines.

## Configuration

```yaml
company_name: Example Retail
currency: EUR
validation_policy: quarantine
date_format: "%Y-%m-%d"
csv_delimiter: ","
csv_encoding: utf-8-sig
csv_chunk_size: null
csv_checkpoint_dir: null
excel_sheet: 0
top_n: 10
quality_report: false
column_mapping:
  Invoice Date: date
  Item: product
  Segment: category
  Units: quantity
  Selling Price: unit_price
  Invoice Number: order_id
```

The config file is optional; unknown keys and unsupported values are rejected.
Excel sheet selection is a zero-based index or a sheet name. CSV numeric values
use a decimal point without thousands separators. Localized date strings require
an explicit `date_format`, for example `%d/%m/%Y`. Native Excel date cells are
accepted. Order IDs should be stored as text to preserve leading zeros.

Common aliases include `Date`, `Transaction Date`, `Product Name`, `Qty`, `Price`,
`Unit Price` and `Order ID`. Whitespace and hyphens in headers become underscores.
Two headers mapping to one field produce an error that names both headers instead
of a silent choice. Blank header cells, such as trailing delimiters, are ignored.
Product and category values retain case, and surrounding whitespace is removed
from every value before validation. Completely blank rows are skipped and logged.
For CSV inputs, `source_row` is the logical record number including the header;
quoted fields containing line breaks can span several physical text-file lines.

Each record in `validation_errors.json` and the Validation Errors worksheet has
`source_file`, `source_row`, `reason` and `invalid_columns`. The reason lists each
failed check separated by `; ` (for example `Missing unit_price` for a blank cell,
`Invalid date (expected format %Y-%m-%d)` or `Duplicate transaction line (first
occurrence: january.csv row 12)`), and `invalid_columns` lists the affected
columns in the same order. Under `quarantine` the raw values are included too.

## Incremental CSV processing

By default each CSV file is parsed in one piece. Set `csv_chunk_size` to a positive
integer, or pass `--chunk-size ROWS` on the command line, and CSV inputs are read
incrementally in chunks of that many records instead:

```bash
automation-toolkit --input big_export.csv --output reports --format excel --chunk-size 50000
```

The command-line flag overrides the configuration value for that run. XLSX inputs
are unaffected. The results are identical to a whole-file read: the same columns,
raw text values, blank-row handling, `source_row` numbers and validation outcome,
which the test suite checks against the bundled fixtures. A chunk size of `0`, a
negative number or a non-integer is rejected as a configuration error (exit code
`1`); a non-numeric `--chunk-size` is rejected by argparse (exit code `2`). A
malformed line or an undecodable byte in any chunk stops the run with the usual
`Cannot read CSV <file>: ...` message, so a problem late in a large file is
reported the same way as one at the top.

In chunk mode each chunk is normalized, stripped of extra columns and cleared of
blank rows as soon as it is parsed, so peak memory is roughly one raw chunk plus
the final table of required fields rather than the whole raw file. Progress is
logged in `--verbose` mode. The pipeline still assembles the complete validated
table because duplicate detection, trend calculations and the Excel export need
every row. For your own scripts the generator behind this is available directly:

```python
from pathlib import Path

from automation_toolkit.config import Config
from automation_toolkit.ingestion import iter_csv_chunks, normalize_columns

config = Config(csv_chunk_size=10_000)
for chunk in iter_csv_chunks(Path("big_export.csv"), config):
    frame = normalize_columns(chunk, config)
    ...  # chunk.index + 2 is the record number in the file, header included
```

`load_files_with_stats` returns the same frame as `load_files` together with an
`IngestionStats` object (per file: records read, blank rows skipped, data rows,
chunks processed and restored, checkpoint use); `load_files` itself is unchanged.

Each chunk carries the header values as its columns, keeps cells as untouched text,
and continues the integer index from the previous chunk. A header-only file yields
one empty chunk; an empty file raises `IngestionError`. Passing `chunk_size=` to
the generator overrides the configured value, and leaving both unset yields the
whole file as a single chunk.

## Checkpoints and resuming an interrupted run

Chunked CSV loading can record its progress so that a run interrupted by Ctrl-C,
a crash or a malformed record resumes at the first unfinished chunk instead of
starting over. Set `csv_checkpoint_dir` in the configuration or pass
`--checkpoint-dir DIR` together with a chunk size:

```bash
automation-toolkit --input big_export.csv --output reports --chunk-size 50000 --checkpoint-dir .checkpoints
# ... interrupted ...
automation-toolkit --input big_export.csv --output reports --chunk-size 50000 --checkpoint-dir .checkpoints
```

Each CSV input gets its own folder under the checkpoint directory, named after the
file plus a hash of its full path. After every chunk the normalized rows of that
chunk are written as a small JSON file and the folder's `checkpoint.json` manifest
is updated; both are written to a temporary name first and renamed, so a crash
never leaves a half-written chunk in place. The manifest records the source file's
path, size and modification time, the chunk size, delimiter, encoding and column
mapping, and a SHA-256 checksum of every chunk file. The second run restores the
saved chunks, skips exactly that many records in the file (quoted line breaks and
blank lines included) and logs
`Resuming <file> from checkpoint: N chunks (M records) already processed`.
When a file has been fully loaded its checkpoint folder is removed, so a run that
completes leaves the directory empty. The result is identical to an uninterrupted
run, including `source_row` numbers and the blank-row count in the log.

A checkpoint is only trusted when everything in it checks out. If the manifest is
unreadable or malformed, the source file changed size or modification time, the
settings differ, a chunk file is missing, truncated, altered or does not match its
manifest entry, the whole checkpoint is discarded with a warning
(`Ignoring unusable checkpoint for <file> (<reason>); processing from the start.`)
and the file is processed from the beginning. Stray files in the folder are
ignored. A checkpoint directory that cannot be written (for example a path that is
an existing file) stops the run with `Cannot write checkpoint for <file>: ...`,
because silently running without the requested checkpoints would be a surprise.
`csv_checkpoint_dir` without `csv_chunk_size` is a configuration error. Excel
inputs are never checkpointed, and nothing is written unless the option is set.

Checkpoints cover ingestion only. Validation, analytics and report generation
always run on the complete table, so a failure while writing reports is reported
as before and the next run re-reads the inputs.

## Data quality report

Set `quality_report: true` in the configuration or pass `--quality-report` on the
command line and every run folder additionally contains `data_quality.json`. The
report is computed from the validation result that the run already produced, so the
inputs are not read a second time and nothing about validation, the other outputs
or the exit codes changes; the file is simply listed as one more entry under
`outputs` in the manifest. The console then prints one extra line, for example
`Data quality: 98.04% accepted (350/357); 7 invalid; top issue: duplicate_line (2)`;
when relevant it also mentions skipped blank rows and files resumed from a
checkpoint.

```bash
automation-toolkit --input examples/sample_sales_dirty.csv --output reports --format excel --quality-report
```

The JSON has a `schema_version`, the toolkit version, the manifest's
`generated_at`, the validation `policy` and the ingestion `settings`, followed by:

- `summary`: files, rejected files, input/accepted/invalid/excluded rows with
  percentages of the input rows, `total_issues` (failed checks, not rows) and
  `rows_with_multiple_issues`.
- `ingestion`: how the inputs were read: `records_read` (raw records including
  blank ones), `blank_rows_skipped`, `data_rows` (equal to the summary's input
  rows), how many files were read in chunks, `chunks_processed` in this run,
  `chunks_restored` from checkpoints, how many files had checkpointing enabled and
  how many `files_resumed_from_checkpoint`. A whole-file CSV or XLSX read counts
  as one chunk. Chunked and whole-file reads of the same file report the same
  records, blank rows and data rows; only the chunk counts differ.
- `dimensions`: distinct affected rows for `completeness` (missing values),
  `validity` (malformed dates, quantities, prices) and `uniqueness` (duplicates).
  A row with problems in two dimensions counts in both.
- `by_check`: one entry per check code and column, most frequent first, with a
  configuration-independent `description`, the row count, its percentage and up
  to five example locations (`source_file`, `source_row`). Codes are
  `missing_value`, `invalid_date`, `invalid_quantity`, `invalid_price` and
  `duplicate_line`; the latter has an empty `column` because a duplicate is a
  property of the whole line.
- `by_column`: `missing`, `invalid`, distinct `issue_rows` and `complete_percent`
  for each business column (plus `order_id` when present). Duplicates are not
  attributed to their key columns here.
- `by_file`: input, accepted, invalid and excluded rows, the number of failed
  checks and whether the file was rejected under `reject_file`, in input order.
  Each entry also carries an `ingestion` object with the file's `format`,
  `records_read`, `blank_rows`, `data_rows`, `chunked`, `chunks_processed`,
  `chunks_restored`, `records_restored`, `checkpointed` and
  `resumed_from_checkpoint`.
- `top_reasons`: the ten most common verbatim `reason` texts, so the counts
  reconcile with `validation_errors.json`.

Percentages are rounded to two decimals and use the validated input rows (blank
rows already removed) as denominator. Identical inputs produce byte-identical
files apart from `generated_at`.

## Composing steps with the workflow engine

`automation_toolkit.workflow` runs named steps in order and stops at the first
failure of a required step. Each step receives a `WorkflowContext`: `ctx["key"]`
reads and writes free-form data, and `ctx.outputs["step"]` holds the return value
of an earlier step.

```python
from automation_toolkit.config import Config
from automation_toolkit.exceptions import WorkflowError
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate
from automation_toolkit.workflow import Workflow

config = Config()
workflow = (
    Workflow("sales")
    .add_step("load", lambda ctx: load_files(ctx["inputs"], config))
    .add_step("validate", lambda ctx: validate(ctx.outputs["load"], config))
    .add_step("analyze", lambda ctx: analyze(ctx.outputs["validate"].transactions))
    .add_step(
        "notify",
        send_summary,
        continue_on_error=True,
        retries=3,
        retry_delay=1.0,  # waits 1s, 2s, 4s between the attempts
        backoff=2.0,
        max_delay=30.0,
        timeout=10.0,  # each attempt is abandoned after 10s
    )
)
try:
    # With a checkpoint path, an interrupted run resumes after its last completed step.
    result = workflow.run({"inputs": inputs}, checkpoint=".checkpoints/sales-workflow.json")
except WorkflowError as exc:
    print(exc)  # which step failed, after how many attempts, and why
    print(exc.result.summary())  # counts of succeeded, failed and skipped steps
    print(exc.result.report(include_traceback=True))  # every attempt of every step
else:
    print(result.summary())
    if not result.succeeded:  # only optional steps can fail without raising
        print("Failed optional steps:", result.failed)
    metrics = result.outputs["analyze"]
```

Failed attempts are logged as warnings while retrying and as an error (or a warning
for `continue_on_error` steps) when the attempts are exhausted. The wait before retry
`k` is `retry_delay * backoff ** (k - 1)` seconds, never more than `max_delay`; the
defaults retry immediately and `backoff=1` keeps the wait constant. A `timeout` bounds
each attempt: a slow attempt fails with `StepTimeoutError` and is retried like any other
error, but the abandoned callable keeps running in the background until it returns, so
only use timeouts for work that is safe to abandon. Each `StepResult` exposes `errors`
(one per failed attempt), `traceback`, `delays`, `attempts`, `duration_seconds`,
`started_at` and `finished_at`; `result.step("notify").timed_out` tells whether the
last attempt hit the timeout. `KeyboardInterrupt` always propagates.

Passing `checkpoint=` saves the outputs and shared data of every completed step to a
checksummed JSON file (written atomically) and removes it when the run finishes. Running
the same workflow again with the same path restores the completed steps instead of
repeating them; `result.restored` lists them. The step outputs and the values stored in
the context must be JSON serializable (lists, dicts, strings, numbers), otherwise the run
stops with a clear error. Do not pass frames or other objects through the context when
checkpointing; store file paths or run folders instead. A damaged, tampered or outdated
checkpoint, or one written for different steps, is ignored with a warning and the
workflow starts from the beginning.
A process that is killed mid-step resumes from the last completed step on the next
run. Do not run two resumes of the same checkpoint file at once.

## Running and resuming workflows from the terminal

`automation-toolkit-workflow` (or `python -m automation_toolkit.workflow_cli`) runs a
`Workflow` object defined in Python. Point `--workflow` at `module:attribute` or at
`path/to/file.py:attribute`; the attribute may be a `Workflow` or a zero-argument
function returning one.

```bash
# nightly.py defines: nightly = Workflow("nightly").add_step(...)...
automation-toolkit-workflow --workflow nightly.py:nightly --describe
automation-toolkit-workflow --workflow nightly.py:nightly \
    --data inputs='["exports/jan.csv"]' --data limit=100 \
    --checkpoint .checkpoints/nightly.json --json-report reports/nightly.json
# ... interrupted or a step failed: fix the cause and run the same command again.
# Completed steps are restored from the checkpoint; --restart discards it instead.
```

`--data KEY=VALUE` sets initial context values (repeatable; the value is parsed as
JSON when possible, otherwise kept as text) and `--data-json FILE` loads a JSON object;
command-line pairs override the file. `--json-report FILE` writes the JSON result report
next to the text report, and `--json-report -` prints only the JSON to stdout so it can be
piped. `--traceback` adds tracebacks to the text report.

| Exit status | Meaning |
| --- | --- |
| 0 | Every step succeeded |
| 2 | The run completed, but `continue_on_error` steps failed |
| 1 | A required step failed after its retries, or the workflow could not be loaded; the text/JSON report is still produced and the checkpoint is kept |
| 130 | Interrupted with Ctrl-C; the checkpoint is kept for a later resume |

The JSON report (`report_version` 1) contains `workflow`, `succeeded`, `resumed`,
`summary`, `duration_seconds`, `counts` by status, the `failed`, `skipped` and
`restored` step names and one entry per step with `status`, `attempts`, `retries`,
`errors` (type, message and the delay waited before the next attempt), the last `error`,
`traceback`, `started_at`, `finished_at`, `timed_out`, `restored`, `output` and
`output_serializable`. From Python the same report is available as `result.to_dict()`,
`result.to_json()` and `result.write_json(path)`.

## Profiling any CSV dataset

`automation-toolkit-profile` (or `python -m automation_toolkit.profile_cli`) describes
what a CSV file contains before you feed it to the pipeline. It does not require the
sales schema and never rejects rows; it only reports.

```bash
automation-toolkit-profile --input examples/sample_sales_dirty.csv
automation-toolkit-profile --input jan.csv feb.csv --json reports/profile.json
automation-toolkit-profile --input export.csv --delimiter ";" --encoding cp1250 --strict
# A large export, 200,000 records per chunk, with NA/null and "#N/A" counted as missing
automation-toolkit-profile --input big.csv --chunk-size 200000 --null-as-missing --null-token "#N/A"
```

The delimiter, encoding and `date_format` come from `--config` (same YAML as the
pipeline) and can be overridden with `--delimiter` and `--encoding`.

| Section | Contents |
| --- | --- |
| Dataset | Rows, columns, missing cells and percentage, duplicate rows (exact repeats of an earlier non-blank row), completely blank rows, short rows (fewer fields than the header) |
| Per column | Inferred type (`integer`, `float`, `boolean`, `date`, `string`, `mixed` or `empty`), dominant type and its share, count and up to three examples of each value type, missing values split into empty, whitespace-only, absent (short row) and placeholders, missing values, placeholder values, unique values, up to five most frequent values, numeric min/max/mean/median/std, date range or text length range, whitespace-padded values |
| Warnings | Duplicate or blank headers (with their new names), headers differing only in case or spacing, mixed-type columns, integers with leading zeros, whitespace-only cells, control characters such as NUL, no data rows, short/blank/duplicate rows, columns that are empty, mostly missing (at least 50%) or constant, placeholders, padded values and columns that are at least 90% one type but contain outliers (for example an invalid date) |

Missing means an empty cell, a whitespace-only cell or a field absent because the
record had fewer fields than the header; `missing_breakdown` counts each kind, and
`rows_with_missing` / `complete_rows` count rows. Placeholder text such as `NA`, `N/A`,
`null`, `None` or `-` is counted separately as `null_like` and excluded from type
inference, because the toolkit otherwise keeps such values as text. `--null-as-missing`
counts placeholders as missing too, and `--null-token TEXT` (repeatable) adds your own.

A type is inferred only when every non-missing value matches it; dates match the
configured `date_format` or ISO 8601, and integers mixed with decimals are `float`.
When one type covers at least 90% of the values the column stays `string` and a
"mostly <type>" warning names the outliers (for example one invalid date). When no
type reaches 90%, the column is `mixed`; its statistics then describe the dominant type
only and say so in `stats.of`.

Blank lines and all-empty lines (`,,,`) are both reported as blank rows of empty cells,
never as short rows. A row with fewer fields than the header is a short row, and its
absent fields are counted under `absent` rather than `empty`. Records with embedded
newlines or delimiters inside quotes are single rows, and the line number in a
malformed-file error counts the physical lines, so it still points at the right place.

Every header gets a unique `name`: duplicates keep the first occurrence and later ones
become `<header>_2`, `<header>_3`, ... (skipping names already used), and blank headers
become `column_<position>`. The original text stays in `header`, and each column is
profiled separately.

**Large files.** The file is streamed in chunks of `--chunk-size` records and never
loaded whole. Without an explicit size (or `csv_chunk_size`) the profiler uses 50,000
rows, capped so one chunk holds about 500,000 cells and about 32 million characters,
so very wide files and files with long free-text fields use smaller chunks. An explicit
chunk size is never split further. Every reported statistic is identical for any chunk size; the test suite checks
that for chunk sizes from 1 to 100,000, and the benchmark re-checks it on 100,000 rows.

Memory grows only with distinct values: 8 bytes per distinct row (duplicate detection)
and 16 bytes per distinct value of each column (a 64-bit hash, an exact count and a
first-appearance rank). Values are compared by hash, so a collision could in principle
undercount by one. Unique counts and top values are exact at any size: only the text of
the 64 best-ranked values per column is kept, and since counts only grow, a value that
ends in the top five was always among them when it last appeared. `top_values_exact` is
kept for compatibility and is always true.

The median is exact up to 200,000 numbers per column; above that it is estimated from
a bottom-k sketch that keeps the values whose *row positions* hash smallest
(`stats.median_approximate`). Because a value's priority depends only on its position,
the sketch is a uniform sample of the column and gives the same answer at any chunk
size; on 600,000-value test columns its rank was within 0.13% of the true median. Median
samples share a budget of 8 million numbers (about 64 MB) per file, so files with more
than 40 columns keep smaller samples per column, never fewer than 20,000 (rank error
about 0.35%).

Minimum, maximum, mean and standard deviation are exact up to float precision:

- Integers keep full precision beyond 64 bits for minimum, maximum and median, and the
  mean of an integer column comes from an exact integer sum. The standard deviation is
  computed from exact differences, so `10**20 + 1` and `10**20 + 2` give 0.707107. A
  400-digit value is still an integer; only digits other than ASCII 0-9 make it text.
- Floats use three exact power-of-two scalings, so values near the float limit
  (`1e308`, `-1e308`) and tiny spreads (`1e-200`) neither overflow nor underflow. If a
  result truly exceeds the float range it is `null` with a warning. A literal such as
  `1e400` is not a valid float and is reported as text.
- Values are rounded to 6 decimals as before, but never to fewer than 6 significant
  digits (`1e-9` stays `1e-09`), and the mean and standard deviation are cut to 15
  significant digits, which removes float noise that would otherwise make them differ
  in the last digit between chunk sizes.
- Dates are compared in UTC when offsets are present (`2026-01-01T23:30:00-05:00` is
  `2026-01-02T04:30` UTC), date-only values count as midnight, and nanoseconds as well
  as years outside 1677-2262 are kept.

```bash
# Time and memory on 100,000 synthetic messy rows, at several chunk sizes
python scripts/benchmark_profiling.py --rows 100000 --json docs/benchmark_profiling.json
```

The [recorded result](benchmark_profiling.json) is one Linux x86_64 run: about 185,000
rows per second on a 7-column file, with the traced peak scaling from 22 MB at
`--chunk-size 10000` to 99 MB at 200,000. It is a single measurement on one machine,
not a performance guarantee; rerun it on the target machine before sizing a workflow.

| Exit status | Meaning |
| --- | --- |
| 0 | Every input was profiled |
| 2 | With `--strict`: every input was profiled but at least one has warnings |
| 1 | An input is missing, empty (no header), undecodable or malformed (a blank first line, or a row with more fields than the header), or the options are invalid; readable inputs are still profiled |

A header-only file is a valid, empty dataset (`rows: 0`). `--json PATH` writes one
profile object (`schema_version` 2: `source`, `summary`, `processing`, `warnings`,
`columns`), or a list of them for several inputs. From Python:

```python
from automation_toolkit.processing.profiling import profile_csv, profile_frame

profile = profile_csv("export.csv", chunk_size=100_000, null_as_missing=True)
print(profile.to_text())
data = profile.to_dict()

# Or feed your own chunks (columns in header order, None = absent field)
from automation_toolkit.processing.profiling import DatasetProfiler

profiler = DatasetProfiler(["id", "amount"], source="stream")
for chunk in chunks:
    profiler.update(chunk)
profile = profiler.result()
```

## Detecting outliers in numeric columns

`automation-toolkit-anomalies` (or `python -m automation_toolkit.anomaly_cli`) scans
the numeric columns of any CSV for outliers. Like the profiler, it does not require
the sales schema and never rejects rows; it only reports. Two methods are available
(`--method`, default `iqr`), and both report a "normal range" fence and every value
outside it:

- `iqr`: the first and third quartile and the Tukey fence
  `[Q1 - factor * IQR, Q3 + factor * IQR]`.
- `modified_zscore`: the median and the median absolute deviation (MAD); a value is an
  outlier when its modified z-score `0.6745 * (x - median) / MAD` exceeds
  `--threshold`, i.e. it lies outside `median +/- threshold * MAD / 0.6745`.

```bash
automation-toolkit-anomalies --input examples/sample_sales_dirty.csv
automation-toolkit-anomalies --input export.csv --json reports/anomalies.json --strict
# A wider fence, only one column, semicolon-delimited input
automation-toolkit-anomalies --input export.csv --delimiter ";" --factor 3 --column unit_price
# The modified z-score instead of the quartile fence, with a stricter cutoff
automation-toolkit-anomalies --input export.csv --method modified_zscore --threshold 3
```

The delimiter, encoding and chunk size come from `--config` (same YAML as the
pipeline) and can be overridden with `--delimiter`, `--encoding` and `--chunk-size`.
`--factor` sets the IQR fence multiplier (default 1.5, the usual convention; 3 is a
common choice for flagging only extreme values; 0 flags everything outside the
quartiles). `--threshold` sets the modified z-score cutoff (default 3.5, the usual
convention from Iglewicz & Hoaglin; 0 flags everything except the median). Each
option belongs to its method; combining `--factor` with `--method modified_zscore`
(or `--threshold` with `iqr`) is an error. The result lists, per column, the values
used, the method's measures (quartiles and IQR, or median and MAD), the fence, the
low/high outlier counts and percentage, and up to five of the most extreme outliers
with their row numbers (record numbers in the file, header included, matching
`source_row` elsewhere).

Both methods exclude the same cells, run on the same streamed chunks and report the
same shape of result; they differ only in where the fence sits. The quartile fence
breaks down when more than a quarter of the values are extreme (the quartiles
themselves move into the contamination), while the median fence tolerates up to half;
on the other hand `modified_zscore` is stricter on clean, skewed data. A MAD of 0 (at
least half the values identical) flags every differing value, with a warning, exactly
like an IQR of 0. Columns with fewer than four numeric values report no fence under
either method.

Missing and invalid values never abort the scan; they are excluded from the
calculation and reported:

- Missing cells - empty, whitespace-only, or absent because the record was short -
  are counted per column as `missing`.
- Placeholder text such as `NA`, `N/A`, `null`, `None` or `-` is counted as
  `null_like`, with a warning; `--null-token TEXT` (repeatable, case-insensitive)
  adds your own. A numeric sentinel works too (`--null-token -999`): cells whose
  stripped text equals it are placeholders, while `-999.0` stays a number.
- Any other non-numeric text is counted as `invalid`, with a warning naming up to
  three examples. A value beyond the float range (for example `1e400`) also counts
  as invalid. Numbers are compared as 64-bit floats, so integers beyond 2**53 lose
  precision here (unlike in the profiler's exact statistics).

Without `--column`, a column is scanned when at least 90% of its parsed (non-missing,
non-placeholder) values are numeric; below that it is skipped with a warning naming
the numeric share, and a column with no numeric values at all is skipped silently.
`--column NAME` (repeatable) restricts the scan to the named columns and analyzes
them even when they are mostly non-numeric; an unknown name is an error. Column
names are the profiler's unique display names, so duplicated headers are `a`,
`a_2`, ... and blank headers `column_<position>`. A column with fewer than four
numeric values reports no bounds, with a warning, because quartiles of so few values
say little. A constant column (IQR of 0) is flagged, since every deviating value then
counts as an outlier.

Quartiles use linear interpolation between the two nearest values (NumPy's default
method, the same as Excel's `QUARTILE.INC`). A value exactly on a fence is not an
outlier. The reported "most extreme" examples are ordered by distance outside the
nearest fence, ties by row number. Quartiles, bounds and values are rounded like the
profiler's statistics (6 decimals, at least 6 significant digits); when the IQR is
below 0.001 more decimals are kept so it shows three significant digits. A fence that
exceeds the float range (data near `1e308`) is `null` in JSON, `inf` in the text
report, and produces a warning: nothing can lie outside it on that side.

Files are streamed in chunks exactly like the profiler, and every reported number,
row number and example is independent of the chunk size. Without an explicit
`--chunk-size`, chunks are capped at 50,000 records and about 32 million characters
(like the profiler), so files with very long fields use smaller chunks. Memory is one
chunk plus, for each scanned column, 8 bytes per numeric value and 1 bit per row; with
`--column` the other columns are not stored at all. Computing the quartiles makes one
temporary copy of one column's values at a time. On 1,000,000 rows with three numeric
columns the traced peak is about 50 MB (34 MB with a single `--column`), so a column
of N numbers needs roughly 16 x N bytes at its peak; the same scan takes about 1.4
seconds (one Linux x86_64 measurement, not a guarantee). Cells are parsed first and
only the failures are stripped and classified, so clean numeric data is scanned at
full speed and messy data costs only in proportion to its messy cells.

Values at the edge of the float range are safe: an outlier at `1.7e308` is reported
unrounded (rounding it could exceed the largest float), a distance that overflows
counts as infinite and ranks first among the examples, and subnormal values such as
`5e-324` are ordinary numbers.

| Exit status | Meaning |
| --- | --- |
| 0 | Every input was scanned |
| 2 | With `--strict`: every input was scanned but at least one contains outliers |
| 1 | An input is missing, empty, undecodable or malformed, or the options are invalid (unknown column, negative factor or threshold, a cutoff option combined with the other method); readable inputs are still scanned |

`--json PATH` writes one result object (`schema_version` 2: `source`, `method`,
`factor`, `threshold`, `summary`, `warnings`, `columns`), or a list of them for
several inputs. The method that is not in use reports `null` for its keys: under
`iqr` each column fills `q1`/`q3`/`iqr` and leaves `median`/`mad` null, and the
reverse under `modified_zscore`; `lower_bound`/`upper_bound`, counts and examples
mean the same under both. (Schema version 1 lacked `threshold`, `median` and `mad`;
everything it had is unchanged.) From Python:

```python
from automation_toolkit.processing.anomalies import detect_outliers_csv, detect_outliers_frame

report = detect_outliers_csv("export.csv", factor=1.5, columns=["unit_price"])
robust = detect_outliers_csv("export.csv", method="modified_zscore", threshold=3.5)
print(report.to_text())
data = report.to_dict()

# Or feed your own chunks (columns in header order, None = absent field)
from automation_toolkit.processing.anomalies import OutlierDetector

detector = OutlierDetector(["id", "amount"], source="stream")
for chunk in chunks:
    detector.update(chunk)
report = detector.result()
```

## Outputs and exit status

The console prints the completed run directory. `all` writes:

```text
run-<timestamp>-<id>/
  sales_report.xlsx
  sales_report.pdf
  charts/categories.png
  charts/monthly.png
  charts/products.png
  charts/comparison.png
  validation_errors.json
  manifest.json
  data_quality.json      (only with quality_report / --quality-report)
```

PDF mode also produces its source PNG charts. Every format writes a manifest and
validation diagnostics. Exit code `0` means reports were generated; quarantined
rows are allowed and must be reviewed. Code `1` indicates an input/configuration
or output failure. Argparse uses code `2` for invalid command arguments. The
`--verbose` flag adds detailed logging. No reports are generated if all rows are
excluded; the error includes counts for diagnosis.

## Optional desktop application

```bash
python -m pip install -e '.[gui]'
automation-toolkit-gui
```

Select multiple CSV/XLSX files, an output folder, an optional YAML config path and
the report format, then choose **Generate Report**. The interface runs the same
pipeline as the CLI. It requires a graphical desktop and Tk; on some Linux
distributions Tk is installed separately as `python3-tk`. GUI interaction requires
manual desktop verification; headless automated tests cover the shared pipeline.

## Rebuild demo data and benchmark

```bash
python scripts/generate_demo.py
python scripts/benchmark.py --rows 100000 --format all --json docs/benchmark.json
```

The benchmark creates a temporary synthetic input and removes generated reports
when finished. It times core processing and then a full run including output
generation. The full run rereads the same file and can benefit from the file cache.
It is a single measurement, not a representative hardware benchmark suite.
