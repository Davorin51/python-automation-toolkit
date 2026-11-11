"""Scan one or more CSV datasets for outliers from the terminal.

Prints, for every numeric column, the normal range and the values outside it, with row
numbers, and optionally writes the full result as JSON. The sales schema is *not*
required: any delimited text file can be scanned. ``--method`` picks the fence: ``iqr``
(the default, quartiles widened by ``--factor``) or ``modified_zscore`` (values whose
modified z-score ``0.6745 * (x - median) / MAD`` exceeds ``--threshold``).

Exit status: 0 when every file was scanned, 1 when any file could not be read
(missing, empty, undecodable or malformed) or the options are invalid. With
``--strict``, 2 when all files were scanned but outliers were found in at least one.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from automation_toolkit import __version__
from automation_toolkit.config import check_chunk_size, load_config
from automation_toolkit.exceptions import ToolkitError
from automation_toolkit.processing.anomalies import (
    DEFAULT_IQR_FACTOR,
    DEFAULT_ZSCORE_THRESHOLD,
    METHODS,
    AnomalyReport,
    detect_outliers_csv,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_OUTLIERS = 2

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect outliers in the numeric columns of CSV datasets."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--input", type=Path, nargs="+", required=True, help="CSV files")
    parser.add_argument("--config", type=Path, help="YAML config (delimiter, encoding, chunk size)")
    parser.add_argument("--delimiter", help="CSV delimiter (overrides csv_delimiter)")
    parser.add_argument("--encoding", help="CSV encoding (overrides csv_encoding)")
    parser.add_argument(
        "--chunk-size",
        type=int,
        metavar="ROWS",
        help="Records processed per chunk (overrides csv_chunk_size; default 50000)",
    )
    parser.add_argument(
        "--method",
        choices=METHODS,
        default="iqr",
        help="How to draw the fence: Tukey's IQR (default) or the modified z-score "
        "(median and MAD based; sturdier when many values are extreme)",
    )
    parser.add_argument(
        "--factor",
        type=float,
        default=None,
        metavar="F",
        help="IQR fence multiplier: outliers lie outside [Q1 - F*IQR, Q3 + F*IQR] "
        "(default 1.5; larger keeps more values; iqr method only)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        metavar="T",
        help="Modified z-score cutoff: outliers have |0.6745*(x-median)/MAD| > T "
        "(default 3.5; larger keeps more values; modified_zscore method only)",
    )
    parser.add_argument(
        "--column",
        action="append",
        default=None,
        metavar="NAME",
        help="Scan only this column, even if it is mostly non-numeric (repeatable)",
    )
    parser.add_argument(
        "--null-token",
        action="append",
        default=[],
        metavar="TEXT",
        help="Extra placeholder text treated like NA (repeatable, case-insensitive)",
    )
    parser.add_argument(
        "--json",
        type=Path,
        metavar="PATH",
        help="Write the result as JSON to PATH (a list when several inputs are given)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="Exit with status 2 when any outliers are found"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.factor is not None and args.method != "iqr":
        logger.error("--factor applies to --method iqr; use --threshold instead.")
        return EXIT_FAILED
    if args.threshold is not None and args.method != "modified_zscore":
        logger.error("--threshold applies to --method modified_zscore; use --factor instead.")
        return EXIT_FAILED
    try:
        config = load_config(args.config)
        if args.delimiter is not None:
            config = replace(config, csv_delimiter=args.delimiter)
        if args.encoding is not None:
            config = replace(config, csv_encoding=args.encoding)
        if args.chunk_size is not None:
            check_chunk_size(args.chunk_size)
    except (ToolkitError, ValueError) as exc:
        logger.error("%s", exc)
        return EXIT_FAILED

    reports: list[AnomalyReport] = []
    failed = False
    for path in args.input:
        try:
            report = detect_outliers_csv(
                path,
                config,
                factor=DEFAULT_IQR_FACTOR if args.factor is None else args.factor,
                method=args.method,
                threshold=(DEFAULT_ZSCORE_THRESHOLD if args.threshold is None else args.threshold),
                columns=args.column,
                chunk_size=args.chunk_size,
                null_tokens=args.null_token,
            )
        except (ToolkitError, ValueError) as exc:
            logger.error("%s", exc)
            failed = True
            continue
        reports.append(report)
        if len(reports) > 1:
            print()
        print(report.to_text())

    if args.json is not None and reports:
        try:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                reports[0].to_dict() if len(args.input) == 1 else [r.to_dict() for r in reports]
            )
            args.json.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logger.error("Cannot write %s: %s", args.json, exc)
            return EXIT_FAILED
        print(f"\nOutlier JSON: {args.json.resolve()}")

    if failed:
        return EXIT_FAILED
    if args.strict and any(r.total_outliers for r in reports):
        return EXIT_OUTLIERS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
