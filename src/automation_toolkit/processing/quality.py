"""Aggregated data-quality statistics derived from one validation run.

The model is a pure function of a :class:`~automation_toolkit.processing.validation.
ValidationResult` and the configuration: it never rereads inputs and never changes the
validation outcome. Statistics are grouped by check code, by business column and by
source file, and :meth:`QualityReport.to_dict` renders a JSON-ready mapping with plain
Python scalars so a later export or CLI summary can use it unchanged.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from automation_toolkit import __version__
from automation_toolkit.config import Config
from automation_toolkit.ingestion import REQUIRED, FileStats, IngestionStats
from automation_toolkit.processing.validation import (
    CHECK_DESCRIPTIONS,
    CHECK_DIMENSIONS,
    MISSING_VALUE,
    ValidationResult,
)

#: Incremented when the rendered mapping changes in a way consumers must notice.
SCHEMA_VERSION = 1
DIMENSIONS = ("completeness", "validity", "uniqueness")
EXAMPLE_LIMIT = 5
TOP_REASON_LIMIT = 10


def _percent(part: int, total: int) -> float:
    """A rounded percentage; zero input rows give ``0.0`` rather than an error."""
    return round(100.0 * part / total, 2) if total else 0.0


@dataclass(frozen=True)
class CheckSummary:
    """How often one check failed, and where."""

    code: str
    column: str
    dimension: str
    description: str
    rows: int
    percent_of_input: float
    examples: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "column": self.column,
            "dimension": self.dimension,
            "description": self.description,
            "rows": self.rows,
            "percent_of_input": self.percent_of_input,
            "examples": [dict(example) for example in self.examples],
        }


@dataclass(frozen=True)
class ColumnSummary:
    """Per-business-column completeness and validity."""

    column: str
    missing: int
    invalid: int
    issue_rows: int
    complete_percent: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "missing": self.missing,
            "invalid": self.invalid,
            "issue_rows": self.issue_rows,
            "complete_percent": self.complete_percent,
        }


@dataclass(frozen=True)
class FileSummary:
    """Per-source-file row accounting, including whole-file rejection.

    ``ingestion`` carries how the file was read (records, blank rows, chunks, checkpoint
    resume) when the caller supplied :class:`IngestionStats`; otherwise it is ``None``.
    """

    source_file: str
    file_name: str
    input_rows: int
    accepted_rows: int
    invalid_rows: int
    excluded_rows: int
    rejected: bool
    issues: int
    ingestion: FileStats | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "file_name": self.file_name,
            "input_rows": self.input_rows,
            "accepted_rows": self.accepted_rows,
            "invalid_rows": self.invalid_rows,
            "excluded_rows": self.excluded_rows,
            "rejected": self.rejected,
            "issues": self.issues,
            "ingestion": self.ingestion.to_dict() if self.ingestion is not None else None,
        }


@dataclass(frozen=True)
class QualitySummary:
    """Run-level counts. ``total_issues`` counts failed checks, not rows."""

    files: int
    rejected_files: int
    input_rows: int
    accepted_rows: int
    invalid_rows: int
    excluded_rows: int
    accepted_percent: float
    invalid_percent: float
    excluded_percent: float
    total_issues: int
    rows_with_multiple_issues: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "rejected_files": self.rejected_files,
            "input_rows": self.input_rows,
            "accepted_rows": self.accepted_rows,
            "invalid_rows": self.invalid_rows,
            "excluded_rows": self.excluded_rows,
            "accepted_percent": self.accepted_percent,
            "invalid_percent": self.invalid_percent,
            "excluded_percent": self.excluded_percent,
            "total_issues": self.total_issues,
            "rows_with_multiple_issues": self.rows_with_multiple_issues,
        }


@dataclass(frozen=True)
class QualityReport:
    """The complete data-quality picture of one validation run."""

    summary: QualitySummary
    dimensions: dict[str, dict[str, Any]]
    checks: list[CheckSummary]
    columns: list[ColumnSummary]
    files: list[FileSummary]
    top_reasons: list[dict[str, Any]]
    policy: str
    settings: dict[str, Any]
    generated_at: datetime | None = None
    #: Run-level reading statistics; ``None`` when the caller did not supply them.
    ingestion: IngestionStats | None = None
    schema_version: int = SCHEMA_VERSION
    toolkit_version: str = __version__

    def to_dict(self) -> dict[str, Any]:
        """A JSON-ready mapping of plain scalars, deterministic for identical input."""
        return {
            "schema_version": self.schema_version,
            "toolkit_version": self.toolkit_version,
            "generated_at": self.generated_at.isoformat() if self.generated_at else None,
            "policy": self.policy,
            "settings": dict(self.settings),
            "summary": self.summary.to_dict(),
            "ingestion": self.ingestion.to_dict() if self.ingestion is not None else None,
            "dimensions": {name: dict(value) for name, value in self.dimensions.items()},
            "by_check": [check.to_dict() for check in self.checks],
            "by_column": [column.to_dict() for column in self.columns],
            "by_file": [file.to_dict() for file in self.files],
            "top_reasons": [dict(reason) for reason in self.top_reasons],
        }


def _business_columns(validation: ValidationResult) -> list[str]:
    """The required fields, plus ``order_id`` when the run carries one."""
    known = set(validation.transactions.columns) | set(validation.errors.columns)
    if not validation.checks.empty:
        known |= set(validation.checks["column"])
    return [*REQUIRED, *(["order_id"] if "order_id" in known else [])]


def _examples(checks: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` locations, ordered by file and row so the output is diff-stable."""
    ordered = checks.sort_values(["source_file", "source_row"], kind="stable").head(limit)
    return [
        {"source_file": str(row.source_file), "source_row": int(row.source_row)}
        for row in ordered.itertuples(index=False)
    ]


def _check_summaries(
    checks: pd.DataFrame, input_rows: int, example_limit: int
) -> list[CheckSummary]:
    if checks.empty:
        return []
    summaries = [
        CheckSummary(
            code=str(code),
            column=str(column),
            dimension=CHECK_DIMENSIONS.get(str(code), "validity"),
            description=CHECK_DESCRIPTIONS.get(str(code), str(code)),
            rows=len(group),
            percent_of_input=_percent(len(group), input_rows),
            examples=_examples(group, example_limit),
        )
        for (code, column), group in checks.groupby(["code", "column"], sort=False)
    ]
    # Most frequent first; ties resolve by code and column so the order never depends on
    # the order rows happened to arrive in.
    return sorted(summaries, key=lambda s: (-s.rows, s.code, s.column))


def _column_summaries(
    checks: pd.DataFrame, columns: list[str], input_rows: int
) -> list[ColumnSummary]:
    summaries = []
    for column in columns:
        hits = checks[checks["column"] == column] if not checks.empty else checks
        missing = int((hits["code"] == MISSING_VALUE).sum()) if not hits.empty else 0
        summaries.append(
            ColumnSummary(
                column=column,
                missing=missing,
                invalid=len(hits) - missing,
                issue_rows=int(hits[["source_file", "source_row"]].drop_duplicates().shape[0])
                if not hits.empty
                else 0,
                complete_percent=_percent(input_rows - missing, input_rows),
            )
        )
    return summaries


def _file_summaries(
    validation: ValidationResult, checks: pd.DataFrame, ingestion: IngestionStats | None
) -> list[FileSummary]:
    rejected = set(validation.rejected_files)
    read_stats = ingestion.by_source() if ingestion is not None else {}
    invalid_per_file = (
        validation.errors["source_file"].value_counts() if len(validation.errors) else pd.Series()
    )
    issues_per_file = checks["source_file"].value_counts() if not checks.empty else pd.Series()
    summaries = []
    for source, rows in validation.file_rows.items():
        invalid = int(invalid_per_file.get(source, 0))
        # Under reject_file every row of an affected file is excluded, including its
        # valid ones; under the other policies only the invalid rows are.
        excluded = rows if source in rejected else invalid
        summaries.append(
            FileSummary(
                source_file=source,
                file_name=Path(source).name,
                input_rows=rows,
                accepted_rows=rows - excluded,
                invalid_rows=invalid,
                excluded_rows=excluded,
                rejected=source in rejected,
                issues=int(issues_per_file.get(source, 0)),
                ingestion=read_stats.get(source),
            )
        )
    return summaries


def _dimensions(checks: pd.DataFrame, input_rows: int) -> dict[str, dict[str, Any]]:
    """Distinct affected rows per quality dimension.

    A row failing checks in two dimensions counts in both, so these totals can add up to
    more than ``invalid_rows``.
    """
    result = {}
    for dimension in DIMENSIONS:
        if checks.empty:
            rows = 0
        else:
            codes = [c for c, d in CHECK_DIMENSIONS.items() if d == dimension]
            hits = checks[checks["code"].isin(codes)]
            rows = int(hits[["source_file", "source_row"]].drop_duplicates().shape[0])
        result[dimension] = {"rows": rows, "percent": _percent(rows, input_rows)}
    return result


def _top_reasons(errors: pd.DataFrame, limit: int) -> list[dict[str, Any]]:
    """The most common verbatim reason texts, reconcilable with the error diagnostics."""
    if not len(errors):
        return []
    counts = errors["reason"].value_counts()
    ordered = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [{"reason": str(reason), "rows": int(rows)} for reason, rows in ordered[:limit]]


def build_quality_report(
    validation: ValidationResult,
    config: Config,
    generated_at: datetime | None = None,
    example_limit: int = EXAMPLE_LIMIT,
    top_reason_limit: int = TOP_REASON_LIMIT,
    ingestion: IngestionStats | None = None,
) -> QualityReport:
    """Summarize ``validation`` by check, column and source file.

    Percentages use the validated input rows (blank rows already removed) as the
    denominator and are rounded to two decimals; with no input rows they are ``0.0``.
    ``ingestion`` (from :func:`~automation_toolkit.ingestion.load_files_with_stats`) adds
    records read, blank rows skipped, chunk counts and checkpoint-resume information at
    run level and per file; it must describe the same files the validation came from,
    and a file without statistics simply gets ``ingestion: None``.
    """
    if ingestion is not None:
        unknown = set(ingestion.by_source()) - set(validation.file_rows)
        if unknown:
            raise ValueError(
                "Ingestion statistics name files that are not in the validation result: "
                + ", ".join(sorted(unknown))
            )
    checks = validation.checks
    input_rows = validation.input_rows
    if not checks.empty:
        per_row = checks.groupby(["source_file", "source_row"], sort=False).size()
        multiple = int((per_row > 1).sum())
    else:
        multiple = 0
    summary = QualitySummary(
        files=len(validation.file_rows),
        rejected_files=len(validation.rejected_files),
        input_rows=input_rows,
        accepted_rows=len(validation.transactions),
        invalid_rows=validation.invalid_rows,
        excluded_rows=validation.excluded_rows,
        accepted_percent=_percent(len(validation.transactions), input_rows),
        invalid_percent=_percent(validation.invalid_rows, input_rows),
        excluded_percent=_percent(validation.excluded_rows, input_rows),
        total_issues=len(checks),
        rows_with_multiple_issues=multiple,
    )
    return QualityReport(
        summary=summary,
        dimensions=_dimensions(checks, input_rows),
        checks=_check_summaries(checks, input_rows, example_limit),
        columns=_column_summaries(checks, _business_columns(validation), input_rows),
        files=_file_summaries(validation, checks, ingestion),
        top_reasons=_top_reasons(validation.errors, top_reason_limit),
        policy=config.validation_policy,
        settings={
            "date_format": config.date_format,
            "csv_chunk_size": config.csv_chunk_size,
            "checkpointing": config.csv_checkpoint_dir is not None,
        },
        generated_at=generated_at,
        ingestion=ingestion,
    )
