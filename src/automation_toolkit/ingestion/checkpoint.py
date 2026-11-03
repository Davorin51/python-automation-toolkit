"""Per-file checkpoints so an interrupted chunked CSV load resumes at the next chunk.

A checkpoint is a folder holding ``checkpoint.json`` plus one JSON file per processed chunk.
The manifest fingerprints the source file and the settings that influence ingestion; anything
that does not match, cannot be parsed, or fails its checksum makes the whole checkpoint
untrusted, in which case it is discarded with a warning and the file is processed from the
start. Files are written to a temporary name and renamed, so a crash never leaves a partially
written chunk or manifest in place.
"""

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError

VERSION = 1
MANIFEST = "checkpoint.json"
logger = logging.getLogger(__name__)


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_atomic(path: Path, payload: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(temporary, path)


def _fingerprint(source: Path, config: Config) -> dict[str, object]:
    stat = source.stat()
    return {
        "version": VERSION,
        "source": {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns},
        "settings": {
            "chunk_size": config.csv_chunk_size,
            "delimiter": config.csv_delimiter,
            "encoding": config.csv_encoding,
            "column_mapping": config.column_mapping,
        },
    }


class CsvCheckpoint:
    """Progress of one CSV file: restored pieces plus the number of records they cover."""

    def __init__(self, folder: Path, source: Path, config: Config) -> None:
        self.folder = folder
        self.source = source
        self.manifest = _fingerprint(source, config)
        self.manifest["columns"] = None
        self.manifest["chunks"] = []
        self.pieces: list[pd.DataFrame] = []
        self.records = 0
        self.blank = 0

    @classmethod
    def open(cls, directory: Path, source: Path, config: Config) -> "CsvCheckpoint":
        """Load the checkpoint for ``source`` if it is intact and current, else start fresh."""
        key = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
        checkpoint = cls(directory / f"{source.stem}-{key}", source, config)
        if (checkpoint.folder / MANIFEST).exists():
            try:
                checkpoint._restore()
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.warning(
                    "Ignoring unusable checkpoint for %s (%s); processing from the start.",
                    source.name,
                    exc,
                )
                checkpoint._discard()
            else:
                logger.info(
                    "Resuming %s from checkpoint: %s chunks (%s records) already processed",
                    source.name,
                    len(checkpoint.manifest["chunks"]),
                    checkpoint.records,
                )
        return checkpoint

    def _restore(self) -> None:
        manifest = json.loads((self.folder / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise ValueError("manifest is not a mapping")
        for field in ("version", "source", "settings"):
            if manifest.get(field) != self.manifest[field]:
                raise ValueError(f"{field} does not match the current file and settings")
        chunks = manifest.get("chunks")
        columns = manifest.get("columns")
        if not isinstance(chunks, list) or not isinstance(columns, list):
            raise ValueError("chunk list or columns are malformed")
        pieces, records, blank = [], 0, 0
        for position, entry in enumerate(chunks, start=1):
            path = self.folder / str(entry["file"])
            if path.name != f"chunk-{position:06d}.json" or _sha256(path) != entry["sha256"]:
                raise ValueError(f"chunk {position} is missing, renamed or altered")
            payload = json.loads(path.read_text(encoding="utf-8"))
            rows = payload["rows"]
            if payload["columns"] != columns or len(rows) != int(entry["rows"]):
                raise ValueError(f"chunk {position} content does not match the manifest")
            if type(entry["records"]) is not int or entry["records"] < len(rows):
                raise ValueError(f"chunk {position} has an invalid record count")
            if rows:
                pieces.append(pd.DataFrame(rows, columns=columns))
            records += entry["records"]
            blank += int(entry["blank"])
        self.manifest["columns"] = columns
        self.manifest["chunks"] = chunks
        self.pieces, self.records, self.blank = pieces, records, blank

    def _discard(self) -> None:
        shutil.rmtree(self.folder, ignore_errors=True)
        self.manifest["columns"] = None
        self.manifest["chunks"] = []
        self.pieces, self.records, self.blank = [], 0, 0

    @property
    def columns(self) -> pd.Index | None:
        columns = self.manifest["columns"]
        return pd.Index(columns) if columns is not None else None

    def save(self, piece: pd.DataFrame, records: int, blank: int) -> None:
        """Persist one processed chunk covering ``records`` raw records, ``blank`` of them blank."""
        position = len(self.manifest["chunks"]) + 1
        name = f"chunk-{position:06d}.json"
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self.folder / name
            columns = [str(c) for c in piece.columns]
            rows = [
                [int(v) if c == "source_row" else v for c, v in zip(columns, row, strict=True)]
                for row in piece.itertuples(index=False, name=None)
            ]
            _write_atomic(path, {"columns": columns, "rows": rows})
            if self.manifest["columns"] is None:
                self.manifest["columns"] = columns
            self.manifest["chunks"].append(
                {
                    "file": name,
                    "sha256": _sha256(path),
                    "records": records,
                    "blank": blank,
                    "rows": len(rows),
                }
            )
            _write_atomic(self.folder / MANIFEST, self.manifest)
        except (OSError, TypeError, ValueError) as exc:
            raise IngestionError(f"Cannot write checkpoint for {self.source.name}: {exc}") from exc
        self.records += records
        self.blank += blank

    def complete(self) -> None:
        """Remove the checkpoint once the file has been fully loaded."""
        shutil.rmtree(self.folder, ignore_errors=True)
