import pandas as pd
import pytest

from automation_toolkit.processing.analytics import analyze


def test_known_business_metrics(clean_sales):
    metrics = analyze(clean_sales)
    assert metrics.total_revenue_cents == 295000
    assert metrics.orders == 2
    assert metrics.average_order_value == 1475
    assert metrics.units_sold == 15
    assert metrics.order_basis == "distinct order_id"
    assert metrics.categories.iloc[0]["category"] == "Electronics"
    assert metrics.categories.revenue.sum() == 2950
    assert metrics.monthly.revenue.tolist() == [2050, 900]
    assert metrics.comparison.change_percent.iloc[1] == pytest.approx(-56.0975609756)
    assert pd.isna(metrics.comparison.change_percent.iloc[0])
    assert metrics.weekly.date.iloc[0].weekday() == 6


def test_one_order_per_row_fallback(clean_sales):
    metrics = analyze(clean_sales.drop(columns="order_id"))
    assert metrics.orders == 3
    assert metrics.average_order_value == pytest.approx(2950 / 3)
    assert "assumed" in metrics.order_basis


def test_missing_month_and_zero_denominator(clean_sales):
    clean_sales.loc[2, "date"] = pd.Timestamp("2026-03-07")
    metrics = analyze(clean_sales)
    assert metrics.monthly.revenue.tolist() == [2050, 0, 900]
    assert metrics.comparison.change_percent.iloc[1] == -100
    assert pd.isna(metrics.comparison.change_percent.iloc[2])


def test_zero_revenue_is_valid(clean_sales):
    clean_sales["revenue_cents"] = 0
    metrics = analyze(clean_sales)
    assert metrics.total_revenue_cents == 0
    assert metrics.average_order_value == 0
