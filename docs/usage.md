# Usage reference

## Install and run

Python 3.11 or newer is required. From the repository root:

```bash
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell instead:
# .venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python -m automation_toolkit --input examples/sample_sales.csv --output reports --format all --config config.yaml
```

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
Two headers mapping to one field produce an error instead of a silent choice.
Product and category values retain case and have surrounding whitespace removed.
For CSV inputs, `source_row` is the logical record number including the header;
quoted fields containing line breaks can span several physical text-file lines.

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
