"""CSV ingestion: whole-file reads and incremental, chunked reads sharing one parser contract."""

import logging
from collections.abc import Iterator
from pathlib import Path

import pandas as pd

from automation_toolkit.config import Config, check_chunk_size
from automation_toolkit.exceptions import ConfigurationError, IngestionError

logger = logging.getLogger(__name__)

# Everything pandas raises for unreadable files, bad encodings and malformed CSV.
_READ_ERRORS = (OSError, ValueError, UnicodeError, LookupError, pd.errors.ParserError)


def _read_options(config: Config, skip_records: int = 0) -> dict[str, object]:
    # Preserve IDs, strings such as NA, and raw values for error diagnostics.
    options: dict[str, object] = {
        "sep": config.csv_delimiter,
        "encoding": config.csv_encoding,
        "dtype": str,
        "keep_default_na": False,
        "skip_blank_lines": False,
        "header": None,
    }
    if skip_records:
        # A row range skips parsed records (quoted line breaks and blank lines included),
        # so a resumed read starts exactly after the last checkpointed record.
        options["skiprows"] = range(1, skip_records + 1)
    return options


def _read_whole(path: Path, config: Config, skip_records: int = 0) -> pd.DataFrame:
    frame = pd.read_csv(path, **_read_options(config, skip_records))
    frame.columns = frame.iloc[0].tolist()
    return frame.iloc[1:].reset_index(drop=True)


def _read_chunks(
    path: Path, config: Config, chunk_size: int, skip_records: int = 0
) -> Iterator[pd.DataFrame]:
    rows = skip_records
    chunks = 0
    options = _read_options(config, skip_records)
    with pd.read_csv(path, chunksize=chunk_size, **options) as reader:
        # The header is read on its own so every data chunk holds exactly chunk_size records.
        header = reader.get_chunk(1)
        columns = header.iloc[0].tolist()
        while True:
            try:
                chunk = reader.get_chunk(chunk_size)
            except StopIteration:
                break
            chunk.columns = columns
            chunk.index = pd.RangeIndex(rows, rows + len(chunk))
            rows += len(chunk)
            chunks += 1
            logger.debug("Read chunk %s of %s (%s rows so far)", chunks, path.name, rows)
            yield chunk
        if chunks == 0 and not skip_records:
            # A header-only file still yields one empty chunk so callers learn the columns.
            empty = header.iloc[0:0].reset_index(drop=True)
            empty.columns = columns
            yield empty
    logger.info(
        "Read %s rows from %s in %s chunks of up to %s",
        rows - skip_records,
        path.name,
        chunks,
        chunk_size,
    )


def iter_csv_chunks(
    path: Path, config: Config, chunk_size: int | None = None, skip_records: int = 0
) -> Iterator[pd.DataFrame]:
    """Yield the raw CSV records as consecutive DataFrames of at most ``chunk_size`` rows.

    ``chunk_size`` defaults to ``config.csv_chunk_size``; when both are ``None`` the whole file
    is yielded as one chunk. Chunks follow the same contract as :func:`read_csv`: the header
    values are the columns, cells are untouched text, blank lines are kept, and the integer
    index continues across chunks so ``index + 2`` is the record number in the file. A
    header-only file yields one empty chunk. Parsing problems anywhere in the file surface as
    :class:`IngestionError` from the ``next()`` call that hit them, and the file is closed.

    ``skip_records`` resumes after that many data records (the header is always read); the
    index of the first yielded row is then ``skip_records``, and nothing is yielded when the
    skipped records already cover the whole file.
    """
    size = check_chunk_size(chunk_size) if chunk_size is not None else config.csv_chunk_size
    if type(skip_records) is not int or skip_records < 0:
        raise ConfigurationError("skip_records must be a non-negative integer.")
    try:
        if size is None:
            yield _read_whole(path, config, skip_records)
        else:
            yield from _read_chunks(path, config, size, skip_records)
    except _READ_ERRORS as exc:
        raise IngestionError(f"Cannot read CSV {path.name}: {exc}") from exc


def read_csv(path: Path, config: Config) -> pd.DataFrame:
    """Read the whole CSV, incrementally when ``config.csv_chunk_size`` is set."""
    if config.csv_chunk_size is None:
        try:
            return _read_whole(path, config)
        except _READ_ERRORS as exc:
            raise IngestionError(f"Cannot read CSV {path.name}: {exc}") from exc
    return pd.concat(list(iter_csv_chunks(path, config)), ignore_index=True)
