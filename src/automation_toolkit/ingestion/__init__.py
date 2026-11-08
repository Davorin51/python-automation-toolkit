"""File ingestion and column normalization."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.ingestion.checkpoint import CsvCheckpoint
from automation_toolkit.ingestion.csv_reader import iter_csv_chunks, read_csv
from automation_toolkit.ingestion.excel_reader import read_excel

__all__ = [
    "ALIASES",
    "REQUIRED",
    "FileStats",
    "IngestionStats",
    "blank_rows",
    "iter_csv_chunks",
    "load_files",
    "load_files_with_stats",
    "normalize_columns",
    "read_csv",
    "read_excel",
]

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


@dataclass(frozen=True)
class FileStats:
    """How one input file was read.

    ``records_read`` counts raw records including blank ones, restored from a checkpoint
    plus parsed in this run, so ``records_read - blank_rows == data_rows`` always holds.
    ``chunks_processed`` is the number of frames parsed in this run: one for a whole-file
    CSV or XLSX read, otherwise the number of CSV chunks; chunks restored from a
    checkpoint are counted separately.
    """

    source_file: str
    file_name: str
    file_format: str
    records_read: int
    blank_rows: int
    data_rows: int
    chunked: bool
    chunks_processed: int
    chunks_restored: int
    records_restored: int
    checkpointed: bool

    @property
    def resumed_from_checkpoint(self) -> bool:
        return self.chunks_restored > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_file": self.source_file,
            "file_name": self.file_name,
            "format": self.file_format,
            "records_read": self.records_read,
            "blank_rows": self.blank_rows,
            "data_rows": self.data_rows,
            "chunked": self.chunked,
            "chunks_processed": self.chunks_processed,
            "chunks_restored": self.chunks_restored,
            "records_restored": self.records_restored,
            "checkpointed": self.checkpointed,
            "resumed_from_checkpoint": self.resumed_from_checkpoint,
        }


@dataclass(frozen=True)
class IngestionStats:
    """Per-file reading statistics for one :func:`load_files_with_stats` call, in input order."""

    files: list[FileStats]

    @property
    def records_read(self) -> int:
        return sum(f.records_read for f in self.files)

    @property
    def blank_rows(self) -> int:
        return sum(f.blank_rows for f in self.files)

    @property
    def data_rows(self) -> int:
        return sum(f.data_rows for f in self.files)

    @property
    def chunks_processed(self) -> int:
        return sum(f.chunks_processed for f in self.files)

    @property
    def chunks_restored(self) -> int:
        return sum(f.chunks_restored for f in self.files)

    @property
    def chunked_files(self) -> int:
        return sum(1 for f in self.files if f.chunked)

    @property
    def checkpointed_files(self) -> int:
        return sum(1 for f in self.files if f.checkpointed)

    @property
    def resumed_files(self) -> int:
        return sum(1 for f in self.files if f.resumed_from_checkpoint)

    def by_source(self) -> dict[str, FileStats]:
        return {f.source_file: f for f in self.files}

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": len(self.files),
            "records_read": self.records_read,
            "blank_rows_skipped": self.blank_rows,
            "data_rows": self.data_rows,
            "chunked_files": self.chunked_files,
            "chunks_processed": self.chunks_processed,
            "chunks_restored": self.chunks_restored,
            "checkpointed_files": self.checkpointed_files,
            "files_resumed_from_checkpoint": self.resumed_files,
        }


def load_files(paths: list[Path], config: Config) -> pd.DataFrame:
    """Load and normalize the inputs; see :func:`load_files_with_stats` for the statistics."""
    return load_files_with_stats(paths, config)[0]


def load_files_with_stats(paths: list[Path], config: Config) -> tuple[pd.DataFrame, IngestionStats]:
    """Load the inputs and report how each file was read.

    The frame is identical to :func:`load_files`; the statistics expose the record, blank
    row and chunk counts that were previously only logged, plus whether a CSV file was
    resumed from a checkpoint.
    """
    if not paths:
        raise IngestionError("Select at least one CSV or XLSX file.")
    resolved = [p.resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise IngestionError("The same input file was selected more than once.")
    frames = []
    stats: list[FileStats] = []
    has_ids = set()
    for original, path in zip(paths, resolved, strict=True):
        if not path.exists():
            raise IngestionError(f"Input file does not exist: {path}")
        if not path.is_file():
            raise IngestionError(f"Input path is not a file: {path}")
        suffix = path.suffix.lower()
        if suffix not in {".csv", ".xlsx"}:
            raise IngestionError(f"Unsupported file type: {path.suffix}. Use CSV or XLSX.")
        # CSV chunks are normalized and filtered one at a time, so extra columns, blank rows
        # and the raw text of each chunk are released before the next chunk is parsed.
        checkpoint = None
        records_restored = chunks_restored = 0
        if suffix == ".csv" and config.csv_checkpoint_dir is not None:
            checkpoint = CsvCheckpoint.open(Path(config.csv_checkpoint_dir), path, config)
            # Captured before any save() appends to the manifest.
            records_restored = checkpoint.records
            chunks_restored = len(checkpoint.manifest["chunks"])
            chunks = iter_csv_chunks(path, config, skip_records=checkpoint.records)
            pieces, columns, blank_count = (
                list(checkpoint.pieces),
                checkpoint.columns,
                checkpoint.blank,
            )
            for piece in pieces:
                # Provenance follows how the file was named in this run, not the earlier one.
                piece["source_file"] = str(original)
        else:
            chunks = (
                iter_csv_chunks(path, config)
                if suffix == ".csv"
                else iter((read_excel(path, config),))
            )
            pieces, columns, blank_count = [], None, 0
        records_now = chunks_now = 0
        for raw in chunks:
            records_now += len(raw)
            chunks_now += 1
            try:
                frame = normalize_columns(raw, config)
            except IngestionError as exc:
                raise IngestionError(f"{path.name}: {exc}") from None
            frame["source_file"] = str(original)
            # Number rows before dropping blanks so source_row still matches the file; the
            # chunk index continues across chunks, so index + 2 is the record in the file.
            frame["source_row"] = frame.index + 2
            blank = blank_rows(raw)
            if blank.any():
                blank_count += int(blank.sum())
                frame = frame.loc[~blank.to_numpy()]
            columns = frame.columns
            if not frame.empty:
                pieces.append(frame)
            if checkpoint is not None:
                checkpoint.save(frame, len(raw), int(blank.sum()))
        if blank_count:
            logger.info("Skipped %s blank rows in %s", blank_count, path.name)
        if not pieces:
            raise IngestionError(f"Input file has no data rows: {path.name}")
        if checkpoint is not None:
            checkpoint.complete()
        has_ids.add(columns is not None and "order_id" in columns)
        frames.extend(pieces)
        data_rows = sum(len(piece) for piece in pieces)
        stats.append(
            FileStats(
                source_file=str(original),
                file_name=path.name,
                file_format=suffix.lstrip("."),
                records_read=records_restored + records_now,
                blank_rows=blank_count,
                data_rows=data_rows,
                chunked=suffix == ".csv" and config.csv_chunk_size is not None,
                chunks_processed=chunks_now,
                chunks_restored=chunks_restored,
                records_restored=records_restored,
                checkpointed=checkpoint is not None,
            )
        )
        logger.info("Loaded %s rows from %s", data_rows, path.name)
    if len(has_ids) > 1:
        raise IngestionError("Provide order_id in every input file or in none of them.")
    return pd.concat(frames, ignore_index=True), IngestionStats(stats)
