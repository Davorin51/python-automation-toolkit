"""Sales metrics with explicit order semantics and calendar-period comparisons."""

from dataclasses import dataclass

import pandas as pd


@dataclass
class Analytics:
    total_revenue_cents: int
    orders: int
    order_basis: str
    average_order_value: float
    units_sold: int
    categories: pd.DataFrame
    products: pd.DataFrame
    monthly: pd.DataFrame
    weekly: pd.DataFrame
    comparison: pd.DataFrame


def analyze(transactions: pd.DataFrame) -> Analytics:
    data = transactions.copy()
    total = sum(int(x) for x in data["revenue_cents"])
    orders = data["order_id"].nunique() if "order_id" in data else len(data)

    def aggregate(field: str) -> pd.DataFrame:
        grouped = (
            data.groupby(field, sort=True)
            .agg(
                revenue_cents=("revenue_cents", "sum"),
                units=("quantity", "sum"),
                transaction_lines=("quantity", "size"),
            )
            .reset_index()
        )
        grouped["revenue"] = grouped["revenue_cents"] / 100
        return grouped.sort_values(["revenue_cents", field], ascending=[False, True])

    def trend(frequency: str) -> pd.DataFrame:
        values = data.set_index("date")["revenue_cents"].resample(frequency).sum()
        result = values.reset_index()
        result["revenue"] = result["revenue_cents"] / 100
        return result

    monthly = trend("MS")
    weekly = trend("W-SUN")
    comparison = monthly.copy()
    comparison["previous_revenue"] = comparison["revenue"].shift(1)
    comparison["change_percent"] = (
        comparison["revenue"].div(comparison["previous_revenue"].replace(0, float("nan"))) - 1
    ) * 100
    return Analytics(
        total,
        int(orders),
        "distinct order_id" if "order_id" in data else "one order per row (assumed)",
        total / 100 / orders if orders else 0.0,
        sum(int(x) for x in data["quantity"]),
        aggregate("category"),
        aggregate("product"),
        monthly,
        weekly,
        comparison,
    )
