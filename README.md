# Python Automation Toolkit

**Automated Data Processing, Analytics & Reporting System**

[![Tests](https://github.com/Davorin51/python-automation-toolkit/actions/workflows/tests.yml/badge.svg)](https://github.com/Davorin51/python-automation-toolkit/actions/workflows/tests.yml)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

Turn daily CSV and Excel sales exports into validated datasets, business metrics,
styled Excel dashboards, PDF reports and charts through one configurable workflow.
Built as a portfolio example of modular Python automation with tests and CI.

![Generated sales report](docs/screenshots/report-preview.png)

## See the result without installing

- [Three-page PDF report](examples/generated/sales_report.pdf)
- [Excel workbook with eight worksheets](examples/generated/sales_report.xlsx)
- [Validation diagnostics](examples/generated/validation_errors.json)
- [Run manifest and source checksum](examples/generated/manifest.json)
- [Excel dashboard preview](docs/screenshots/excel-dashboard.png)

Every bundled business record is **synthetic**. The CSV and XLSX examples contain
the same data. The dirty fixture adds realistic validation failures.

## What it automates

A sales team receives exports from several sources and manually combines files,
checks errors, calculates totals and prepares management reports. This toolkit
performs that batch workflow and keeps rejected-row diagnostics for review.

| Capability | Implementation |
| --- | --- |
| CSV and XLSX input | Multiple files, header aliases, explicit mappings and provenance |
| Data quality | Invalid dates, missing fields, negative/fractional quantities, invalid prices, duplicates |
| Validation policy | Quarantine raw rows, skip with diagnostics, or reject the entire affected file |
| Business metrics | Revenue, orders, average order value, units, category/product breakdowns |
| Trends | Calendar months, Sunday-ending weeks and month-over-month percentage change |
| Excel reports | Summary, Transactions, Categories, Products, Monthly Trends, Weekly Trends, Period Comparison, Validation Errors |
| PDF and charts | Cover with KPIs, four charts, data-quality counts and generation date |
| Interfaces | CLI and optional CustomTkinter desktop app share one pipeline |
| Run integrity | Staged publication, unique output folders and a manifest with source checksums |
| Developer workflow | Type hints, logging, pytest, Ruff, coverage gate and GitHub Actions |

## Quick start

```bash
git clone https://github.com/Davorin51/python-automation-toolkit.git
cd python-automation-toolkit
python -m venv .venv
```

Activate the virtual environment:

```bash
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install and generate the complete demo:

```bash
python -m pip install -e .
python -m automation_toolkit --input examples/sample_sales_dirty.csv --output reports --format all --config config.yaml
```

The command prints a completed run folder under `reports/`. It contains an Excel
workbook, a PDF, four PNG charts, `validation_errors.json` and `manifest.json`.
Repeated runs create separate folders and preserve earlier reports.

### Expected demo results

| Metric | Value |
| --- | ---: |
| Input rows in dirty fixture | 357 |
| Accepted transaction lines | 350 |
| Distinct orders | 174 |
| Units sold | 1,570 |
| Revenue | EUR 249,641.98 |
| Average order value | EUR 1,434.72 |
| Invalid/duplicate rows excluded | 7 |

The control totals in [expected_results.json](examples/expected_results.json) are
checked against all three fixtures by automated tests. Prices are calculated in
integer cents. `order_id` is optional: without it, one row is assumed to be one
order, and that assumption is displayed in the report.

### More examples

```bash
# One Excel input, only Excel output
automation-toolkit --input examples/sample_sales.xlsx --output reports --format excel

# Combine separate exports (the schemas must agree on order_id)
automation-toolkit --input january.csv february.xlsx --output reports --format all

# Install and launch the optional desktop application
python -m pip install -e '.[gui]'
automation-toolkit-gui
```

The GUI needs a desktop environment with Tk. Its business workflow is covered by
the shared pipeline tests; interactive GUI behavior requires manual verification.
See [usage and configuration](docs/usage.md) for delimiters, date formats, sheet
selection, mappings, exit codes and troubleshooting.

## Input schema

| Field | Rule |
| --- | --- |
| Date | ISO date by default; configurable date format or native Excel date |
| Product | Required non-empty text |
| Category | Required non-empty text |
| Quantity | Positive whole number |
| Unit Price | Non-negative numeric price, at most two decimal places |
| Order ID | Optional text identifier; required on every row when the column exists |

All records in a run use one currency. The currency config is a label, not an
exchange-rate converter. Duplicate detection uses all normalized business fields,
including order ID when present. Use different IDs for separate purchases with
otherwise identical data. Identical legitimate lines within one order require a
future transaction-line-ID extension. Details and limits are documented in
[architecture.md](docs/architecture.md).

## Quality checks

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest --cov --cov-report=term-missing
python -m build
```

CI runs linting, formatting, tests, an **80% coverage gate**, package builds and a
complete demo on Python 3.11, 3.12 and 3.13. Coverage excludes the optional GUI and
the module entry-point wrapper. Regression and integration tests check arithmetic,
cross-file duplicates, missing months, zero denominators, Excel native dates,
report contents, literal Excel strings, invalid configuration and failed writes.

## Measured 100,000-row example

```bash
python scripts/benchmark.py --rows 100000 --format all --json docs/benchmark.json
```

The included [benchmark result](docs/benchmark.json) records one Linux x86_64 run
on Python 3.12.14. Core ingestion, validation and analytics took approximately
**1.6 seconds**; a full run with Excel, PDF and four charts took approximately
**49 seconds**. These are environment-specific wall-clock measurements. The full
run rereads the same input after the core measurement and may benefit from file
caching. No memory measurement or universal throughput claim is made.

The implementation holds input data in memory. Excel's worksheet row limit still
applies. Test the benchmark on the deployment machine before sizing a workflow.

## Project structure

```text
src/automation_toolkit/
  cli.py, config.py, pipeline.py
  ingestion/       CSV/XLSX readers and schema normalization
  processing/      Cleaning, validation and business analytics
  reporting/       Excel, PDF and charts
  gui/             Optional desktop adapter
tests/             Unit, regression and end-to-end tests
examples/          Synthetic data, control totals and generated reports
scripts/           Deterministic fixture generation and benchmark
docs/              Architecture, usage, measurements and previews
.github/workflows/ Automated quality checks
```

See [architecture](docs/architecture.md), [contribution guidelines](CONTRIBUTING.md)
and [changelog](CHANGELOG.md). Planned extensions include configurable transaction
line IDs, chunked ingestion and additional business schemas.

## License

[MIT](LICENSE) - Davorin Milicevic. Available for study, adaptation and commercial use.
