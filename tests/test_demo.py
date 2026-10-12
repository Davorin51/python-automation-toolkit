import json
from pathlib import Path

import pytest

from automation_toolkit.config import Config
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.analytics import analyze
from automation_toolkit.processing.validation import validate

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.mark.parametrize(
    "filename", ["sample_sales.csv", "sample_sales.xlsx", "sample_sales_dirty.csv"]
)
def test_portfolio_fixture_control_totals(filename):
    expected = json.loads((EXAMPLES / "expected_results.json").read_text())
    result = validate(load_files([EXAMPLES / filename], Config()), Config())
    metrics = analyze(result.transactions)
    assert len(result.transactions) == expected["accepted_rows"]
    assert metrics.orders == expected["orders"]
    assert metrics.units_sold == expected["units_sold"]
    assert metrics.total_revenue_cents == expected["total_revenue_cents"]
    assert result.invalid_rows == (7 if "dirty" in filename else 0)
