"""Opt-in data-quality export through the pipeline and the CLI."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import automation_toolkit.pipeline as pipeline_module
from automation_toolkit.cli import main
from automation_toolkit.config import Config, load_config
from automation_toolkit.exceptions import ConfigurationError, ReportError
from automation_toolkit.ingestion import load_files
from automation_toolkit.pipeline import run_pipeline
from automation_toolkit.processing.quality import build_quality_report
from automation_toolkit.processing.validation import DUPLICATE_LINE, validate
from automation_toolkit.reporting.quality_report import FILENAME, write_quality_json

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DIRTY = EXAMPLES / "sample_sales_dirty.csv"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- Disabled (default) -----------------------------------------------------------


def test_quality_report_is_off_by_default(sales_file, tmp_path):
    run = run_pipeline([sales_file], tmp_path / "reports", report_format="excel")
    assert run.quality is None
    assert not (run.output_dir / FILENAME).exists()
    manifest = read_json(run.output_dir / "manifest.json")
    assert manifest["outputs"] == ["sales_report.xlsx", "validation_errors.json"]
    assert Config().quality_report is False


def test_cli_without_flag_writes_no_quality_file(sales_file, tmp_path, capsys):
    output = tmp_path / "reports"
    assert main(["--input", str(sales_file), "--output", str(output), "--format", "excel"]) == 0
    assert "Data quality" not in capsys.readouterr().out
    assert not list(output.rglob(FILENAME))


# --- Enabled through configuration -------------------------------------------------


@pytest.mark.parametrize("fmt", ["all", "excel", "pdf", "charts"])
def test_enabled_report_is_written_for_every_format(sales_file, tmp_path, fmt):
    config = Config(quality_report=True)
    run = run_pipeline([sales_file], tmp_path / "reports", config, fmt)
    assert run.quality is not None
    path = run.output_dir / FILENAME
    assert path.exists()
    manifest = read_json(run.output_dir / "manifest.json")
    assert FILENAME in manifest["outputs"]
    payload = read_json(path)
    assert payload == run.quality.to_dict()
    assert payload["generated_at"] == manifest["generated_at"]
    assert payload["summary"]["input_rows"] == manifest["input_rows"]
    assert payload["summary"]["accepted_rows"] == manifest["accepted_rows"]
    assert payload["summary"]["invalid_rows"] == 0
    assert payload["by_check"] == []


def test_report_uses_the_existing_validation_result_without_rereading(
    sales_file, tmp_path, monkeypatch
):
    loads = []
    real_load = pipeline_module.load_files_with_stats
    real_build = pipeline_module.build_quality_report
    seen = {}

    def counting_load(paths, config):
        loads.append(list(paths))
        frame, stats = real_load(paths, config)
        seen["stats"] = stats
        return frame, stats

    def recording_build(validation, config, generated_at=None, **kwargs):
        seen["validation"] = validation
        seen["ingestion"] = kwargs.get("ingestion")
        return real_build(validation, config, generated_at, **kwargs)

    monkeypatch.setattr(pipeline_module, "load_files_with_stats", counting_load)
    monkeypatch.setattr(pipeline_module, "build_quality_report", recording_build)
    run = run_pipeline([sales_file], tmp_path / "reports", Config(quality_report=True), "excel")
    assert loads == [[sales_file]]
    assert seen["validation"] is run.validation
    # The reading statistics come from that single load, not from a second pass.
    assert seen["ingestion"] is seen["stats"]
    assert run.quality.ingestion is seen["stats"]


def test_enabling_the_report_does_not_change_validation_or_other_outputs(sales_file, tmp_path):
    plain = run_pipeline([sales_file], tmp_path / "a", Config(), "excel")
    with_quality = run_pipeline([sales_file], tmp_path / "b", Config(quality_report=True), "excel")
    assert plain.validation.input_rows == with_quality.validation.input_rows
    assert plain.validation.errors.equals(with_quality.validation.errors)
    assert plain.validation.transactions.equals(with_quality.validation.transactions)
    plain_manifest = read_json(plain.output_dir / "manifest.json")
    quality_manifest = read_json(with_quality.output_dir / "manifest.json")
    for key in ("input_rows", "accepted_rows", "invalid_rows", "excluded_rows", "orders"):
        assert plain_manifest[key] == quality_manifest[key]
    assert set(quality_manifest["outputs"]) - set(plain_manifest["outputs"]) == {FILENAME}


# --- Invalid records and reject_file ---------------------------------------------


def test_invalid_records_in_exported_json(tmp_path):
    run = run_pipeline([DIRTY], tmp_path / "reports", Config(quality_report=True), "excel")
    payload = read_json(run.output_dir / FILENAME)
    assert payload["summary"]["input_rows"] == 357
    assert payload["summary"]["invalid_rows"] == 7
    assert payload["summary"]["excluded_rows"] == 7
    assert payload["summary"]["accepted_percent"] == 98.04
    assert payload["summary"]["total_issues"] == 7
    by_check = {(c["code"], c["column"]): c["rows"] for c in payload["by_check"]}
    assert by_check == {
        (DUPLICATE_LINE, ""): 2,
        ("invalid_date", "date"): 1,
        ("invalid_price", "unit_price"): 1,
        ("invalid_quantity", "quantity"): 1,
        ("missing_value", "product"): 1,
        ("missing_value", "unit_price"): 1,
    }
    assert payload["dimensions"]["uniqueness"]["rows"] == 2
    assert payload["by_file"][0]["file_name"] == "sample_sales_dirty.csv"
    assert payload["by_file"][0]["rejected"] is False
    # Every location in the quality report is also in the error diagnostics.
    errors = read_json(run.output_dir / "validation_errors.json")
    error_rows = {(e["source_file"], e["source_row"]) for e in errors}
    for check in payload["by_check"]:
        for example in check["examples"]:
            assert (example["source_file"], example["source_row"]) in error_rows
    assert sum(r["rows"] for r in payload["top_reasons"]) == len(errors)


def test_reject_file_policy_in_exported_json(sales_file, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(
        sales_file.read_text()
        .replace("A00", "B00")
        .replace("2026-01-05,Laptop,Electronics,2", "2026-01-05,Laptop,Electronics,-2")
    )
    config = Config(validation_policy="reject_file", quality_report=True)
    run = run_pipeline([sales_file, bad], tmp_path / "reports", config, "excel")
    payload = read_json(run.output_dir / FILENAME)
    manifest = read_json(run.output_dir / "manifest.json")
    assert payload["policy"] == "reject_file"
    assert payload["summary"]["rejected_files"] == 1
    assert payload["summary"]["excluded_rows"] == 3 == manifest["excluded_rows"]
    assert payload["summary"]["invalid_rows"] == 1 == manifest["invalid_rows"]
    assert payload["summary"]["accepted_rows"] == 3 == manifest["accepted_rows"]
    good, rejected = payload["by_file"]
    assert (good["file_name"], good["rejected"], good["excluded_rows"]) == ("sales.csv", False, 0)
    assert (rejected["file_name"], rejected["rejected"]) == ("bad.csv", True)
    assert (rejected["input_rows"], rejected["invalid_rows"], rejected["excluded_rows"]) == (
        3,
        1,
        3,
    )
    assert rejected["accepted_rows"] == 0
    assert manifest["rejected_files"] == ["bad.csv"]


# --- JSON writer ------------------------------------------------------------------


def test_write_quality_json_format_and_unicode(tmp_path):
    path = tmp_path / "ventes-été.csv"
    path.write_text(
        "date,product,category,quantity,unit_price\n2026-01-05,Café,Food,2,-1\n",
        encoding="utf-8",
    )
    config = Config(validation_policy="quarantine")
    result = validate(load_files([path], config), config)
    report = build_quality_report(result, config, datetime(2026, 11, 8, tzinfo=UTC))
    target = tmp_path / "out" / FILENAME
    target.parent.mkdir()
    write_quality_json(target, report)
    text = target.read_text(encoding="utf-8")
    assert text.endswith("}\n")
    assert text.startswith('{\n  "schema_version": 1,')
    assert "ventes-été.csv" in text and "\\u00e9" not in text
    payload = json.loads(text)
    assert payload == report.to_dict()
    assert payload["generated_at"] == "2026-11-08T00:00:00+00:00"
    write_quality_json(target, report)
    assert target.read_text(encoding="utf-8") == text


def test_failed_quality_export_is_a_report_error_and_cleans_staging(
    sales_file, tmp_path, monkeypatch
):
    def fail(path, report):
        raise OSError("disk full")

    monkeypatch.setattr(pipeline_module, "write_quality_json", fail)
    output = tmp_path / "reports"
    with pytest.raises(ReportError, match="disk full"):
        run_pipeline([sales_file], output, Config(quality_report=True), "excel")
    assert list(output.iterdir()) == []


# --- CLI and configuration ----------------------------------------------------------


def test_cli_flag_enables_report_and_prints_summary(tmp_path, capsys):
    output = tmp_path / "reports"
    code = main(
        ["--input", str(DIRTY), "--output", str(output), "--format", "excel", "--quality-report"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert (
        "Data quality: 98.04% accepted (350/357); 7 invalid; top issue: duplicate_line (2)" in out
    )
    files = list(output.rglob(FILENAME))
    assert len(files) == 1
    assert read_json(files[0])["summary"]["invalid_rows"] == 7


def test_cli_summary_for_clean_and_rejected_inputs(sales_file, tmp_path, capsys):
    output = tmp_path / "reports"
    assert (
        main(
            [
                "--input",
                str(sales_file),
                "--output",
                str(output),
                "--format",
                "excel",
                "--quality-report",
            ]
        )
        == 0
    )
    assert "Data quality: 100.00% accepted (3/3); 0 invalid\n" in capsys.readouterr().out
    bad = tmp_path / "bad.csv"
    bad.write_text(sales_file.read_text().replace("A00", "B00").replace(",2,900", ",-2,900"))
    (tmp_path / "cfg.yaml").write_text("validation_policy: reject_file\nquality_report: true\n")
    assert (
        main(
            [
                "--input",
                str(sales_file),
                str(bad),
                "--output",
                str(output),
                "--format",
                "excel",
                "--config",
                str(tmp_path / "cfg.yaml"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "3 invalid" not in out
    assert "; 1 invalid; 1 rejected file(s); top issue: invalid_quantity in quantity (1)" in out


def test_quality_report_config_key_is_validated(tmp_path):
    (tmp_path / "ok.yaml").write_text("quality_report: true\n")
    assert load_config(tmp_path / "ok.yaml").quality_report is True
    (tmp_path / "bad.yaml").write_text("quality_report: yes please\n")
    with pytest.raises(ConfigurationError, match="quality_report"):
        load_config(tmp_path / "bad.yaml")
    with pytest.raises(ConfigurationError, match="quality_report"):
        Config(quality_report=1)
