"""Profile one or more CSV datasets from the terminal.

Prints a data-quality summary (rows, missing values, duplicate rows, inferred column
types and warnings) for every input and optionally writes the full profile as JSON.
The sales schema is *not* required: any delimited text file can be profiled.

Exit status: 0 when every file was profiled, 1 when any file could not be read
(missing, empty, undecodable or malformed) or the options are invalid. With
``--strict``, 2 when all files were profiled but at least one has warnings.
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
from automation_toolkit.processing.profiling import DatasetProfile, profile_csv
from automation_toolkit.reporting.profile_report import write_profile_json

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_WARNINGS = 2

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Profile CSV datasets: missing values, duplicates, column types and more."
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--input", type=Path, nargs="+", required=True, help="CSV files")
    parser.add_argument(
        "--config", type=Path, help="YAML config (delimiter, encoding, date_format)"
    )
    parser.add_argument("--delimiter", help="CSV delimiter (overrides csv_delimiter)")
    parser.add_argument("--encoding", help="CSV encoding (overrides csv_encoding)")
    parser.add_argument(
        "--chunk-size",
        type=int,
        metavar="ROWS",
        help="Records processed per chunk (overrides csv_chunk_size; default 50000)",
    )
    parser.add_argument(
        "--null-as-missing",
        action="store_true",
        help="Count placeholder values such as NA, null or - as missing",
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
        help="Write the profile as JSON to PATH (a list when several inputs are given)",
    )
    parser.add_argument(
        "--strict", action="store_true", help="Exit with status 2 when any profile has warnings"
    )
    parser.add_argument("--verbose", action="store_true")
    return parser


def _has_warnings(profile: DatasetProfile) -> bool:
    return bool(profile.warnings) or any(c.warnings for c in profile.column_profiles)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
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

    profiles: list[DatasetProfile] = []
    failed = False
    for path in args.input:
        try:
            profile = profile_csv(
                path,
                config,
                chunk_size=args.chunk_size,
                null_tokens=args.null_token,
                null_as_missing=args.null_as_missing,
            )
        except ToolkitError as exc:
            logger.error("%s", exc)
            failed = True
            continue
        profiles.append(profile)
        if len(profiles) > 1:
            print()
        print(profile.to_text())

    if args.json is not None and profiles:
        try:
            if len(args.input) == 1:
                write_profile_json(args.json, profiles[0])
            else:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(
                    json.dumps([p.to_dict() for p in profiles], indent=2, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
        except OSError as exc:
            logger.error("Cannot write %s: %s", args.json, exc)
            return EXIT_FAILED
        print(f"\nProfile JSON: {args.json.resolve()}")

    if failed:
        return EXIT_FAILED
    if args.strict and any(_has_warnings(p) for p in profiles):
        return EXIT_WARNINGS
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
