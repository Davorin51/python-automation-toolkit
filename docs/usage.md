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
