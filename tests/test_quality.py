"""Data-quality statistics: clean data, several issues per row and invalid records."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.quality import SCHEMA_VERSION, build_quality_report
from automation_toolkit.processing.validation import (
    DUPLICATE_LINE,
    INVALID_DATE,
    INVALID_PRICE,
    INVALID_QUANTITY,
    MISSING_VALUE,
    validate,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def report_for(frame: pd.DataFrame, config: Config | None = None):
    config = config or Config()
    return build_quality_report(validate(frame, config), config)


def counts_by_check(report) -> dict[tuple[str, str], int]:
    return {(c.code, c.column): c.rows for c in report.checks}


# --- Clean data ------------------------------------------------------------------


def test_clean_data_reports_no_issues(sales_file):
    report = report_for(load_files([sales_file], Config()))
    assert report.summary.input_rows == 3
    assert report.summary.accepted_rows == 3
    assert report.summary.invalid_rows == 0
    assert report.summary.excluded_rows == 0
    assert report.summary.accepted_percent == 100.0
    assert report.summary.invalid_percent == 0.0
    assert report.summary.total_issues == 0
    assert report.summary.rows_with_multiple_issues == 0
    assert report.checks == []
    assert report.top_reasons == []
    assert all(d == {"rows": 0, "percent": 0.0} for d in report.dimensions.values())
    assert [c.column for c in report.columns] == [
        "date",
        "product",
        "category",
        "quantity",
        "unit_price",
        "order_id",
    ]
    assert all(c.complete_percent == 100.0 and c.issue_rows == 0 for c in report.columns)


def test_clean_data_without_order_id_omits_the_column(sales_file):
    raw = load_files([sales_file], Config()).drop(columns="order_id")
    report = report_for(raw)
    assert [c.column for c in report.columns] == [
        "date",
        "product",
        "category",
        "quantity",
        "unit_price",
    ]


# --- Several issues in one row ---------------------------------------------------


def test_multiple_issues_in_one_row_are_counted_per_check(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, ["date", "quantity", "unit_price", "product"]] = ["nope", "x", "", " "]
    report = report_for(raw)
    assert report.summary.invalid_rows == 1
    assert report.summary.total_issues == 4
    assert report.summary.rows_with_multiple_issues == 1
    assert counts_by_check(report) == {
        (INVALID_DATE, "date"): 1,
        (INVALID_QUANTITY, "quantity"): 1,
        (MISSING_VALUE, "unit_price"): 1,
        (MISSING_VALUE, "product"): 1,
    }
    assert report.dimensions["completeness"] == {"rows": 1, "percent": 33.33}
    assert report.dimensions["validity"] == {"rows": 1, "percent": 33.33}
    assert report.dimensions["uniqueness"] == {"rows": 0, "percent": 0.0}


def test_checks_follow_the_reason_order_for_one_row(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, ["date", "quantity", "unit_price", "product"]] = ["nope", "x", "", " "]
    result = validate(raw, Config())
    assert result.checks["code"].tolist() == [
        INVALID_DATE,
        INVALID_QUANTITY,
        MISSING_VALUE,
        MISSING_VALUE,
    ]
    assert result.checks["column"].tolist() == ["date", "quantity", "unit_price", "product"]
    assert result.checks["source_row"].tolist() == [2, 2, 2, 2]
    assert result.errors.reason.iloc[0].count("; ") == 3


def test_column_summary_separates_missing_from_invalid(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, "unit_price"] = ""
    raw.loc[1, "unit_price"] = "1.234"
    report = report_for(raw)
    price = next(c for c in report.columns if c.column == "unit_price")
    assert (price.missing, price.invalid, price.issue_rows) == (1, 1, 2)
    assert price.complete_percent == 66.67
    date = next(c for c in report.columns if c.column == "date")
    assert (date.missing, date.invalid, date.complete_percent) == (0, 0, 100.0)


# --- Invalid records across the dirty fixture ------------------------------------


def test_dirty_fixture_statistics_by_check_and_dimension():
    report = report_for(load_files([EXAMPLES / "sample_sales_dirty.csv"], Config()))
    assert report.summary.input_rows == 357
    assert report.summary.accepted_rows == 350
    assert report.summary.invalid_rows == 7
    assert report.summary.excluded_rows == 7
    assert report.summary.total_issues == 7
    assert report.summary.rows_with_multiple_issues == 0
    assert report.summary.accepted_percent == 98.04
    assert report.summary.invalid_percent == 1.96
    assert counts_by_check(report) == {
        (DUPLICATE_LINE, ""): 2,
        (INVALID_DATE, "date"): 1,
        (INVALID_PRICE, "unit_price"): 1,
        (INVALID_QUANTITY, "quantity"): 1,
        (MISSING_VALUE, "product"): 1,
        (MISSING_VALUE, "unit_price"): 1,
    }
    # Most frequent check first, then alphabetically by code.
    assert [c.code for c in report.checks][0] == DUPLICATE_LINE
    assert report.dimensions == {
        "completeness": {"rows": 2, "percent": 0.56},
        "validity": {"rows": 3, "percent": 0.84},
        "uniqueness": {"rows": 2, "percent": 0.56},
    }
    duplicates = next(c for c in report.checks if c.code == DUPLICATE_LINE)
    assert duplicates.dimension == "uniqueness"
    assert [e["source_row"] for e in duplicates.examples] == [357, 358]


def test_duplicates_are_not_attributed_to_the_key_columns():
    report = report_for(load_files([EXAMPLES / "sample_sales_dirty.csv"], Config()))
    category = next(c for c in report.columns if c.column == "category")
    assert (category.missing, category.invalid, category.issue_rows) == (0, 0, 0)
    product = next(c for c in report.columns if c.column == "product")
    assert (product.missing, product.invalid) == (1, 0)


def test_top_reasons_reconcile_with_the_error_diagnostics():
    config = Config()
    result = validate(load_files([EXAMPLES / "sample_sales_dirty.csv"], config), config)
    report = build_quality_report(result, config)
    assert sum(r["rows"] for r in report.top_reasons) == len(result.errors)
    assert set(r["reason"] for r in report.top_reasons) == set(result.errors.reason)
    assert report.top_reasons == sorted(report.top_reasons, key=lambda r: (-r["rows"], r["reason"]))


def test_example_limit_caps_and_orders_locations(sales_file, tmp_path):
    rows = ["date,product,category,quantity,unit_price"]
    rows += [f"2026-01-{day:02d},P{day},C,-1,1.00" for day in range(1, 9)]
    path = tmp_path / "many.csv"
    path.write_text("\n".join(rows) + "\n")
    report = report_for(load_files([path], Config()))
    quantity = next(c for c in report.checks if c.code == INVALID_QUANTITY)
    assert quantity.rows == 8
    assert len(quantity.examples) == 5
    assert [e["source_row"] for e in quantity.examples] == [2, 3, 4, 5, 6]
    limited = build_quality_report(
        validate(load_files([path], Config()), Config()), Config(), example_limit=2
    )
    assert len(limited.checks[0].examples) == 2


# --- Per-file aggregation and policies -------------------------------------------


def test_statistics_per_source_file(sales_file, tmp_path):
    second = tmp_path / "second.csv"
    second.write_text(
        "Date,Product,Category,Quantity,Unit Price,Order ID\n"
        "2026-03-01,Lamp,Office,2,19.00,B001\n"
        "2026-03-02,,Office,1,5.00,B002\n",
        encoding="utf-8",
    )
    report = report_for(load_files([sales_file, second], Config()))
    assert report.summary.files == 2
    assert [f.file_name for f in report.files] == ["sales.csv", "second.csv"]
    first, other = report.files
    assert (first.input_rows, first.accepted_rows, first.invalid_rows, first.issues) == (3, 3, 0, 0)
    assert (other.input_rows, other.accepted_rows, other.invalid_rows, other.issues) == (2, 1, 1, 1)
    assert not any(f.rejected for f in report.files)
    assert [f.excluded_rows for f in report.files] == [0, 1]


def test_reject_file_marks_the_file_and_excludes_its_valid_rows(sales_file, tmp_path):
    raw = load_files([sales_file], Config())
    raw.loc[0, "quantity"] = "-1"
    raw.loc[2, "source_file"] = str(tmp_path / "good.csv")
    config = Config(validation_policy="reject_file")
    report = build_quality_report(validate(raw, config), config)
    bad, good = report.files
    assert bad.rejected and not good.rejected
    assert (bad.input_rows, bad.invalid_rows, bad.excluded_rows, bad.accepted_rows) == (2, 1, 2, 0)
    assert (good.input_rows, good.excluded_rows, good.accepted_rows) == (1, 0, 1)
    assert report.summary.rejected_files == 1
    assert report.summary.excluded_rows == 2
    assert report.policy == "reject_file"


def test_skip_policy_produces_the_same_statistics_as_quarantine(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, "quantity"] = "-1"
    quarantine = report_for(raw, Config())
    skipped = report_for(raw, Config(validation_policy="skip"))
    assert skipped.summary.to_dict() == quarantine.summary.to_dict()
    assert counts_by_check(skipped) == counts_by_check(quarantine)
    assert skipped.policy == "skip"


# --- Rendered mapping -------------------------------------------------------------


def test_to_dict_is_json_ready_and_deterministic(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, ["quantity", "product"]] = ["-1", ""]
    config = Config(date_format="%Y-%m-%d", csv_chunk_size=2)
    generated = datetime(2026, 11, 8, 10, 0, tzinfo=UTC)
    payload = build_quality_report(validate(raw, config), config, generated).to_dict()
    assert payload == build_quality_report(validate(raw, config), config, generated).to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["generated_at"] == "2026-11-08T10:00:00+00:00"
    assert payload["settings"] == {
        "date_format": "%Y-%m-%d",
        "csv_chunk_size": 2,
        "checkpointing": False,
    }
    assert list(payload) == [
        "schema_version",
        "toolkit_version",
        "generated_at",
        "policy",
        "settings",
        "summary",
        "dimensions",
        "by_check",
        "by_column",
        "by_file",
        "top_reasons",
    ]
    assert payload["summary"]["invalid_rows"] == 1
    assert all(isinstance(v, int) for k, v in payload["summary"].items() if "percent" not in k)
    assert payload["by_check"][0]["examples"][0]["source_row"] == 2
    assert payload["generated_at"] is not None
    assert build_quality_report(validate(raw, config), config).to_dict()["generated_at"] is None


@pytest.mark.parametrize("chunk_size", [None, 1, 2, 1000])
def test_statistics_are_identical_for_chunked_reads(chunk_size):
    config = Config(csv_chunk_size=chunk_size)
    report = build_quality_report(
        validate(load_files([EXAMPLES / "sample_sales_dirty.csv"], config), config), config
    )
    payload = report.to_dict()
    payload["settings"] = {}
    whole = build_quality_report(
        validate(load_files([EXAMPLES / "sample_sales_dirty.csv"], Config()), Config()), Config()
    ).to_dict()
    whole["settings"] = {}
    assert payload == whole


def test_blank_rows_are_outside_the_statistics(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(
        "date,product,category,quantity,unit_price\n2026-01-05,Laptop,Electronics,2,900\n\n\n"
    )
    report = report_for(load_files([path], Config()))
    assert report.summary.input_rows == 1
    assert report.summary.accepted_percent == 100.0
    assert report.files[0].input_rows == 1
