import argparse
import logging
from pathlib import Path

from automation_toolkit import __version__
from automation_toolkit.config import load_config
from automation_toolkit.exceptions import ToolkitError
from automation_toolkit.pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate sales data and generate business reports."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--input", type=Path, nargs="+", required=True, help="CSV/XLSX input files")
    parser.add_argument("--output", type=Path, default=Path("reports"))
    parser.add_argument("--format", choices=["all", "excel", "pdf", "charts"], default="all")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        result = run_pipeline(args.input, args.output, load_config(args.config), args.format)
    except (ToolkitError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        return 1
    print(f"Reports: {result.output_dir.resolve()}")
    print(
        f"Revenue: {result.analytics.total_revenue_cents / 100:,.2f}; "
        f"orders: {result.analytics.orders}; excluded rows: {result.validation.excluded_rows}"
    )
    return 0
