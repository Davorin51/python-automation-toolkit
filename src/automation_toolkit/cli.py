import argparse
import logging
from dataclasses import replace
from pathlib import Path

from automation_toolkit import __version__
from automation_toolkit.config import load_config
from automation_toolkit.exceptions import ToolkitError
from automation_toolkit.pipeline import run_pipeline
from automation_toolkit.processing.quality import QualityReport


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate sales data and generate business reports."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--input", type=Path, nargs="+", required=True, help="CSV/XLSX input files")
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--format", choices=["all", "excel", "pdf", "charts"], default="all")
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--chunk-size",
        type=int,
        metavar="ROWS",
        help="Read CSV inputs incrementally in chunks of ROWS records (overrides csv_chunk_size)",
    )
    parser.add_argument(
        "--checkpoint-dir",
        type=Path,
        metavar="DIR",
        help="Save chunked CSV progress under DIR and resume an interrupted run from it",
    )
    parser.add_argument(
        "--quality-report",
        action="store_true",
        help="Also write data_quality.json with validation statistics by check, column and file",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        config = load_config(args.config)
        if args.chunk_size is not None:
            config = replace(config, csv_chunk_size=args.chunk_size)
        if args.checkpoint_dir is not None:
            config = replace(config, csv_checkpoint_dir=str(args.checkpoint_dir))
        if args.quality_report:
            config = replace(config, quality_report=True)
        result = run_pipeline(args.input, args.output, config, args.format)
    except (ToolkitError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    print(f"Reports: {result.output_dir.resolve()}")
    print(
        f"Revenue: {result.analytics.total_revenue_cents / 100:,.2f}; "
        f"orders: {result.analytics.orders}; excluded rows: {result.validation.excluded_rows}"
    )
    if result.quality is not None:
        print(quality_summary(result.quality))
    return 0


def quality_summary(report: QualityReport) -> str:
    """One console line: acceptance rate, invalid rows and the most frequent check."""
    summary = report.summary
    text = (
        f"Data quality: {summary.accepted_percent:.2f}% accepted "
        f"({summary.accepted_rows}/{summary.input_rows}); {summary.invalid_rows} invalid"
    )
    if summary.rejected_files:
        text += f"; {summary.rejected_files} rejected file(s)"
    if report.ingestion is not None:
        if report.ingestion.blank_rows:
            text += f"; {report.ingestion.blank_rows} blank row(s) skipped"
        if report.ingestion.resumed_files:
            text += f"; {report.ingestion.resumed_files} file(s) resumed from checkpoint"
    if report.checks:
        top = report.checks[0]
        where = f" in {top.column}" if top.column else ""
        text += f"; top issue: {top.code}{where} ({top.rows})"
    return text
