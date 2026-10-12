import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.validation import validate


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("date", "2026-02-30", "Invalid date"),
        ("date", "not a date", "Invalid date"),
        ("date", 12345, "Invalid date"),
        ("quantity", "-1", "Quantity"),
        ("quantity", "0", "Quantity"),
        ("quantity", "1.5", "Quantity"),
        ("quantity", "inf", "Quantity"),
        ("quantity", "1000001", "Quantity"),
        ("unit_price", "-1", "Price"),
        ("unit_price", "", "Price"),
        ("unit_price", "nan", "Price"),
        ("unit_price", "inf", "Price"),
        ("unit_price", "0.001", "Price"),
        ("unit_price", "0.0100001", "Price"),
        ("unit_price", "10000000001", "Price"),
        ("product", " ", "Missing product"),
        ("category", "", "Missing category"),
        ("order_id", " ", "Missing order_id"),
    ],
)
def test_invalid_values(sales_file, field, value, message):
    raw = load_files([sales_file], Config())
    raw[field] = raw[field].astype(object)
    raw.loc[0, field] = value
    result = validate(raw, Config())
    assert result.invalid_rows == 1
    assert result.excluded_rows == 1
    assert result.errors.source_row.iloc[0] == 2
    assert message in result.errors.reason.iloc[0]
    assert result.errors[field].iloc[0] == value


def test_exact_cents_and_whitespace(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, "unit_price"] = "0.29"
    raw.loc[0, "product"] = " Laptop "
    result = validate(raw, Config())
    assert result.transactions.revenue_cents.iloc[0] == 58
    assert result.transactions["product"].iloc[0] == "Laptop"
    assert raw["product"].iloc[0] == " Laptop "


def test_cross_file_duplicates(sales_file):
    raw = load_files([sales_file], Config())
    copy = raw.iloc[[0]].copy()
    copy["source_file"] = "another.csv"
    result = validate(pd.concat([raw, copy], ignore_index=True), Config())
    assert len(result.transactions) == 3
    assert result.invalid_rows == 1
    assert result.errors.reason.iloc[0] == "Duplicate transaction line"


def test_distinct_orders_not_duplicates(sales_file):
    raw = load_files([sales_file], Config())
    copy = raw.iloc[[0]].copy()
    copy["order_id"] = "OTHER"
    result = validate(pd.concat([raw, copy], ignore_index=True), Config())
    assert result.invalid_rows == 0


def test_reject_file_excludes_its_good_rows(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, "quantity"] = "-1"
    raw.loc[2, "source_file"] = "good.csv"
    result = validate(raw, Config(validation_policy="reject_file"))
    assert result.invalid_rows == 1
    assert result.excluded_rows == 2
    assert len(result.transactions) == 1
    assert result.rejected_files == [str(sales_file)]


def test_skip_keeps_only_diagnostics(sales_file):
    raw = load_files([sales_file], Config())
    raw.loc[0, "quantity"] = "-1"
    result = validate(raw, Config(validation_policy="skip"))
    assert list(result.errors.columns) == ["source_file", "source_row", "reason"]


def test_duplicate_without_order_ids(sales_file):
    raw = load_files([sales_file], Config()).drop(columns="order_id")
    result = validate(pd.concat([raw, raw.iloc[[0]]], ignore_index=True), Config())
    assert result.invalid_rows == 1


def test_revenue_overflow_rejected(sales_file):
    raw = load_files([sales_file], Config())
    raw = pd.concat([raw.iloc[[0]]] * 10, ignore_index=True)
    raw["order_id"] = [str(i) for i in range(10)]
    raw["quantity"] = "1000000"
    raw["unit_price"] = "10000000000"
    with pytest.raises(ValueError, match="integer-cent"):
        validate(raw, Config())
