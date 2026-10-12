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


def normalize_columns(frame: pd.DataFrame, config: Config) -> pd.DataFrame:
    names = []
    for column in frame.columns:
        name = config.column_mapping.get(str(column), str(column))
        name = re.sub(r"[\s-]+", "_", name.strip().lower())
        names.append(ALIASES.get(name, name))
    if len(names) != len(set(names)):
        raise IngestionError("Ambiguous columns: multiple headers map to the same field.")
    result = frame.copy()
    result.columns = names
    missing = set(REQUIRED) - set(names)
    if missing:
        raise IngestionError(f"Missing required columns: {', '.join(sorted(missing))}.")
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
        if not path.is_file():
            raise IngestionError(f"Input file does not exist: {path}")
        reader = {".csv": read_csv, ".xlsx": read_excel}.get(path.suffix.lower())
        if reader is None:
            raise IngestionError(f"Unsupported file type: {path.suffix}. Use CSV or XLSX.")
        frame = normalize_columns(reader(path, config), config)
        if frame.empty:
            raise IngestionError(f"Input file has no data rows: {path.name}")
        has_ids.add("order_id" in frame.columns)
        frame["source_file"] = str(original)
        frame["source_row"] = range(2, len(frame) + 2)
        frames.append(frame)
        logger.info("Loaded %s rows from %s", len(frame), path.name)
    if len(has_ids) > 1:
        raise IngestionError("Provide order_id in every input file or in none of them.")
    return pd.concat(frames, ignore_index=True)
