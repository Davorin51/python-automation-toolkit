"""Measure one synthetic run. Results are environment-specific, not a performance guarantee."""

import argparse
import csv
import json
import platform
import tempfile
from pathlib import Path
from time import perf_counter

from automation_toolkit.config import Config
from automation_toolkit.ingestion import load_files
from automation_toolkit.pipeline import run_pipeline
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--format", choices=["all", "excel", "charts", "pdf"], default="all")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")
    with tempfile.TemporaryDirectory() as folder:
        base = Path(folder)
        path = base / "sales.csv"
        with path.open("w", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["date", "product", "category", "quantity", "unit_price", "order_id"])
            for i in range(args.rows):
                writer.writerow(
                    [
                        f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
                        f"Product {i % 30}",
                        f"Category {i % 5}",
                        1 + i % 4,
                        "19.99",
                        f"B{i:08d}",
                    ]
                )
        start = perf_counter()
        validation = validate(load_files([path], Config()), Config())
        metrics = analyze(validation.transactions)
        core_seconds = perf_counter() - start
        start = perf_counter()
        run = run_pipeline([path], base / "reports", report_format=args.format)
        full_seconds = perf_counter() - start
        assert len(run.validation.transactions) == args.rows
        assert run.analytics.total_revenue_cents == metrics.total_revenue_cents
        result = {
            "python": platform.python_version(),
            "platform": platform.system(),
            "architecture": platform.machine(),
            "rows": args.rows,
            "format": args.format,
            "core_seconds": round(core_seconds, 3),
            "full_pipeline_seconds": round(full_seconds, 3),
            "output_bytes": sum(p.stat().st_size for p in run.output_dir.rglob("*") if p.is_file()),
            "method": "One CSV run; core measured first; full run rereads the same file. "
            "Generation of input data excluded. Wall-clock time; no memory measurement.",
        }
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
