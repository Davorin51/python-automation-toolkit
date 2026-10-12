import json

import pytest

from automation_toolkit.cli import main
from automation_toolkit.config import Config
from automation_toolkit.exceptions import ReportError, ValidationError
from automation_toolkit.pipeline import run_pipeline


def test_end_to_end_manifest_and_no_overwrite(sales_file, tmp_path):
    output = tmp_path / "reports"
    run = run_pipeline([sales_file], output)
    manifest = json.loads((run.output_dir / "manifest.json").read_text())
    assert manifest["total_revenue_cents"] == 295000
    assert manifest["orders"] == 2
    assert len(manifest["sources"][0]["sha256"]) == 64
    assert (run.output_dir / "sales_report.pdf").exists()
    assert (run.output_dir / "sales_report.xlsx").exists()
    second = run_pipeline([sales_file], output, report_format="excel")
    assert second.output_dir != run.output_dir
    assert (run.output_dir / "sales_report.pdf").exists()
    assert not (second.output_dir / "charts").exists()


@pytest.mark.parametrize("fmt", ["excel", "pdf", "charts"])
def test_selected_formats(sales_file, tmp_path, fmt):
    run = run_pipeline([sales_file], tmp_path / "reports", report_format=fmt)
    assert (run.output_dir / "sales_report.xlsx").exists() == (fmt == "excel")
    assert (run.output_dir / "sales_report.pdf").exists() == (fmt == "pdf")


def test_unwritable_output(sales_file, tmp_path):
    target = tmp_path / "not_a_directory"
    target.write_text("keep me")
    with pytest.raises(ReportError, match="Cannot publish"):
        run_pipeline([sales_file], target)
    assert target.read_text() == "keep me"


def test_failed_report_cleans_staging(sales_file, tmp_path, monkeypatch):
    def fail(*args):
        raise PermissionError("Output is read-only")

    monkeypatch.setattr("automation_toolkit.pipeline.write_excel", fail)
    output = tmp_path / "reports"
    with pytest.raises(ReportError, match="read-only"):
        run_pipeline([sales_file], output, report_format="excel")
    assert list(output.iterdir()) == []


def test_no_usable_rows(sales_file, tmp_path):
    sales_file.write_text(sales_file.read_text().replace("2026", "oops"))
    with pytest.raises(ValidationError, match="No valid"):
        run_pipeline([sales_file], tmp_path / "reports")
    assert not (tmp_path / "reports").exists()


def test_rejected_file_pdf(sales_file, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        sales_file.read_text()
        .replace("A00", "B00")
        .replace("2026-01-05,Laptop,Electronics,2", "2026-01-05,Laptop,Electronics,-2")
    )
    result = run_pipeline(
        [sales_file, bad], tmp_path / "reports", Config(validation_policy="reject_file"), "pdf"
    )
    assert result.validation.excluded_rows == 3


def test_cli_success_and_friendly_errors(sales_file, tmp_path, capsys):
    assert (
        main(
            ["--input", str(sales_file), "--output", str(tmp_path / "reports"), "--format", "excel"]
        )
        == 0
    )
    assert "2,950.00" in capsys.readouterr().out
    assert main(["--input", str(tmp_path / "missing.csv")]) == 1
    assert main(["--input", str(sales_file), "--config", str(tmp_path / "missing.yaml")]) == 1
    with pytest.raises(ReportError):
        run_pipeline([sales_file], tmp_path, report_format="unknown")
