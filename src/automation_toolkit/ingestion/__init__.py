"""File ingestion and column normalization."""

import logging
import re
from pathlib import Path

import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.ingestion.csv_reader import read_csv
from automation_toolkit.ingestion.excel_reader import read_excel

REQUIRED = ["date", "product", "category", "quantity", "unit_price"]
ALIASES = {
    "transaction_date": "date",
    "sales_date": "date",
    "product_name": "product",
    "product_category": "category",
    "qty": "quantity",
    "price": "unit_price",
    "unitprice": "unit_price",
    "orderid": "order_id",
}
logger = logging.getLogger(__name__)


def _header_text(column: object) -> str:
    """Return the header as text; blank, None or NaN headers become an empty string."""
    if column is None or (isinstance(column, float) and pd.isna(column)):
        return ""
    return str(column).strip()


def blank_rows(frame: pd.DataFrame) -> pd.Series:
    """Rows whose cells are all missing or whitespace-only, such as trailing blank lines."""
    if frame.empty:
        return pd.Series(False, index=frame.index, dtype=bool)
    blank_cells = frame.isna() | frame.map(lambda v: isinstance(v, str) and not v.strip())
    return blank_cells.all(axis=1)


def normalize_columns(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    names = []
    sources: dict[str, list[str]] = {}
    for column in frame.columns:
        header = _header_text(column)
        if not header:
            # Blank headers (trailing delimiters, empty formatted Excel columns) carry no data.
            names.append("")
            continue
        name = config.column_mapping.get(header, header)
        name = re.sub(r"[\s-]+", "_", name.strip().lower())
        name = ALIASES.get(name, name)
        names.append(name)
        sources.setdefault(name, []).append(header)
    ambiguous = {name: headers for name, headers in sources.items() if len(headers) > 1}
    if ambiguous:
        detail = "; ".join(
            f"{', '.join(repr(h) for h in headers)} -> {name}"
            for name, headers in ambiguous.items()
        )
        raise IngestionError(
            f"Ambiguous columns: multiple headers map to the same field ({detail})."
        )
    result = frame.copy()
    result.columns = names
    missing = set(REQUIRED) - set(names)
    if missing:
        found_headers = [h for headers in sources.values() for h in headers]
        found = ", ".join(repr(h) for h in found_headers) or "none"
        raise IngestionError(
            f"Missing required columns: {', '.join(sorted(missing))}. "
            f"Found headers: {found}. Rename the headers or use column_mapping."
        )
    return result[REQUIRED + (["order_id"] if "order_id" in names else [])]


def load_files(paths: list[Path], config: Config) -> pd.DataFrame:
    if not paths:
        raise IngestionError("Select at least one CSV or XLSX file.")
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise IngestionError("The same input file was selected more than once.")
    frames = []
    has_ids = set()
    for original, path in zip(paths, resolved, strict=True):
        if not path.exists():
            raise IngestionError(f"Input file does not exist: {path}")
        if not path.is_file():
            raise IngestionError(f"Input path is not a file: {path}")
        reader = {".csv": read_csv, ".xlsx": read_excel}.get(path.suffix.lower())
        if reader is None:
            raise IngestionError(f"Unsupported file type: {path.suffix}. Use CSV or XLSX.")
        raw = reader(path, config)
        try:
            frame = normalize_columns(raw, config)
        except IngestionError as exc:
            raise IngestionError(f"{path.name}: {exc}") from None
        frame["source_file"] = str(original)
        # Number rows before dropping blanks so source_row still matches the file.
        frame["source_row"] = range(2, len(frame) + 2)
        blank = blank_rows(raw)
        if blank.any():
            logger.info("Skipped %s blank rows in %s", int(blank.sum()), path.name)
            frame = frame.loc[~blank.to_numpy()]
        if frame.empty:
            raise IngestionError(f"Input file has no data rows: {path.name}")
        has_ids.add("order_id" in frame.columns)
        frames.append(frame)
        logger.info("Loaded %s rows from %s", len(frame), path.name)
    if len(has_ids) > 1:
        raise IngestionError("Provide order_id in every input file or in none of them.")
    return pd.concat(frames, ignore_index=True)
