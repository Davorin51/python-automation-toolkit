"""Measure CSV profiling time and memory at several chunk sizes.

Generates a synthetic messy dataset (blank rows, short rows, duplicates, placeholders,
mixed types, a high-cardinality column), profiles it once per chunk size and reports
wall-clock seconds, peak tracemalloc memory and peak process RSS.

Results are environment-specific and are not a performance guarantee. Peak RSS includes
the interpreter and pandas/numpy themselves, so the baseline is reported separately;
``tracemalloc_peak_mb`` is the allocation attributable to the profiling run.
"""

from __future__ import annotations

import argparse
import csv
import json
import platform
import resource
import tempfile
import tracemalloc
from pathlib import Path
from time import perf_counter

from automation_toolkit.processing.profiling import profile_csv

COLUMNS = ["id", "order_id", "amount", "day", "category", "flag", "note"]


def generate(path: Path, rows: int) -> None:
    """Write a deterministic messy CSV: every 500th row blank, short or duplicated."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        previous: list[str] = []
        for i in range(rows):
            if i and i % 500 == 0:
                handle.write("\n")  # blank line
                continue
            if i and i % 997 == 0 and previous:
                writer.writerow(previous)  # exact duplicate
                continue
            if i and i % 601 == 0:
                writer.writerow([i, f"B{i:09d}", f"{i % 937 * 1.25:.2f}"])  # short row
                continue
            amount = "n/a" if i % 311 == 0 else f"{i % 937 * 1.25:.2f}"
            note = ["", "   ", "NA", f"note {i % 50}", "  padded "][i % 5]
            previous = [
                str(i),
                f"B{i:09d}",
                amount,
                f"2026-{1 + i % 12:02d}-{1 + i % 28:02d}",
                f"Category {i % 7}",
                "yes" if i % 2 else "no",
                note,
            ]
            writer.writerow(previous)


def rss_mb() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)


def measure(path: Path, chunk_size: int | None) -> dict[str, object]:
    # Timing and memory are measured in separate passes: tracemalloc traces every
    # allocation and slows this workload down by roughly ten times, so a run with it
    # enabled cannot be used for wall-clock numbers.
    start = perf_counter()
    profile = profile_csv(path, chunk_size=chunk_size)
    seconds = perf_counter() - start
    tracemalloc.start()
    profile_csv(path, chunk_size=chunk_size)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "chunk_size": profile.chunk_size,
        "requested_chunk_size": chunk_size,
        "chunks": profile.chunks,
        "seconds": round(seconds, 3),
        "rows_per_second": round(profile.rows / seconds) if seconds else None,
        "tracemalloc_peak_mb": round(peak / 2**20, 1),
        "process_peak_rss_mb": rss_mb(),
        "summary": profile.to_dict()["summary"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument(
        "--chunk-sizes",
        type=int,
        nargs="+",
        default=[10_000, 50_000, 200_000],
        help="Chunk sizes to measure; 0 means 'let the profiler choose'",
    )
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    if args.rows < 1:
        parser.error("--rows must be positive")

    baseline_rss = rss_mb()
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "messy.csv"
        generate(path, args.rows)
        runs = [measure(path, size or None) for size in args.chunk_sizes]
        file_bytes = path.stat().st_size

    summaries = {json.dumps(run["summary"], sort_keys=True) for run in runs}
    result = {
        "python": platform.python_version(),
        "platform": platform.system(),
        "architecture": platform.machine(),
        "rows": args.rows,
        "columns": len(COLUMNS),
        "input_bytes": file_bytes,
        "baseline_rss_mb": baseline_rss,
        "identical_across_chunk_sizes": len(summaries) == 1,
        "runs": runs,
        "method": "Synthetic messy CSV profiled per chunk size; generation excluded. "
        "Time and memory come from separate passes because tracemalloc slows the run "
        "down by about ten times. process_peak_rss_mb never shrinks, so it is "
        "cumulative across runs; tracemalloc_peak_mb is per run.",
    }
    if not result["identical_across_chunk_sizes"]:  # pragma: no cover - guards a regression
        raise SystemExit("Chunk size changed the profile summary; this is a bug.")
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
