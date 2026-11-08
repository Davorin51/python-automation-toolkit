"""Vectorized checks with raw-value quarantine and source-row provenance."""

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.ingestion import REQUIRED
from automation_toolkit.processing.cleaning import clean_text

QUANTITY_RULE = "Quantity must be a positive integer <= 1000000"
PRICE_RULE = "Price must be non-negative with at most two decimal places"

# Stable identifiers for the failed checks. The human-readable ``reason`` text may change
# with the configuration (date format) or the data (first occurrence of a duplicate), so
# these codes, not the messages, are what statistics and downstream tools group by.
MISSING_VALUE = "missing_value"
INVALID_DATE = "invalid_date"
INVALID_QUANTITY = "invalid_quantity"
INVALID_PRICE = "invalid_price"
DUPLICATE_LINE = "duplicate_line"

# Quality dimension each check belongs to.
CHECK_DIMENSIONS = {
    MISSING_VALUE: "completeness",
    INVALID_DATE: "validity",
    INVALID_QUANTITY: "validity",
    INVALID_PRICE: "validity",
    DUPLICATE_LINE: "uniqueness",
}

# Configuration-independent descriptions, unlike the per-row ``reason`` text.
CHECK_DESCRIPTIONS = {
    MISSING_VALUE: "Required value is empty",
    INVALID_DATE: "Date does not match the configured format",
    INVALID_QUANTITY: QUANTITY_RULE,
    INVALID_PRICE: PRICE_RULE,
    DUPLICATE_LINE: "Duplicate of an earlier transaction line",
}

CHECK_COLUMNS = ["source_file", "source_row", "code", "column"]


def empty_checks() -> pd.DataFrame:
    """An empty per-check frame with the documented columns and dtypes."""
    return pd.DataFrame(
        {
            "source_file": pd.Series(dtype="object"),
            "source_row": pd.Series(dtype="int64"),
            "code": pd.Series(dtype="object"),
            "column": pd.Series(dtype="object"),
        }
    )


@dataclass
class ValidationResult:
    transactions: pd.DataFrame
    errors: pd.DataFrame
    input_rows: int
    invalid_rows: int
    excluded_rows: int
    rejected_files: list[str]
    #: One row per failed check: ``source_file``, ``source_row``, ``code``, ``column``.
    #: A row failing several checks appears once per check, in rule-evaluation order;
    #: ``column`` is empty for row-level checks such as duplicates.
    checks: pd.DataFrame = field(default_factory=empty_checks)
    #: Input rows per source file, in the order the files first appear.
    file_rows: dict[str, int] = field(default_factory=dict)


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
    recorded: list[tuple[str, str, pd.Series]] = []

    def flag(
        mask: pd.Series,
        message: str | pd.Series,
        column: str,
        code: str,
        check_column: str | None = None,
    ) -> None:
        mask = mask.fillna(True).astype(bool)
        if not mask.any():
            return
        reasons.loc[mask] += message.loc[mask] if isinstance(message, pd.Series) else message
        reasons.loc[mask] += "; "
        columns_hit.loc[mask] += column + "; "
        recorded.append((code, column if check_column is None else check_column, mask))

    for column in ["date", "quantity", "unit_price"]:
        data[column] = _strip_text(data[column])
    blank_date = _is_blank(data["date"])
    date_types = data["date"].map(lambda value: isinstance(value, (str, date, datetime)))
    data["date"] = pd.to_datetime(
        data["date"].where(date_types), format=config.date_format, errors="coerce"
    )
    flag(blank_date, "Missing date", "date", MISSING_VALUE)
    flag(
        data["date"].isna() & ~blank_date,
        f"Invalid date (expected format {config.date_format})",
        "date",
        INVALID_DATE,
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
    flag(blank_quantity, "Missing quantity", "quantity", MISSING_VALUE)
    flag(~q_valid & ~blank_quantity, QUANTITY_RULE, "quantity", INVALID_QUANTITY)
    flag(blank_price, "Missing unit_price", "unit_price", MISSING_VALUE)
    flag(~p_valid & ~blank_price, PRICE_RULE, "unit_price", INVALID_PRICE)
    data["quantity"] = quantity.where(q_valid).round().astype("Int64")
    data["unit_price_cents"] = scaled.where(p_valid).round().astype("Int64")
    for column in ["product", "category"] + (["order_id"] if "order_id" in data else []):
        flag(data[column].eq(""), f"Missing {column}", column, MISSING_VALUE)
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
            DUPLICATE_LINE,
            # A duplicate is a property of the whole line, not of one column, so it is
            # not attributed to any single column in the per-check statistics.
            check_column="",
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
    counts = raw["source_file"].value_counts(sort=False)
    file_rows = {str(name): int(counts[name]) for name in raw["source_file"].drop_duplicates()}
    return ValidationResult(
        good.reset_index(drop=True),
        errors.reset_index(drop=True),
        len(data),
        int(invalid.sum()),
        int(excluded.sum()),
        rejected,
        _checks_frame(raw, recorded),
        file_rows,
    )


def _checks_frame(raw: pd.DataFrame, recorded: list[tuple[str, str, pd.Series]]) -> pd.DataFrame:
    """One row per failed check, ordered by input row and then by rule-evaluation order."""
    parts = []
    for code, column, mask in recorded:
        part = raw.loc[mask, ["source_file", "source_row"]].copy()
        part["code"] = code
        part["column"] = column
        parts.append(part)
    if not parts:
        return empty_checks()
    # A stable sort keeps the checks of one row in the order the rules ran, which is the
    # same order as the parts of its ``reason`` text.
    frame = pd.concat(parts).sort_index(kind="stable").reset_index(drop=True)
    return frame.astype({"source_row": "int64", "code": "object", "column": "object"})
