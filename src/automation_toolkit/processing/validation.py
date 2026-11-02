"""Vectorized checks with raw-value quarantine and source-row provenance."""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.ingestion import REQUIRED
from automation_toolkit.processing.cleaning import clean_text

QUANTITY_RULE = "Quantity must be a positive integer <= 1000000"
PRICE_RULE = "Price must be non-negative with at most two decimal places"


@dataclass
class ValidationResult:
    transactions: pd.DataFrame
    errors: pd.DataFrame
    input_rows: int
    invalid_rows: int
    excluded_rows: int
    rejected_files: list[str]


def _is_blank(series: pd.Series) -> pd.Series:
    """Missing values and empty or whitespace-only text."""
    return series.isna() | series.map(lambda v: isinstance(v, str) and not v.strip())


def _strip_text(series: pd.Series) -> pd.Series:
    """Trim surrounding whitespace from text cells, leaving native values untouched."""
    return series.map(lambda v: v.strip() if isinstance(v, str) else v)


def validate(frame: pd.DataFrame, config: Config) -> ValidationResult:
    data = clean_text(frame).reset_index(drop=True)
    raw = frame.reset_index(drop=True).copy()
    reasons = pd.Series("", index=data.index, dtype="string")
    columns_hit = pd.Series("", index=data.index, dtype="string")

    def flag(mask: pd.Series, message: str | pd.Series, column: str) -> None:
        mask = mask.fillna(True).astype(bool)
        if not mask.any():
            return
        reasons.loc[mask] += message.loc[mask] if isinstance(message, pd.Series) else message
        reasons.loc[mask] += "; "
        columns_hit.loc[mask] += column + "; "

    for column in ["date", "quantity", "unit_price"]:
        data[column] = _strip_text(data[column])
    blank_date = _is_blank(data["date"])
    date_types = data["date"].map(lambda value: isinstance(value, (str, date, datetime)))
    data["date"] = pd.to_datetime(
        data["date"].where(date_types), format=config.date_format, errors="coerce"
    )
    flag(blank_date, "Missing date", "date")
    flag(
        data["date"].isna() & ~blank_date,
        f"Invalid date (expected format {config.date_format})",
        "date",
    )
    # Currency is converted once to cents. Reject fractional cents and non-finite values.
    blank_quantity = _is_blank(data["quantity"])
    blank_price = _is_blank(data["unit_price"])
    quantity = pd.to_numeric(data["quantity"], errors="coerce")
    price = pd.to_numeric(data["unit_price"], errors="coerce")
    q_valid = np.isfinite(quantity) & quantity.gt(0) & quantity.le(1_000_000)
    q_valid &= quantity.eq(quantity.round())
    scaled = price * 100
    p_valid = np.isfinite(price) & price.ge(0) & scaled.le(1_000_000_000_000)
    price_text = data["unit_price"].astype("string").str.strip()
    p_valid &= price_text.str.fullmatch(r"\+?(?:\d+(?:\.\d{1,2})?|\.\d{1,2})").fillna(False)
    flag(blank_quantity, "Missing quantity", "quantity")
    flag(~q_valid & ~blank_quantity, QUANTITY_RULE, "quantity")
    flag(blank_price, "Missing unit_price", "unit_price")
    flag(~p_valid & ~blank_price, PRICE_RULE, "unit_price")
    data["quantity"] = quantity.where(q_valid).round().astype("Int64")
    data["unit_price_cents"] = scaled.where(p_valid).round().astype("Int64")
    for column in ["product", "category"] + (["order_id"] if "order_id" in data else []):
        flag(data[column].eq(""), f"Missing {column}", column)
    # Only valid rows participate: a bad first occurrence must not suppress a later valid row.
    valid = reasons.eq("")
    keys = ["date", "product", "category", "quantity", "unit_price_cents"]
    if "order_id" in data:
        keys.append("order_id")
    duplicates = pd.Series(False, index=data.index)
    duplicates.loc[valid] = data.loc[valid].duplicated(keys, keep="first")
    if duplicates.any():
        groups = data.loc[valid].groupby(keys, sort=False, dropna=False)
        first_file = groups["source_file"].transform("first").map(lambda p: Path(str(p)).name)
        first_row = groups["source_row"].transform("first").astype(str)
        message = (
            "Duplicate transaction line (first occurrence: "
            + first_file
            + " row "
            + first_row
            + ")"
        )
        flag(
            duplicates,
            message.reindex(data.index),
            "; ".join(k.removesuffix("_cents") for k in keys),
        )
    invalid = reasons.ne("")
    rejected = (
        sorted(data.loc[invalid, "source_file"].unique().tolist())
        if (config.validation_policy == "reject_file")
        else []
    )
    excluded = invalid | data["source_file"].isin(rejected)
    error_columns = ["source_file", "source_row"]
    if config.validation_policy == "quarantine":
        error_columns += REQUIRED + (["order_id"] if "order_id" in data else [])
    errors = raw.loc[invalid, error_columns].copy()
    errors["reason"] = reasons.loc[invalid].str.rstrip("; ")
    errors["invalid_columns"] = columns_hit.loc[invalid].str.rstrip("; ")
    good = data.loc[~excluded].copy()
    good["revenue_cents"] = good["quantity"] * good["unit_price_cents"]
    # Bound aggregate arithmetic to avoid signed int64 overflow in pandas groupby sums.
    if sum(int(x) for x in good["revenue_cents"]) > np.iinfo(np.int64).max:
        raise ValueError("Total revenue exceeds supported integer-cent range.")
    good["unit_price"] = good["unit_price_cents"] / 100
    good["revenue"] = good["revenue_cents"] / 100
    return ValidationResult(
        good.reset_index(drop=True),
        errors.reset_index(drop=True),
        len(data),
        int(invalid.sum()),
        int(excluded.sum()),
        rejected,
    )
