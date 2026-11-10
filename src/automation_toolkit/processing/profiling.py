"""Schema-agnostic, streaming profiling of a tabular dataset.

Unlike :mod:`automation_toolkit.processing.quality`, which summarizes the sales
validation outcome, the profiler accepts *any* CSV: it does not require the sales
schema, never validates or rejects rows, and only describes what the file contains -
size, missing values, duplicate and blank rows, inferred column types, value
statistics and structural problems such as duplicate headers or short rows.

Large files are processed in chunks of ``chunk_size`` records (default
:data:`DEFAULT_CHUNK_SIZE`, or ``config.csv_chunk_size`` when set). Every statistic is
accumulated incrementally, so memory is bounded by one chunk plus:

* 8 bytes per distinct non-blank row (row hashes for duplicate detection);
* 24 bytes per distinct value of each column (hash, exact count, first position) for
  exact unique counts and top values, plus the text of at most
  :data:`TRACKED_VALUE_LIMIT` values;
* at most :data:`MEDIAN_SAMPLE_LIMIT` numbers per column, shared within a budget of
  :data:`MEDIAN_SAMPLE_BUDGET` per file (exact median below it, a deterministic
  positional bottom-k estimate above it).

Results do not depend on the chunk size: the median sample, top-value ranking and the
reported precision of the mean and standard deviation are all independent of it.
Duplicate and unique counts compare 64-bit hashes; a collision is astronomically
unlikely but not impossible.

The file is parsed with the standard :mod:`csv` module using the configured delimiter
and encoding, so the profiler sees exactly how many fields each record had. That keeps
an empty cell (``""``), a whitespace-only cell and a cell *absent* because the row was
too short apart; all three count as missing and are reported separately.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from automation_toolkit import __version__
from automation_toolkit.config import Config, check_chunk_size
from automation_toolkit.exceptions import IngestionError

#: Incremented when the rendered mapping changes in a way consumers must notice.
#: Version 2: unique column ``name`` plus original ``header``, ``mixed`` type,
#: missing-value breakdown, dominant type, type examples and chunk metadata.
SCHEMA_VERSION = 2
DEFAULT_CHUNK_SIZE = 50_000
#: When the chunk size is not given, it is also capped so that one chunk holds about
#: this many cells; otherwise a very wide file would hold millions of strings at once.
DEFAULT_CHUNK_CELLS = 500_000
#: ...and about this many characters of cell text, so a file with very long fields
#: (free text, embedded JSON) does not hold hundreds of megabytes in one chunk.
DEFAULT_CHUNK_CHARS = 32 * 2**20
TOP_VALUE_LIMIT = 5
TYPE_EXAMPLE_LIMIT = 3
#: Distinct values per column whose *text* is retained to name the top values. Counts of
#: all distinct values are kept as 24-byte records, so top values are always exact.
TRACKED_VALUE_LIMIT = 64
#: Numbers per column kept for the median; above it the median is estimated.
MEDIAN_SAMPLE_LIMIT = 200_000
#: Median samples share this many values per file (8 bytes each, about 64 MB), so a
#: very wide numeric file keeps smaller per-column samples, but never fewer than
#: :data:`MEDIAN_SAMPLE_MIN` (rank error about 0.35%).
MEDIAN_SAMPLE_BUDGET = 8_000_000
MEDIAN_SAMPLE_MIN = 20_000
#: Text placeholders commonly used for "no value". Counted separately as ``null_like``
#: and, unless ``null_as_missing`` is set, not as missing: the toolkit preserves them
#: as text because ``NA`` may be a legitimate value.
NULL_TOKENS = frozenset({"na", "n/a", "nan", "null", "none", "nil", "-", "?"})
TRUE_TOKENS = frozenset({"true", "yes", "y", "t"})
FALSE_TOKENS = frozenset({"false", "no", "n", "f"})
#: A type group covering at least this share of a column's values dominates it: a
#: non-text group gets a "mostly <type>" warning naming the outliers; when no group
#: reaches it the column is ``mixed``.
MOSTLY_THRESHOLD = 0.9
#: Columns with at least this share of missing cells get a warning.
HIGH_MISSING_THRESHOLD = 0.5
TYPES = ("integer", "float", "boolean", "date", "string")
_INTEGER_TEXT = r"[+-]?[0-9]{1,4000}"
#: C0 control characters except tab, which is legitimate inside a quoted field.
_CONTROL_CHARS = r"[\x00-\x08\x0b-\x1f\x7f]"
_GROUPS = {
    "numeric": ("integer", "float"),
    "boolean": ("boolean",),
    "date": ("date",),
    "string": ("string",),
}


def _percent(part: int, total: int) -> float:
    return round(100.0 * part / total, 2) if total else 0.0


def _scalar(value: Any) -> Any:
    """A JSON-safe plain Python scalar (no numpy types, no NaN/inf)."""
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        if value == 0:
            return 0.0
        # Six decimals as before, but never fewer than six significant digits, so tiny
        # values (1e-9, subnormals) are not reported as 0.0.
        return round(value, max(6, 5 - math.floor(math.log10(abs(value)))))
    return value


@dataclass(frozen=True)
class ColumnProfile:
    """Statistics for one column, identified by position, unique name and raw header."""

    position: int
    name: str
    inferred_type: str
    count: int
    missing: int
    missing_percent: float
    null_like: int
    unique: int
    unique_percent: float
    type_counts: dict[str, int]
    top_values: list[dict[str, Any]] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)
    whitespace_padded: int = 0
    warnings: list[str] = field(default_factory=list)
    header: str = ""
    missing_breakdown: dict[str, int] = field(default_factory=dict)
    dominant_type: str | None = None
    dominant_share: float = 0.0
    type_examples: dict[str, list[str]] = field(default_factory=dict)
    top_values_exact: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "name": self.name,
            "header": self.header,
            "inferred_type": self.inferred_type,
            "dominant_type": self.dominant_type,
            "dominant_share": self.dominant_share,
            "count": self.count,
            "missing": self.missing,
            "missing_percent": self.missing_percent,
            "missing_breakdown": dict(self.missing_breakdown),
            "null_like": self.null_like,
            "unique": self.unique,
            "unique_percent": self.unique_percent,
            "type_counts": dict(self.type_counts),
            "type_examples": {k: list(v) for k, v in self.type_examples.items()},
            "top_values": [dict(item) for item in self.top_values],
            "top_values_exact": self.top_values_exact,
            "stats": dict(self.stats),
            "whitespace_padded": self.whitespace_padded,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DatasetProfile:
    """A data-quality summary of one dataset."""

    source: str
    rows: int
    columns: int
    total_cells: int
    missing_cells: int
    missing_cells_percent: float
    duplicate_rows: int
    duplicate_rows_percent: float
    blank_rows: int
    short_rows: int
    column_profiles: list[ColumnProfile]
    warnings: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))
    rows_with_missing: int = 0
    chunks: int = 1
    chunk_size: int | None = None
    null_as_missing: bool = False

    @property
    def empty(self) -> bool:
        return self.rows == 0

    @property
    def complete_rows(self) -> int:
        return self.rows - self.rows_with_missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "toolkit_version": __version__,
            "generated_at": self.generated_at.isoformat(),
            "source": self.source,
            "summary": {
                "rows": self.rows,
                "columns": self.columns,
                "total_cells": self.total_cells,
                "missing_cells": self.missing_cells,
                "missing_cells_percent": self.missing_cells_percent,
                "rows_with_missing": self.rows_with_missing,
                "complete_rows": self.complete_rows,
                "duplicate_rows": self.duplicate_rows,
                "duplicate_rows_percent": self.duplicate_rows_percent,
                "blank_rows": self.blank_rows,
                "short_rows": self.short_rows,
            },
            "processing": {
                "chunks": self.chunks,
                "chunk_size": self.chunk_size,
                "null_as_missing": self.null_as_missing,
            },
            "warnings": list(self.warnings),
            "columns": [column.to_dict() for column in self.column_profiles],
        }

    def to_text(self) -> str:
        """A compact, human-readable report for the terminal."""
        lines = [
            f"Profile of {self.source}",
            f"  Rows: {self.rows}  Columns: {self.columns}",
            f"  Missing cells: {self.missing_cells}/{self.total_cells} "
            f"({self.missing_cells_percent:.2f}%); rows with missing values: "
            f"{self.rows_with_missing}",
            f"  Duplicate rows: {self.duplicate_rows} ({self.duplicate_rows_percent:.2f}%)",
            f"  Blank rows: {self.blank_rows}  Short rows: {self.short_rows}",
        ]
        if self.column_profiles:
            width = max(len("Column"), *(len(c.name) for c in self.column_profiles))
            lines.append("")
            lines.append(f"  {'Column'.ljust(width)}  {'Type':<8} {'Missing':>12} {'Unique':>8}")
            for c in self.column_profiles:
                missing = f"{c.missing} ({c.missing_percent:.0f}%)"
                lines.append(
                    f"  {c.name.ljust(width)}  {c.inferred_type:<8} {missing:>12} {c.unique:>8}"
                )
        issues = list(self.warnings) + [
            f"{c.name}: {w}" for c in self.column_profiles for w in c.warnings
        ]
        if issues:
            lines.append("")
            lines.append("  Warnings:")
            lines.extend(f"  - {issue}" for issue in issues)
        return "\n".join(lines)


def _classify(values: pd.Series, date_format: str) -> np.ndarray:
    """Label every (stripped, non-missing) text value with the narrowest type it parses as.

    Returns a positional array so the caller never depends on the chunk's index being
    unique: a caller-supplied frame may repeat index labels (``pd.concat``), which would
    otherwise make label-based assignment ambiguous or raise.
    """
    kinds = np.full(len(values), "string", dtype=object)
    if values.empty:
        return kinds
    numeric = pd.to_numeric(values, errors="coerce").to_numpy(dtype=float)
    finite = np.isfinite(numeric)
    # ASCII digits only ("\d" would accept other scripts' digits, which pandas cannot
    # parse). Integers are recognized by their text, so a 400-digit integer is still an
    # integer although it exceeds the float range; past Python's int-string limit it
    # stays text.
    is_int = values.str.fullmatch(_INTEGER_TEXT).to_numpy(dtype=bool)
    kinds[finite] = "float"
    kinds[is_int] = "integer"
    finite |= is_int
    lowered = values.str.lower()
    kinds[~finite & lowered.isin(TRUE_TOKENS | FALSE_TOKENS).to_numpy()] = "boolean"
    rest = np.flatnonzero(kinds == "string")
    if rest.size:
        parsed = _parse_dates(values.iloc[rest], date_format)
        kinds[rest[parsed.notna().to_numpy()]] = "date"
    return kinds


def _to_datetime(values: pd.Series, date_format: str) -> pd.Series:
    """Parse with one format; never raise, and always return timezone-naive values.

    Values with different UTC offsets (``...+02:00`` next to ``...Z``) make pandas raise
    even with ``errors="coerce"``. Those are parsed as UTC instead, and any aware
    result is converted to naive UTC so it can be compared with naive dates.
    """
    try:
        parsed = pd.to_datetime(values, format=date_format, errors="coerce")
    except (ValueError, TypeError, OverflowError):
        parsed = pd.to_datetime(values, format=date_format, errors="coerce", utc=True)
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert(None)
    return parsed


def _parse_dates(values: pd.Series, date_format: str) -> pd.Series:
    """Timestamps (object dtype, ``NaT`` where unparsed) trying each format in turn.

    Each fallback parses only what the previous formats left, and results are combined
    as Timestamp objects: the formats can yield different resolutions (``1500-06-01``
    needs microseconds, ``...00.123456789`` nanoseconds), and merging those arrays with
    ``fillna`` is deprecated in pandas and slated to raise.
    """
    result = np.full(len(values), pd.NaT, dtype=object)
    remaining = np.arange(len(values))
    formats = [date_format] + (["%Y-%m-%d"] if date_format != "%Y-%m-%d" else []) + ["ISO8601"]
    for fmt in formats:
        if not remaining.size:
            break
        parsed = _to_datetime(values.iloc[remaining], fmt)
        ok = parsed.notna().to_numpy()
        result[remaining[ok]] = parsed[ok].astype(object).to_numpy()
        remaining = remaining[~ok]
    return pd.Series(result, index=values.index, dtype=object)


def _strict_type(type_counts: dict[str, int], present: int) -> str | None:
    if present == 0:
        return "empty"
    if type_counts["integer"] == present:
        return "integer"
    if type_counts["integer"] + type_counts["float"] == present:
        return "float"
    for kind in ("boolean", "date", "string"):
        if type_counts[kind] == present:
            return kind
    return None


class _HashSet:
    """Distinct 64-bit hashes stored compactly in numpy arrays, deduplicated lazily."""

    def __init__(self) -> None:
        self._parts: list[np.ndarray] = []
        self._pending = 0
        self._unique = 0

    def add(self, hashes: np.ndarray) -> None:
        if hashes.size == 0:
            return
        self._parts.append(np.unique(hashes))
        self._pending += self._parts[-1].size
        # Compact on pending volume, and on part count so that many tiny chunks (a
        # low-cardinality column) cannot pile up thousands of small arrays.
        if self._pending > max(self._unique, 1_000_000) or len(self._parts) > 64:
            self._compact()

    def _compact(self) -> None:
        merged = np.unique(np.concatenate(self._parts)) if self._parts else np.empty(0, "u8")
        self._parts = [merged]
        self._unique = merged.size
        self._pending = 0

    def __len__(self) -> int:
        self._compact()
        return self._unique


def _mix64(values: np.ndarray) -> np.ndarray:
    """SplitMix64 finalizer: a fast, well-distributed hash of 64-bit integers."""
    with np.errstate(over="ignore"):
        x = values.astype(np.uint64, copy=True)
        x ^= x >> np.uint64(30)
        x *= np.uint64(0xBF58476D1CE4E5B9)
        x ^= x >> np.uint64(27)
        x *= np.uint64(0x94D049BB133111EB)
        x ^= x >> np.uint64(31)
    return x


#: Moments are accumulated at three power-of-two scales at once. Scaling by a power of
#: two is exact, so the unscaled result is used whenever it is representable; the
#: down-scaled one rescues values near the float limit (``1e308`` and ``-1e308``) and the
#: up-scaled one rescues tiny spreads whose squares underflow to zero (``1e-200``).
_SCALES = (1.0, 2.0**-600, 2.0**600)
_INT64_SAFE = 2**62


class _Numbers:
    """Streaming count, min, max, mean, variance (Chan et al.) and a median sketch.

    Moments are accumulated on ``value - shift`` (the first value seen), so large values
    with a small spread keep their precision. For integer columns the difference is
    taken exactly in integer arithmetic before converting to float, so ``10**20 + 1``
    and ``10**20 + 2`` have a standard deviation of 0.707 rather than 0.

    The median is exact while at most :data:`MEDIAN_SAMPLE_LIMIT` values have been seen.
    Above that it is estimated from a bottom-k sketch: every value is given the hash of
    its *position in the column* as a priority and the k smallest priorities are kept.
    Since a value's priority depends only on its position, the retained sample - and so
    the estimate - is identical for any chunk size, and it is a uniform random sample of
    the column rather than one biased toward the rows seen last. Integer samples are
    kept as integers, so the median of integers beyond 2**53 is exact.
    """

    def __init__(self, limit: int | None = None) -> None:
        #: Values kept for the median; read at construction so tests can patch the module.
        self.limit = MEDIAN_SAMPLE_LIMIT if limit is None else limit
        self.n = 0
        self.min = math.inf
        self.max = -math.inf
        self.shift: int | float | None = None
        #: Per scale: [mean, m2, valid]; a scale becomes invalid on overflow.
        self._moments = [[0.0, 0.0, True] for _ in _SCALES]
        self.sample: np.ndarray = np.empty(0, dtype=np.int64)
        #: Exact sum while every value is an integer (``None`` once a float arrives), so
        #: the mean of large integers does not suffer float cancellation.
        self.int_sum: int | None = 0
        #: Priorities of ``sample``; allocated only once the limit is exceeded.
        self._priorities: np.ndarray | None = None

    def add(
        self,
        values: np.ndarray,
        exact: np.ndarray | None = None,
        sample: np.ndarray | None = None,
    ) -> None:
        """Fold in float ``values``; ``exact`` holds the same values as integers, if all are.

        ``exact`` is an ``int64`` array, or an ``object`` array of Python ints when a
        value does not fit in 64 bits. ``sample`` is the per-value representation kept
        for the median and the extremes (defaults to ``exact``, else ``values``).
        """
        if values.size == 0:
            return
        if self.shift is None:
            self.shift = int(exact[0]) if exact is not None else float(values[0])
        n_b = values.size
        total = self.n + n_b
        if exact is None:
            self.int_sum = None
        elif self.int_sum is not None:
            self.int_sum += _exact_sum(exact)
        deltas = self._deltas(values, exact)
        with np.errstate(over="ignore", under="ignore", invalid="ignore"):
            for scale, moments in zip(_SCALES, self._moments, strict=True):
                if not moments[2]:
                    continue
                if deltas is not None:
                    z = deltas * scale
                else:
                    z = values * scale - _float(self.shift) * scale
                mean_b = float(np.mean(z))
                m2_b = float(np.sum((z - mean_b) ** 2))
                delta = mean_b - moments[0]
                mean = moments[0] + delta * n_b / total
                m2 = moments[1] + m2_b + delta * delta * self.n * n_b / total
                if math.isfinite(mean) and math.isfinite(m2):
                    moments[0], moments[1] = mean, m2
                else:
                    moments[2] = False
        if sample is None:
            sample = exact if exact is not None else values
        if sample.dtype == object:  # exact Python comparisons of ints and floats
            items = sample.tolist()
            low, high = min(items), max(items)
        else:
            low, high = sample.min().item(), sample.max().item()
        self.min = low if self.n == 0 else min(self.min, low)
        self.max = high if self.n == 0 else max(self.max, high)
        positions = np.arange(self.n, total, dtype=np.uint64)
        self.n = total
        self._add_sample(sample, positions)

    def _deltas(self, values: np.ndarray, exact: np.ndarray | None) -> np.ndarray | None:
        """Exact integer differences from ``shift`` as floats, or None for float data."""
        if exact is None or not isinstance(self.shift, int):
            return None
        if exact.dtype != object and abs(self.shift) < _INT64_SAFE:
            low, high = int(exact.min()), int(exact.max())
            if -_INT64_SAFE < low and high < _INT64_SAFE:
                return (exact - np.int64(self.shift)).astype(float)
        out = np.empty(exact.size, dtype=float)
        for i, value in enumerate(exact.tolist()):
            try:
                out[i] = float(value - self.shift)
            except OverflowError:  # a difference beyond the float range
                out[i] = math.inf
        return out

    def _add_sample(self, values: np.ndarray, positions: np.ndarray) -> None:
        # Common dtype: int64 and float64 meet as float64 (the column has floats, so float
        # precision applies); anything meeting Python ints beyond int64 (object) becomes
        # object, because converting those to float would overflow.
        if self.sample.dtype != values.dtype:
            if object in (self.sample.dtype, values.dtype):
                self.sample, values = self.sample.astype(object), values.astype(object)
            else:
                self.sample, values = self.sample.astype(float), values.astype(float)
        if self._priorities is None and self.n <= self.limit:
            self.sample = np.concatenate([self.sample, values])
            return
        if self._priorities is None:
            # First overflow: give the values kept so far their positional priorities.
            self._priorities = _mix64(np.arange(self.sample.size, dtype=np.uint64))
        self._keep_smallest(
            np.concatenate([self.sample, values]),
            np.concatenate([self._priorities, _mix64(positions)]),
        )

    def _keep_smallest(self, values: np.ndarray, priorities: np.ndarray) -> None:
        if values.size > self.limit:
            keep = np.argpartition(priorities, self.limit)[: self.limit]
            # Sort by priority so the result never depends on argpartition's tie order.
            keep = keep[np.argsort(priorities[keep], kind="stable")]
            values, priorities = values[keep], priorities[keep]
        self.sample, self._priorities = values, priorities

    def _choose(self) -> int | None:
        """Index of the scale to report: unscaled when representable and not underflowed."""
        valid = [i for i, m in enumerate(self._moments) if m[2]]
        if not valid:
            return None
        spread = self.max > self.min
        for i in valid:
            if not spread or self.n < 2 or self._moments[i][1] > 0:
                return i
        return valid[0]

    def median(self) -> int | float | None:
        size = self.sample.size
        if not size:
            return None
        ordered = np.sort(self.sample, kind="stable")
        mid = size // 2
        if size % 2:
            value = ordered[mid]
            value = value.item() if hasattr(value, "item") else value
        else:
            low, high = ordered[mid - 1], ordered[mid]
            low = low.item() if hasattr(low, "item") else low
            high = high.item() if hasattr(high, "item") else high
            if isinstance(low, float) and isinstance(high, float):
                value = low / 2 + high / 2  # never overflows, unlike (low + high) / 2
            else:
                exact = (Fraction(low) + Fraction(high)) / 2
                if exact.denominator == 1:
                    value = int(exact)
                else:
                    try:
                        value = float(exact)
                    except OverflowError:  # a .5 midpoint beyond the float range
                        value = int(exact)
        if isinstance(value, int) and abs(value) <= 2**53:
            value = float(value)  # keep the established float type where it is exact
        return value

    def stats(self) -> dict[str, Any]:
        chosen = self._choose()
        mean = std = None
        if chosen is not None:
            scale = _SCALES[chosen]
            mean_z, m2_z, _ = self._moments[chosen]
            with np.errstate(over="ignore"):
                try:
                    mean = _float(self.shift) + mean_z / scale
                except OverflowError:
                    mean = None
                if self.n > 1:
                    std = math.sqrt(m2_z / (self.n - 1)) / scale
        if self.int_sum is not None and self.n:
            # Exact for integers, even when the float moments overflowed.
            try:
                mean = float(Fraction(self.int_sum, self.n))  # correctly rounded
            except OverflowError:
                mean = None
        # Chunked accumulation differs from a single pass by float rounding in the last
        # digits; keep 15 significant digits (of the data's magnitude for the mean, where
        # cancellation happens) so the report is identical at every chunk size.
        magnitude = max(abs(_float(self.min)), abs(_float(self.max))) if self.n else 0.0
        if self.int_sum is None:
            mean = _significant(mean, magnitude)
        std = _significant(std, std)
        return {
            "min": _scalar(self.min),
            "max": _scalar(self.max),
            "mean": _scalar(mean),
            "median": _scalar(self.median()),
            "median_approximate": self.n > self.limit,
            "std": _scalar(std),
        }


#: Value text held between compactions, shared by all columns of a file (each column
#: gets between ``_PENDING_MIN`` and ``_PENDING_MAX`` of it).
_PENDING_BUDGET = 262_144
_PENDING_MIN = 1_024
_PENDING_MAX = 32_768
_U32_MAX = 2**32 - 1


def _pending_cap(width: int) -> int:
    return min(_PENDING_MAX, max(_PENDING_MIN, _PENDING_BUDGET // max(width, 1)))


class _ValueCounter:
    """Exact value counts in memory proportional to distinct values, not to their text.

    Each distinct value is stored as 16 bytes: its 64-bit text hash, a 32-bit count and
    a 32-bit first-appearance rank (the tie-break, so equal counts list in file order);
    either widens to 64 bits if a file ever needs it. Only the text of the
    :data:`TRACKED_VALUE_LIMIT` best-ranked values is kept.

    That is enough for exact top values: counts only grow, so whatever ranks above a
    value at any moment still ranks above it at the end. A value in the final top N was
    therefore in the top N - and had its text kept - at its last occurrence, and it can
    never be pruned afterwards. The result does not depend on the chunk size.
    """

    def __init__(self, pending_cap: int = _PENDING_MAX) -> None:
        self._hashes = np.empty(0, dtype=np.uint64)
        self._counts = np.empty(0, dtype=np.uint32)
        self._rank = np.empty(0, dtype=np.uint32)
        self._next_rank = 0
        self._text: dict[int, str] = {}
        self._pending: list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        self._pending_size = 0
        self._cap = pending_cap

    def add(
        self, hashes: np.ndarray, counts: np.ndarray, first: np.ndarray, text: np.ndarray
    ) -> None:
        """Fold in distinct values with their counts and first row positions."""
        if hashes.size == 0:
            return
        self._pending.append((hashes, counts, first, text))
        self._pending_size += hashes.size
        # Compaction is O(distinct values), so wait for a comparable amount of new data;
        # the cap bounds how much value text is held between compactions.
        limit = max(self._cap // 4, min(self._hashes.size, self._cap))
        if self._pending_size >= limit or len(self._pending) > 256:
            self._compact()

    def _compact(self) -> None:
        if not self._pending:
            return
        hashes = np.concatenate([part[0] for part in self._pending])
        counts = np.concatenate([part[1] for part in self._pending]).astype(np.int64)
        first = np.concatenate([part[2] for part in self._pending]).astype(np.int64)
        text = np.concatenate([part[3] for part in self._pending])
        self._pending, self._pending_size = [], 0

        order = np.argsort(hashes, kind="stable")
        hashes, counts, first, text = hashes[order], counts[order], first[order], text[order]
        starts = np.flatnonzero(np.r_[True, hashes[1:] != hashes[:-1]])
        new_hashes = hashes[starts]
        new_counts = np.add.reduceat(counts, starts)
        new_first = np.minimum.reduceat(first, starts)
        new_text = text[starts]

        where = np.searchsorted(self._hashes, new_hashes)
        clipped = np.minimum(where, max(self._hashes.size - 1, 0))
        found = (where < self._hashes.size) & (
            self._hashes[clipped] == new_hashes if self._hashes.size else False
        )
        hit, miss = where[found], ~found
        if self._counts.dtype == np.uint32 and new_counts.size:
            peak = int(new_counts.max()) + (int(self._counts[hit].max()) if hit.size else 0)
            if peak > _U32_MAX:
                self._counts = self._counts.astype(np.int64)
        self._counts[hit] += new_counts[found].astype(self._counts.dtype)
        # Values already stored were first seen before anything pending, so only new
        # values get ranks, in order of their first row position.
        new_ranks = np.empty(int(miss.sum()), dtype=np.int64)
        new_ranks[np.argsort(new_first[miss], kind="stable")] = np.arange(new_ranks.size)
        new_ranks += self._next_rank
        self._next_rank += new_ranks.size
        if self._rank.dtype == np.uint32 and self._next_rank > _U32_MAX:
            self._rank = self._rank.astype(np.int64)
        self._hashes = np.insert(self._hashes, where[miss], new_hashes[miss])
        self._counts = np.insert(
            self._counts, where[miss], new_counts[miss].astype(self._counts.dtype)
        )
        self._rank = np.insert(self._rank, where[miss], new_ranks.astype(self._rank.dtype))

        keep = self._best(max(TRACKED_VALUE_LIMIT, TOP_VALUE_LIMIT))
        pending_text = dict(zip(new_hashes.tolist(), new_text.tolist(), strict=True))
        texts: dict[int, str] = {}
        for h in self._hashes[keep].tolist():
            value = self._text.get(h, pending_text.get(h))
            if value is not None:
                texts[h] = value
        self._text = texts

    def _best(self, limit: int) -> np.ndarray:
        """Indices of the ``limit`` best (count desc, first occurrence asc), unordered."""
        size = self._counts.size
        if size <= limit:
            return np.arange(size)
        threshold = np.partition(self._counts, size - limit)[size - limit]
        above = np.flatnonzero(self._counts > threshold)
        ties = np.flatnonzero(self._counts == threshold)
        need = limit - above.size
        if ties.size > need:
            ties = ties[np.argpartition(self._rank[ties], need - 1)[:need]]
        return np.concatenate([above, ties])

    def __len__(self) -> int:
        self._compact()
        return int(self._hashes.size)

    def top(self, limit: int) -> list[tuple[str, int]]:
        self._compact()
        best = self._best(limit)
        counts = self._counts[best].astype(np.int64)  # unsigned counts cannot be negated
        best = best[np.lexsort((self._rank[best], -counts))]
        return [
            (self._text[h], c)
            for h, c in zip(self._hashes[best].tolist(), self._counts[best].tolist(), strict=True)
            if h in self._text
        ]


class _ColumnAccumulator:
    def __init__(
        self,
        position: int,
        header: str,
        name: str,
        date_format: str,
        null_tokens: frozenset[str],
        null_as_missing: bool,
        sample_limit: int | None = None,
        pending_cap: int = _PENDING_MAX,
    ) -> None:
        self.position = position
        self.header = header
        self.name = name
        self.date_format = date_format
        self.null_tokens = null_tokens
        self.null_as_missing = null_as_missing
        self.empty = self.whitespace = self.absent = 0
        self.null_like = self.padded = self.observed = self.leading_zeros = 0
        self.control_chars = 0
        self.type_counts = dict.fromkeys(TYPES, 0)
        self.examples: dict[str, list[str]] = {kind: [] for kind in TYPES}
        self.values = _ValueCounter(pending_cap)
        self.numbers = _Numbers(sample_limit)
        #: Rows seen before the current chunk: global positions for first occurrences.
        self.offset = 0
        self.date_min: pd.Timestamp | None = None
        self.date_max: pd.Timestamp | None = None
        self.len_min: int | None = None
        self.len_max: int | None = None
        #: Exact integer extremes as Python ints; floats lose digits above 2**53.
        self.int_min: int | None = None
        self.int_max: int | None = None

    def update(self, raw: pd.Series) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Fold one chunk in.

        Returns per-row masks (missing, blank, absent) and per-row hashes of the stripped
        cell text, so the caller can detect blank and duplicate rows without touching
        the strings again.

        All string work happens once per *distinct* value in the chunk rather than once
        per cell: the column is factorized, the distinct values are stripped, typed and
        parsed, and the results are broadcast back to the rows through integer codes.
        ``pd.factorize`` lists values in order of first appearance, so everything that
        depends on file order (type examples, which values are tracked once the limit is
        reached, the positions used by the median sketch) is unchanged.
        """
        codes, uniques = pd.factorize(raw.to_numpy(dtype=object), use_na_sentinel=True)
        absent = codes == -1
        # Code -1 (an absent field) indexes the extra last slot, which holds "".
        text_u = np.append(pd.Series(uniques, dtype=object).astype(str).to_numpy(object), "")
        stripped_s = pd.Series(text_u, dtype=object).str.strip()
        stripped_u = stripped_s.to_numpy(dtype=object)
        blank_u = stripped_u == ""
        empty_u = text_u == ""
        padded_u = (text_u != stripped_u) & ~blank_u
        control_u = stripped_s.str.contains(_CONTROL_CHARS, regex=True).to_numpy(dtype=bool)

        is_blank = blank_u[codes]
        is_empty = empty_u[codes] & ~absent
        self.absent += int(absent.sum())
        self.empty += int(is_empty.sum())
        self.whitespace += int((is_blank & ~is_empty & ~absent).sum())
        self.padded += int(padded_u[codes].sum())
        # NUL and other C0 controls survive CSV parsing and usually mean a corrupt or
        # binary file, so they are reported rather than silently profiled as text.
        self.control_chars += int(control_u[codes].sum())
        row_hash = pd.util.hash_array(stripped_u)[codes]

        # Second level: distinct non-blank *stripped* values (" a" and "a" merge here).
        keep_u = np.flatnonzero(~blank_u)
        value_codes, vals = pd.factorize(stripped_u[keep_u])
        to_value = np.full(len(text_u), -1, dtype=np.intp)
        to_value[keep_u] = value_codes
        row_codes = to_value[codes]
        present_codes = row_codes[row_codes >= 0]  # in row order
        weights = np.bincount(present_codes, minlength=len(vals))
        vals_s = pd.Series(vals, dtype=object)

        is_null_v = vals_s.str.lower().isin(self.null_tokens).to_numpy(dtype=bool)
        self.null_like += int(weights[is_null_v].sum())
        missing = is_blank.copy()
        if self.null_as_missing:
            has_value = row_codes >= 0
            missing[has_value] |= is_null_v[row_codes[has_value]]
            observed_v = ~is_null_v
        else:
            observed_v = np.ones(len(vals), dtype=bool)
        self.observed += int(weights[observed_v].sum())

        typed_idx = np.flatnonzero(~is_null_v)
        kinds_v = np.full(len(vals), "", dtype=object)
        kinds_v[typed_idx] = _classify(pd.Series(vals[typed_idx], dtype=object), self.date_format)
        for kind in TYPES:
            of_kind = kinds_v == kind
            self.type_counts[kind] += int(weights[of_kind].sum())
            examples = self.examples[kind]
            if len(examples) < TYPE_EXAMPLE_LIMIT and of_kind.any():
                for value in vals[of_kind]:
                    if value not in examples:  # may already come from an earlier chunk
                        examples.append(value)
                        if len(examples) == TYPE_EXAMPLE_LIMIT:
                            break

        numeric_v = np.isin(kinds_v, _GROUPS["numeric"])
        integer_v = kinds_v == "integer"
        integer_cache: np.ndarray | None = None
        if numeric_v.any():
            with np.errstate(over="ignore"):
                number_v = np.full(len(vals), np.nan)
                number_v[numeric_v] = pd.to_numeric(vals_s[numeric_v], errors="coerce").to_numpy(
                    dtype=float
                )
            row_numeric = present_codes[numeric_v[present_codes]]
            # The value kept for each number must not depend on what shares its chunk:
            # integers are exact (int64, or Python ints beyond it), floats are floats, and
            # int64 meets float64 as float64 - in one chunk or across several alike.
            sample_v = number_v
            exact = None
            if integer_v.any():
                integer_cache = _exact_ints(vals_s[integer_v])
                floats = numeric_v & ~integer_v
                if not floats.any() or integer_cache.dtype == object:
                    sample_v = np.zeros(len(vals), dtype=integer_cache.dtype)
                    if floats.any():
                        sample_v[floats] = number_v[floats].tolist()
                    sample_v[integer_v] = integer_cache
                if not floats.any():
                    exact = sample_v[row_numeric]  # only integers: exact moments too
            self.numbers.add(number_v[row_numeric], exact, sample_v[row_numeric])
        if integer_v.any():
            integers = vals_s[integer_v]
            exact_ints = integer_cache if integer_cache is not None else _exact_ints(integers)
            low, high = int(exact_ints.min()), int(exact_ints.max())
            self.int_min = low if self.int_min is None else min(self.int_min, low)
            self.int_max = high if self.int_max is None else max(self.int_max, high)
            zeros = integers.str.fullmatch(r"[+-]?0\d+").to_numpy(dtype=bool)
            self.leading_zeros += int(weights[integer_v][zeros].sum())
        date_v = kinds_v == "date"
        if date_v.any():
            parsed = _parse_dates(vals_s[date_v], self.date_format).dropna().tolist()
            low, high = min(parsed), max(parsed)
            self.date_min = low if self.date_min is None else min(self.date_min, low)
            self.date_max = high if self.date_max is None else max(self.date_max, high)

        if observed_v.any():
            observed = vals_s[observed_v]
            lengths = observed.str.len()
            low_len, high_len = int(lengths.min()), int(lengths.max())
            self.len_min = low_len if self.len_min is None else min(self.len_min, low_len)
            self.len_max = high_len if self.len_max is None else max(self.len_max, high_len)
            # vals are in order of first appearance, so the first occurrence of value k
            # is where the running maximum of the per-row codes first reaches k.
            rows_with_value = np.flatnonzero(row_codes >= 0)
            seen = np.maximum.accumulate(row_codes[rows_with_value])
            first_v = rows_with_value[np.flatnonzero(np.r_[True, seen[1:] > seen[:-1]])]
            self.values.add(
                pd.util.hash_array(observed.to_numpy(dtype=object)),
                weights[observed_v],
                (first_v + self.offset)[observed_v],
                observed.to_numpy(dtype=object),
            )
        self.offset += len(codes)
        return missing, is_blank, absent, row_hash

    def finish(self, rows: int) -> ColumnProfile:
        nulls_missing = self.null_like if self.null_as_missing else 0
        missing = self.empty + self.whitespace + self.absent + nulls_missing
        count = self.observed
        typed = sum(self.type_counts.values())
        unique = len(self.values)

        group_counts = {g: sum(self.type_counts[k] for k in ks) for g, ks in _GROUPS.items()}
        dominant_group = max(group_counts, key=lambda g: group_counts[g]) if typed else None
        share = group_counts[dominant_group] / typed if dominant_group else 0.0
        dominant: str | None = None
        if dominant_group == "numeric":
            dominant = "float" if self.type_counts["float"] else "integer"
        elif dominant_group:
            dominant = dominant_group
        inferred = _strict_type(self.type_counts, typed)
        if inferred is None:
            # One group dominating (a few outliers) stays "string" with a "mostly" warning;
            # several substantial groups with none dominating make the column "mixed".
            inferred = "string" if share >= MOSTLY_THRESHOLD else "mixed"

        ranked = self.values.top(TOP_VALUE_LIMIT)
        top_values = [
            {"value": value, "count": n}
            for value, n in ranked
            if n > 1 or unique <= TOP_VALUE_LIMIT
        ]

        stats: dict[str, Any] = {}
        stats_kind = dominant if inferred == "mixed" else inferred
        if stats_kind in ("integer", "float") and self.numbers.n:
            stats = self.numbers.stats()
            if stats_kind == "integer" and self.int_min is not None:
                stats["min"], stats["max"] = self.int_min, self.int_max
            elif stats_kind == "float":
                # Integers inside a float column keep the float type a float column reports
                # (whether or not they shared a chunk with floats), unless beyond its range.
                for key in ("min", "max"):
                    value = stats[key]
                    if isinstance(value, int) and abs(_float(value)) != math.inf:
                        stats[key] = float(value)
        elif stats_kind == "date" and self.date_min is not None:
            stats = {"min": self.date_min.isoformat(), "max": self.date_max.isoformat()}
        elif self.len_min is not None:
            stats = {"min_length": self.len_min, "max_length": self.len_max}
        if inferred == "mixed" and stats:
            stats["of"] = f"{dominant} values only"

        warnings: list[str] = []
        if rows and missing == rows:
            warnings.append("column is entirely empty")
        elif rows and missing / rows >= HIGH_MISSING_THRESHOLD:
            warnings.append(f"{_percent(missing, rows):.0f}% of values are missing")
        if self.whitespace:
            warnings.append(f"{self.whitespace} whitespace-only value(s) counted as missing")
        if count > 1 and unique == 1:
            warnings.append("column has a single constant value")
        if self.null_like:
            how = "counted as missing" if self.null_as_missing else "such as NA/null/-"
            warnings.append(f"{self.null_like} placeholder value(s) {how}")
        if self.padded:
            warnings.append(f"{self.padded} value(s) with leading or trailing whitespace")
        if (
            stats_kind in ("integer", "float")
            and self.numbers.n
            and (stats.get("mean") is None or (self.numbers.n > 1 and stats.get("std") is None))
        ):
            warnings.append("values are too large for a float mean and standard deviation")
        if self.control_chars:
            warnings.append(
                f"{self.control_chars} value(s) contain control characters "
                "(NUL or similar); the file may be corrupt or binary"
            )
        if self.leading_zeros:
            warnings.append(
                f"{self.leading_zeros} integer value(s) with leading zeros; "
                "treat as text identifiers to keep them"
            )
        if inferred == "mixed":
            parts = ", ".join(
                f"{_percent(n, typed):.1f}% {g}"
                for g, n in sorted(group_counts.items(), key=lambda item: -item[1])
                if n
            )
            warnings.append(f"mixed types: {parts}")
        if inferred in ("string", "mixed") and dominant_group not in (None, "string"):
            if share >= MOSTLY_THRESHOLD:
                outliers = [
                    value
                    for kind in TYPES
                    if kind not in _GROUPS[dominant_group]
                    for value in self.examples[kind]
                ][:TYPE_EXAMPLE_LIMIT]
                examples = ", ".join(repr(v) for v in outliers)
                warnings.append(
                    f"mostly {dominant_group} ({share:.1%}) but has non-conforming values: "
                    f"{examples}"
                )

        return ColumnProfile(
            position=self.position,
            name=self.name,
            header=self.header,
            inferred_type=inferred,
            dominant_type=dominant,
            dominant_share=round(share, 4),
            count=count,
            missing=missing,
            missing_percent=_percent(missing, rows),
            missing_breakdown={
                "empty": self.empty,
                "whitespace": self.whitespace,
                "absent": self.absent,
                "null_like": nulls_missing,
            },
            null_like=self.null_like,
            unique=unique,
            unique_percent=_percent(unique, count),
            type_counts=dict(self.type_counts),
            type_examples={k: list(v) for k, v in self.examples.items() if v},
            top_values=top_values,
            top_values_exact=True,
            stats=stats,
            whitespace_padded=self.padded,
            warnings=warnings,
        )


def _column_names(headers: list[str]) -> tuple[list[str], list[str]]:
    """Unique display names plus dataset warnings for blank and duplicate headers.

    Blank headers become ``column_<position>``. Repeated headers keep the first
    occurrence and suffix later ones with ``_2``, ``_3``..., skipping names already
    taken, so every column stays addressable in the report and the JSON.
    """
    warnings: list[str] = []
    names: list[str] = []
    taken = {h for h in headers if h}
    seen: dict[str, int] = {}
    renamed: list[str] = []
    for position, header in enumerate(headers, start=1):
        if not header:
            name = f"column_{position}"
            while name in taken:
                name += "_"
            taken.add(name)
            names.append(name)
            continue
        seen[header] = seen.get(header, 0) + 1
        if seen[header] == 1:
            names.append(header)
            continue
        suffix = seen[header]
        while f"{header}_{suffix}" in taken:
            suffix += 1
        name = f"{header}_{suffix}"
        taken.add(name)
        names.append(name)
        renamed.append(f"{header!r} at position {position} -> {name}")
    blank = [names[i] for i, header in enumerate(headers) if not header]
    if blank:
        warnings.append(f"blank header(s) renamed to {', '.join(blank)}")
    duplicated = sorted(h for h, n in seen.items() if n > 1)
    if duplicated:
        warnings.append(
            f"duplicate header(s): {', '.join(duplicated)}; renamed {'; '.join(renamed)}"
        )
    folded: dict[str, list[str]] = {}
    for header in dict.fromkeys(h for h in headers if h):
        folded.setdefault(" ".join(header.lower().split()), []).append(header)
    similar = [group for group in folded.values() if len(group) > 1]
    if similar:
        text = "; ".join(" / ".join(repr(h) for h in group) for group in similar)
        warnings.append(f"headers differing only in case or spacing: {text}")
    return names, warnings


class DatasetProfiler:
    """Incremental profiler: feed raw chunks with :meth:`update`, then call :meth:`result`.

    Each chunk is a DataFrame whose columns are in header order (by position); cells
    are raw text, and ``None``/``NaN`` marks a field absent because the record was short.
    """

    def __init__(
        self,
        headers: Iterable[Any],
        source: str = "<dataframe>",
        config: Config | None = None,
        *,
        null_tokens: Iterable[str] | None = None,
        null_as_missing: bool = False,
        chunk_size: int | None = None,
    ) -> None:
        config = config or Config()
        self.source = source
        self.chunk_size = chunk_size
        self.null_as_missing = null_as_missing
        self.headers = ["" if _isna(h) else str(h).strip() for h in headers]
        self.names, self.warnings = _column_names(self.headers)
        tokens = NULL_TOKENS | {t.strip().lower() for t in (null_tokens or ()) if t.strip()}
        sample_limit = _sample_limit(len(self.headers))
        self.columns = [
            _ColumnAccumulator(
                i + 1,
                header,
                name,
                config.date_format,
                frozenset(tokens),
                null_as_missing,
                sample_limit,
                _pending_cap(len(self.headers)),
            )
            for i, (header, name) in enumerate(zip(self.headers, self.names, strict=True))
        ]
        self.rows = self.blank_rows = self.short_rows = self.rows_with_missing = 0
        self.chunks = 0
        self.row_hashes = _HashSet()
        self.content_rows = 0
        self._short_override: int | None = None

    def update(self, chunk: pd.DataFrame) -> None:
        width = len(self.columns)
        if chunk.shape[1] != width:
            raise ValueError(f"chunk has {chunk.shape[1]} columns; expected {width}")
        rows = len(chunk)
        self.chunks += 1
        if rows == 0:
            return
        self.rows += rows
        if width == 0:
            self.blank_rows += rows
            return
        positional = chunk.set_axis(range(width), axis=1)
        any_missing = np.zeros(rows, dtype=bool)
        blank = np.ones(rows, dtype=bool)
        short = np.zeros(rows, dtype=bool)
        hashes: list[np.ndarray] = []
        for i, column in enumerate(self.columns):
            missing, is_blank, absent, row_hash = column.update(positional[i])
            any_missing |= missing
            blank &= is_blank
            short |= absent
            hashes.append(row_hash)
        self.rows_with_missing += int(any_missing.sum())
        self.blank_rows += int(blank.sum())
        self.short_rows += int((short & ~blank).sum())
        keep = ~blank
        self.content_rows += int(keep.sum())
        if keep.any():
            self.row_hashes.add(_row_hashes(hashes, keep))

    def result(self) -> DatasetProfile:
        width = len(self.columns)
        columns = [column.finish(self.rows) for column in self.columns]
        short_rows = self.short_rows if self._short_override is None else self._short_override
        duplicate_rows = self.content_rows - len(self.row_hashes) if width else 0
        missing_cells = sum(c.missing for c in columns)
        total_cells = self.rows * width
        warnings = list(self.warnings)
        if self.rows == 0:
            warnings.append("dataset has a header but no data rows")
        if short_rows:
            warnings.append(f"{short_rows} row(s) have fewer fields than the header")
        if self.blank_rows:
            warnings.append(f"{self.blank_rows} completely blank row(s)")
        if duplicate_rows:
            warnings.append(f"{duplicate_rows} duplicate row(s) repeat an earlier row exactly")
        return DatasetProfile(
            source=self.source,
            rows=self.rows,
            columns=width,
            total_cells=total_cells,
            missing_cells=missing_cells,
            missing_cells_percent=_percent(missing_cells, total_cells),
            rows_with_missing=self.rows_with_missing,
            duplicate_rows=duplicate_rows,
            duplicate_rows_percent=_percent(duplicate_rows, self.rows),
            blank_rows=self.blank_rows,
            short_rows=short_rows,
            column_profiles=columns,
            warnings=warnings,
            chunks=max(self.chunks, 1),
            chunk_size=self.chunk_size,
            null_as_missing=self.null_as_missing,
        )


def _sample_limit(width: int) -> int:
    """Per-column median sample size: the file-wide budget shared across columns."""
    shared = MEDIAN_SAMPLE_BUDGET // max(width, 1)
    return min(MEDIAN_SAMPLE_LIMIT, max(MEDIAN_SAMPLE_MIN, shared))


def _row_hashes(columns: list[np.ndarray], keep: np.ndarray) -> np.ndarray:
    """Combine the per-column cell hashes of the kept rows into one hash per row.

    The mixing step is the one pandas itself uses for multi-column hashing, so columns
    do not cancel out and the order of the columns matters.
    """
    combined = np.zeros(int(keep.sum()), dtype=np.uint64)
    with np.errstate(over="ignore"):
        for column in columns:
            combined = combined * np.uint64(1000003) ^ column[keep]
    return combined


def _float(value: int | float | None) -> float:
    """``float(value)``, but an integer beyond the float range becomes ``inf``."""
    try:
        return float(value or 0)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _exact_sum(values: np.ndarray) -> int:
    """Exact sum of an int64 or object integer array, without int64 overflow."""
    if values.dtype != object and values.size:
        bound = max(abs(int(values.min())), abs(int(values.max())))
        if bound * values.size < 2**63:
            return int(values.sum())
    return sum(int(v) for v in values.tolist())


def _significant(value: float | None, reference: float | None, digits: int = 15) -> float | None:
    """``value`` rounded to ``digits`` significant digits of ``reference``'s magnitude."""
    if value is None or reference is None or not math.isfinite(value):
        return value
    if not reference or not math.isfinite(reference):
        return value
    return round(value, digits - 1 - math.floor(math.log10(abs(reference))))


def _exact_ints(integers: pd.Series) -> np.ndarray:
    """Integer strings as ``int64``, or as Python ints when any exceeds 64 bits."""
    parsed = pd.to_numeric(integers, errors="coerce")
    if parsed.dtype.kind == "i":
        return parsed.to_numpy(dtype=np.int64)
    return np.array([int(value) for value in integers], dtype=object)


def _isna(value: Any) -> bool:
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def profile_frame(
    frame: pd.DataFrame,
    source: str = "<dataframe>",
    config: Config | None = None,
    short_rows: int | None = None,
    *,
    null_tokens: Iterable[str] | None = None,
    null_as_missing: bool = False,
) -> DatasetProfile:
    """Profile an in-memory DataFrame of raw values as a single chunk.

    Header text is taken from ``frame.columns``; columns are handled by position, so
    duplicate or blank headers are profiled individually and renamed uniquely.
    ``NaN``/``None`` cells are fields absent from a short row; empty strings are empty
    cells. ``short_rows`` overrides the short-row count when measured elsewhere.
    """
    profiler = DatasetProfiler(
        frame.columns, source, config, null_tokens=null_tokens, null_as_missing=null_as_missing
    )
    profiler._short_override = short_rows
    profiler.update(frame)
    return profiler.result()


def profile_csv(
    path: Path | str,
    config: Config | None = None,
    *,
    chunk_size: int | None = None,
    null_tokens: Iterable[str] | None = None,
    null_as_missing: bool = False,
) -> DatasetProfile:
    """Stream ``path`` in chunks with the configured delimiter and encoding and profile it.

    ``chunk_size`` defaults to ``config.csv_chunk_size``, else :data:`DEFAULT_CHUNK_SIZE`.
    Raises :class:`IngestionError` for a missing, empty, undecodable or malformed file
    (a blank header line, or a record with more fields than the header); a header-only
    file is a valid empty dataset.
    """
    path = Path(path)
    config = config or Config()
    size = check_chunk_size(chunk_size) if chunk_size is not None else config.csv_chunk_size
    if not path.is_file():
        raise IngestionError(f"Cannot profile {path}: file not found.")
    try:
        with path.open(newline="", encoding=config.csv_encoding) as handle:
            reader = csv.reader(handle, delimiter=config.csv_delimiter)
            header = next(reader, None)
            if header is None or not any(cell.strip() for cell in header):
                if header is None or not any(any(c.strip() for c in r) for r in reader):
                    raise IngestionError(
                        f"Cannot profile {path.name}: the file is empty (no header row)."
                    )
                raise IngestionError(
                    f"Cannot profile {path.name}: line 1 is blank; expected a header row."
                )
            effective = size or _default_chunk_size(len(header))
            profiler = DatasetProfiler(
                header,
                path.name,
                config,
                null_tokens=null_tokens,
                null_as_missing=null_as_missing,
                chunk_size=effective,
            )
            max_chars = None if size else DEFAULT_CHUNK_CHARS
            chunks = _record_chunks(reader, len(header), effective, path.name, max_chars)
            for chunk in chunks:
                profiler.update(chunk)
    except IngestionError:
        raise
    except (OSError, UnicodeError, LookupError, csv.Error) as exc:
        raise IngestionError(f"Cannot read CSV {path.name}: {exc}") from exc
    return profiler.result()


def _default_chunk_size(width: int) -> int:
    """Rows per chunk when the caller gave none: capped by rows and by cells."""
    return max(1, min(DEFAULT_CHUNK_SIZE, DEFAULT_CHUNK_CELLS // max(width, 1)))


def _record_chunks(
    reader: Iterator[list[str]], width: int, size: int, name: str, max_chars: int | None = None
) -> Iterator[pd.DataFrame]:
    """Yield DataFrames of at most ``size`` records (and ``max_chars`` characters, if set).

    The character cap applies only to the default chunk size: an explicit chunk size
    is a promise about records per chunk and is never split further.
    """
    batch: list[list[str]] = []
    chars = 0
    for record in reader:
        if len(record) > width:
            line = getattr(reader, "line_num", "?")
            raise IngestionError(
                f"Cannot read CSV {name}: Expected {width} fields in line {line}, "
                f"saw {len(record)}."
            )
        batch.append(record)
        if max_chars is not None:
            chars += sum(map(len, record))
        if len(batch) == size or (max_chars is not None and chars >= max_chars):
            yield _frame(batch, width)
            batch = []
            chars = 0
    if batch:
        yield _frame(batch, width)


def _frame(records: list[list[str]], width: int) -> pd.DataFrame:
    # Short records are padded with None, which marks those fields as absent. A record
    # with no fields at all is a blank line, not a short row: pad it with empty strings
    # so that "\n" and ",,," are both reported as a blank row of empty cells.
    padded = [
        record + [""] * (width - len(record))
        if not record
        else record + [None] * (width - len(record))
        for record in records
    ]
    return pd.DataFrame(padded, columns=range(width), dtype=object)
