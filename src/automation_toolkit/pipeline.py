"""Shared orchestration with staged, per-run output publication."""

import hashlib
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from automation_toolkit import __version__
from automation_toolkit.config import Config
from automation_toolkit.exceptions import ReportError, ValidationError
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.analytics import Analytics, analyze
from automation_toolkit.processing.validation import ValidationResult, validate
from automation_toolkit.reporting.charts import generate_charts
from automation_toolkit.reporting.excel_report import write_excel
from automation_toolkit.reporting.pdf_report import write_pdf

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    output_dir: Path
    validation: ValidationResult
    analytics: Analytics


def run_pipeline(
    inputs: list[Path], output: Path, config: Config | None = None, report_format: str = "all"
) -> PipelineResult:
    config = config or Config()
    if report_format not in {"all", "excel", "pdf", "charts"}:
        raise ReportError("Report format must be all, excel, pdf or charts.")
    validation = validate(load_files(inputs, config), config)
    if validation.transactions.empty:
        raise ValidationError(
            f"No valid transactions remain: {validation.invalid_rows} invalid "
            f"rows, {len(validation.rejected_files)} rejected files. "
            "Correct the inputs or review validation_policy."
        )
    metrics = analyze(validation.transactions)
    generated_at = datetime.now(UTC)
    staging = None
    try:
        output.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".toolkit-", dir=output))
        charts = (
            generate_charts(metrics, staging / "charts", config)
            if report_format in {"all", "pdf", "charts"}
            else []
        )
        if report_format in {"all", "excel"}:
            write_excel(staging / "sales_report.xlsx", validation, metrics, config, generated_at)
        if report_format in {"all", "pdf"}:
            write_pdf(
                staging / "sales_report.pdf", validation, metrics, charts, config, generated_at
            )
        validation.errors.to_json(staging / "validation_errors.json", orient="records", indent=2)
        sources = []
        for source in inputs:
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            sources.append({"name": source.name, "sha256": digest})
        manifest = {
            "version": __version__,
            "generated_at": generated_at.isoformat(),
            "currency": config.currency,
            "policy": config.validation_policy,
            "input_rows": validation.input_rows,
            "accepted_rows": len(validation.transactions),
            "invalid_rows": validation.invalid_rows,
            "excluded_rows": validation.excluded_rows,
            "rejected_files": [Path(x).name for x in validation.rejected_files],
            "total_revenue_cents": metrics.total_revenue_cents,
            "orders": metrics.orders,
            "order_basis": metrics.order_basis,
            "sources": sources,
            "outputs": sorted(
                str(p.relative_to(staging)) for p in staging.rglob("*") if p.is_file()
            ),
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        target = output / f"run-{generated_at:%Y%m%dT%H%M%S}-{uuid4().hex[:8]}"
        staging.rename(target)
    except Exception as exc:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        raise ReportError(f"Cannot publish reports in {output}: {exc}") from exc
    logger.info("Published reports to %s; %s rows excluded", target, validation.excluded_rows)
    return PipelineResult(target, validation, metrics)
