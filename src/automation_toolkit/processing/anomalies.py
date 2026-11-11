"""Outlier detection for the numeric columns of any CSV dataset.

Like :mod:`automation_toolkit.processing.profiling`, this module is schema-agnostic:
it accepts any delimited text file, never validates or rejects rows, and only reports.
Two methods are available, both reporting the same "normal range" fence:

* ``iqr`` (the default): the first and third quartile, the interquartile range (IQR)
  and the Tukey fence ``[Q1 - factor * IQR, Q3 + factor * IQR]`` (``factor`` defaults
  to the conventional 1.5).
* ``modified_zscore``: the median and the median absolute deviation (MAD); a value is
  an outlier when its modified z-score ``0.6745 * (x - median) / MAD`` exceeds
  ``threshold`` (defaults to the conventional 3.5, Iglewicz & Hoaglin), which is the
  fence ``median +/- threshold * MAD / 0.6745``. Being median-based twice over, it
  tolerates many extreme values better than the quartile fence.

Every column lists how many values fall outside the fence, with the most extreme
examples and their row numbers.

Missing cells (empty, whitespace-only, or absent because the record was short),
placeholder text such as ``NA`` or ``null`` and non-numeric text are excluded from the
calculation and counted separately, so the result says exactly which values it is based
on. Without an explicit column selection, a column is analyzed when at least
:data:`MOSTLY_NUMERIC` of its non-missing, non-placeholder values are numeric; a column
below that is skipped with a warning naming the share, and a column with no numeric
values at all is skipped silently. Columns requested by name are always analyzed.

The file is streamed in chunks with the profiler's CSV reader, so short rows, blank
rows and malformed records are treated exactly as in ``automation-toolkit-profile``,
and the result does not depend on the chunk size. Numeric values are compared as
64-bit floats (integers beyond 2**53 lose precision; a value beyond the float range
counts as invalid). Memory is bounded by one chunk plus, per analyzed column, 8 bytes
per numeric value and 1 bit per row (which rows held those values); computing the
quartiles needs one further temporary copy of the values of one column at a time.
"""

from __future__ import annotations

import csv
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from automation_toolkit import __version__
from automation_toolkit.config import Config, check_chunk_size
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.processing.profiling import (
    DEFAULT_CHUNK_CHARS,
    NULL_TOKENS,
    _column_names,
    _default_chunk_size,
    _percent,
    _record_chunks,
)

#: Incremented when the rendered mapping changes in a way consumers must notice.
#: Version 2: ``method`` can be ``modified_zscore``; new ``threshold`` (report) and
#: ``median``/``mad`` (column) keys, ``null`` under the method that does not use them.
SCHEMA_VERSION = 2
#: Tukey's conventional fence multiplier.
DEFAULT_IQR_FACTOR = 1.5
#: Iglewicz & Hoaglin's conventional modified z-score cutoff.
DEFAULT_ZSCORE_THRESHOLD = 3.5
#: The modified z-score is ``ZSCORE_SCALE * (x - median) / MAD``: the MAD of a normal
#: distribution is 0.6745 standard deviations, so the score reads like a z-score.
ZSCORE_SCALE = 0.6745
METHODS = ("iqr", "modified_zscore")
#: Outlier examples reported per column (the most extreme first).
EXAMPLE_LIMIT = 5
#: Distinct invalid-text examples reported per column.
INVALID_EXAMPLE_LIMIT = 3
#: Quartiles of fewer values than this say little; such columns report no bounds.
MIN_VALUES = 4
#: Share of parsed values that must be numeric for a column to be auto-selected.
MOSTLY_NUMERIC = 0.9


@dataclass(frozen=True)
class ColumnOutliers:
    """The outlier analysis of one column.

    ``q1``/``q3``/``iqr`` are filled by the ``iqr`` method, ``median``/``mad`` by
    ``modified_zscore``; the other method's fields are ``None``. The fence
    (``lower_bound``/``upper_bound``), counts and examples mean the same under both.
    """

    position: int
    name: str
    header: str
    values: int
    missing: int
    null_like: int
    invalid: int
    q1: float | None
    q3: float | None
    iqr: float | None
    lower_bound: float | None
    upper_bound: float | None
    outliers: int
    outlier_percent: float
    low_outliers: int
    high_outliers: int
    examples: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    median: float | None = None
    mad: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "position": self.position,
            "name": self.name,
            "header": self.header,
            "values": self.values,
            "missing": self.missing,
            "null_like": self.null_like,
            "invalid": self.invalid,
            "q1": self.q1,
            "q3": self.q3,
            "iqr": self.iqr,
            "median": self.median,
            "mad": self.mad,
            "lower_bound": self.lower_bound,
            "upper_bound": self.upper_bound,
            "outliers": self.outliers,
            "outlier_percent": self.outlier_percent,
            "low_outliers": self.low_outliers,
            "high_outliers": self.high_outliers,
            "examples": [dict(example) for example in self.examples],
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class AnomalyReport:
    """Outlier results for one dataset.

    ``factor`` is set under the ``iqr`` method and ``threshold`` under
    ``modified_zscore``; the other is ``None``.
    """

    source: str
    rows: int
    factor: float | None
    column_results: list[ColumnOutliers]
    warnings: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now().replace(microsecond=0))
    method: str = "iqr"
    threshold: float | None = None

    @property
    def total_outliers(self) -> int:
        return sum(column.outliers for column in self.column_results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "toolkit_version": __version__,
            "generated_at": self.generated_at.isoformat(),
            "source": self.source,
            "method": self.method,
            "factor": self.factor,
            "threshold": self.threshold,
            "summary": {
                "rows": self.rows,
                "columns_analyzed": len(self.column_results),
                "total_outliers": self.total_outliers,
            },
            "warnings": list(self.warnings),
            "columns": [column.to_dict() for column in self.column_results],
        }

    def to_text(self) -> str:
        """A compact, human-readable report for the terminal."""
        if self.method == "iqr":
            how = f"IQR factor {self.factor:g}"
        else:
            how = f"modified z-score threshold {self.threshold:g}"
        lines = [
            f"Outlier scan of {self.source} ({how})",
            f"  Rows: {self.rows}  Columns analyzed: {len(self.column_results)}"
            f"  Outliers: {self.total_outliers}",
        ]
        if self.column_results:
            width = max(len("Column"), *(len(c.name) for c in self.column_results))
            lines.append("")
            lines.append(f"  {'Column'.ljust(width)}  {'Values':>8} {'Outliers':>12}  Normal range")
            for c in self.column_results:
                if c.q1 is None and c.median is None:
                    bounds = "(not computed)"
                else:
                    low = "-inf" if c.lower_bound is None else f"{c.lower_bound:g}"
                    high = "inf" if c.upper_bound is None else f"{c.upper_bound:g}"
                    bounds = f"[{low}, {high}]"
                outliers = f"{c.outliers} ({c.outlier_percent:g}%)"
                lines.append(f"  {c.name.ljust(width)}  {c.values:>8} {outliers:>12}  {bounds}")
            for c in self.column_results:
                if c.examples:
                    shown = ", ".join(f"{e['value']:g} (row {e['row']})" for e in c.examples)
                    more = c.outliers - len(c.examples)
                    suffix = f" and {more} more" if more else ""
                    lines.append(f"  {c.name}: most extreme {shown}{suffix}")
        issues = list(self.warnings) + [
            f"{c.name}: {w}" for c in self.column_results for w in c.warnings
        ]
        if issues:
            lines.append("")
            lines.append("  Warnings:")
            lines.extend(f"  - {issue}" for issue in issues)
        return "\n".join(lines)


#: Growable buffers start with blocks this small and double up to the maximum, so a
#: column with a handful of values costs a few KB, and appending never copies data.
_BLOCK_MIN = 256
_BLOCK_MAX = 1 << 16
#: Significant digits of the IQR that reported numbers always resolve (see _number).
_SPREAD_DIGITS = 3


class _Blocks:
    """An append-only array stored as fixed-size blocks.

    Unlike a list of per-chunk arrays (about 100 bytes of overhead each, so tiny chunks
    multiplied memory) or one array grown by reallocation (a temporary copy on every
    growth), filled blocks are never copied or reallocated.
    """

    def __init__(self, dtype: Any) -> None:
        self.dtype = np.dtype(dtype)
        self.blocks: list[np.ndarray] = []
        self.size = 0
        self._used = 0  # filled part of the last block

    def append(self, values: np.ndarray) -> None:
        start = 0
        while start < values.size:
            if not self.blocks or self._used == self.blocks[-1].size:
                capacity = min(_BLOCK_MAX, max(_BLOCK_MIN, self.size))
                self.blocks.append(np.empty(capacity, dtype=self.dtype))
                self._used = 0
            block = self.blocks[-1]
            take = min(block.size - self._used, values.size - start)
            block[self._used : self._used + take] = values[start : start + take]
            self._used += take
            start += take
            self.size += take

    def views(self) -> list[np.ndarray]:
        """The stored values as a list of arrays (views, no copies)."""
        if not self.blocks:
            return []
        return [*self.blocks[:-1], self.blocks[-1][: self._used]]

    def copy(self) -> np.ndarray:
        """All values as one new contiguous array."""
        out = np.empty(self.size, dtype=self.dtype)
        offset = 0
        for view in self.views():
            out[offset : offset + view.size] = view
            offset += view.size
        return out


class _RowBits:
    """One bit per row, set where the column held a numeric value."""

    def __init__(self) -> None:
        self.packed = _Blocks(np.uint8)
        self.tail = np.zeros(0, dtype=bool)  # fewer than 8 bits not yet packed

    def append(self, mask: np.ndarray) -> None:
        bits = np.concatenate([self.tail, mask]) if self.tail.size else mask
        full = bits.size - bits.size % 8
        self.packed.append(np.packbits(bits[:full]))
        self.tail = bits[full:].copy()

    def rows_of(self, ordinals: list[int]) -> list[int]:
        """Row indices (0-based) of the given 0-based ordinals among the set bits."""
        wanted = sorted(set(ordinals))
        found: dict[int, int] = {}
        parts = self.packed.views()
        if self.tail.size:
            parts.append(np.packbits(self.tail))
        seen = 0  # set bits in earlier parts
        row = 0  # rows covered by earlier parts
        for part in parts:
            if not wanted:
                break
            bits = np.unpackbits(part)
            count = int(bits.sum())
            if wanted[0] < seen + count:
                set_rows = np.flatnonzero(bits) + row
                while wanted and wanted[0] < seen + count:
                    ordinal = wanted.pop(0)
                    found[ordinal] = int(set_rows[ordinal - seen])
            seen += count
            row += bits.size
        return [found[o] for o in ordinals]


class _ColumnState:
    """Numeric values, the rows that held them and exclusion counts of one column."""

    def __init__(self) -> None:
        self.missing = 0
        self.null_like = 0
        self.invalid = 0
        self.invalid_examples: list[str] = []
        self.values = _Blocks(np.float64)
        self.rows = _RowBits()


def _number(value: float | None, spread: float | None = None) -> float | None:
    """A JSON-safe float rounded for the report; ``None`` if missing or not finite.

    Rounding is the profiler's: 6 decimals, never fewer than 6 significant digits. When
    the interquartile range ``spread`` is so small that 6 decimals cannot resolve it
    (below 0.001), more decimals are kept so it shows :data:`_SPREAD_DIGITS` significant
    digits; otherwise the quartiles, both bounds and the outliers could all print as the
    same number. Never more than 15 significant digits of the value itself, so float
    noise such as ``22.749999999999996`` is still removed.
    """
    if value is None or not math.isfinite(value):
        return None
    value = float(value)
    if value == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(value)))
    decimals = max(6, 5 - magnitude)
    if spread is not None and math.isfinite(spread) and spread > 0:
        decimals = max(decimals, _SPREAD_DIGITS - 1 - math.floor(math.log10(spread)))
    try:
        return round(value, min(decimals, 14 - magnitude))
    except OverflowError:  # rounding a value near the float limit can round past it
        return value


def _check_cutoff(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a non-negative number.")
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a non-negative number.")
    return value


def _to_floats(parsed: pd.Series) -> np.ndarray:
    """Parsed numbers as float64; a value beyond the float range becomes ``inf``."""
    if parsed.dtype != object:
        return parsed.to_numpy(dtype=float)
    out = np.empty(len(parsed), dtype=float)  # Python ints beyond int64 arrive as objects
    for i, value in enumerate(parsed.tolist()):
        try:
            out[i] = math.nan if value is None or value is pd.NA else float(value)
        except (OverflowError, TypeError, ValueError):
            out[i] = math.inf
    return out


class OutlierDetector:
    """Incremental IQR detector: feed raw chunks with :meth:`update`, call :meth:`result`.

    Chunks follow the :class:`~automation_toolkit.processing.profiling.DatasetProfiler`
    convention: columns in header order, cells as raw text, ``None``/``NaN`` marking a
    field absent because the record was short. ``first_row`` is the row number reported
    for the first data row (``2`` for a CSV, where row 1 is the header).
    """

    def __init__(
        self,
        headers: Iterable[Any],
        source: str = "<dataframe>",
        *,
        factor: float = DEFAULT_IQR_FACTOR,
        method: str = "iqr",
        threshold: float = DEFAULT_ZSCORE_THRESHOLD,
        columns: Iterable[str] | None = None,
        null_tokens: Iterable[str] | None = None,
        first_row: int = 1,
    ) -> None:
        if method not in METHODS:
            raise ValueError(f"method must be one of: {', '.join(METHODS)} (got {method!r})")
        self.source = source
        self.method = method
        self.factor = _check_cutoff(factor, "factor")
        self.threshold = _check_cutoff(threshold, "threshold")
        self.first_row = first_row
        headers = ["" if pd.isna(h) else str(h).strip() for h in headers]
        self.names, _ = _column_names(headers)
        self.headers = headers
        self.requested = list(dict.fromkeys(columns)) if columns is not None else None
        if self.requested is not None:
            unknown = [name for name in self.requested if name not in self.names]
            if unknown:
                raise ValueError(
                    "Unknown column(s): "
                    + ", ".join(unknown)
                    + "; available: "
                    + ", ".join(self.names)
                )
        self.null_tokens = NULL_TOKENS | {
            t.strip().lower() for t in (null_tokens or ()) if t.strip()
        }
        #: Placeholder tokens that are themselves numbers (a ``-999`` sentinel). The fast
        #: path classifies only cells that fail to parse, so these need a separate check.
        tokens = sorted(self.null_tokens)
        parsed_tokens = _to_floats(pd.to_numeric(pd.Series(tokens, dtype=object), errors="coerce"))
        self._numeric_tokens = frozenset(
            t for t, v in zip(tokens, parsed_tokens, strict=True) if math.isfinite(v)
        )
        self._states = [_ColumnState() for _ in self.names]
        #: Positions of the columns worth accumulating: with an explicit selection the
        #: other columns can never be reported, so they are not stored at all.
        self._tracked = [
            i
            for i, name in enumerate(self.names)
            if self.requested is None or name in self.requested
        ]
        self.rows = 0

    def update(self, chunk: pd.DataFrame) -> None:
        width = len(self.names)
        if chunk.shape[1] != width:
            raise ValueError(f"chunk has {chunk.shape[1]} columns; expected {width}")
        if len(chunk) == 0:
            return
        positional = chunk.set_axis(range(width), axis=1)
        for i in self._tracked:
            self._update_column(self._states[i], positional[i])
        self.rows += len(chunk)

    def _update_column(self, state: _ColumnState, raw: pd.Series) -> None:
        cells = pd.Series(raw.to_numpy(dtype=object), dtype=object)
        absent = cells.isna().to_numpy(dtype=bool)
        text = cells.where(~absent, "").astype(str) if absent.any() else cells.astype(str)
        # Parse first, classify the rejects after: pandas parses an ASCII-padded number
        # exactly like its stripped text, and everything else it rejects - blanks,
        # placeholders, unicode padding, invalid text - is stripped and classified below,
        # so the outcome is identical to stripping every cell first, but the string work
        # only touches the (usually few) cells that did not parse.
        numbers = _to_floats(pd.to_numeric(text, errors="coerce"))
        finite = np.isfinite(numbers)
        if self._numeric_tokens:
            parsed = np.flatnonzero(finite)
            sentinel = (
                text.iloc[parsed].str.strip().str.lower().isin(self._numeric_tokens).to_numpy()
            )
            if sentinel.any():
                finite[parsed[sentinel]] = False
                state.null_like += int(sentinel.sum())
                # Already counted: keep them out of the reject classification below.
                absent_or_token = absent.copy()
                absent_or_token[parsed[sentinel]] = True
            else:
                absent_or_token = absent
        else:
            absent_or_token = absent
        state.missing += int(absent.sum())
        rest = np.flatnonzero(~finite & ~(absent_or_token & ~absent))
        if rest.size:
            stripped = text.iloc[rest].str.strip()
            rest_text = stripped.to_numpy(dtype=object)
            rest_absent = absent[rest]
            blank = (rest_text == "") & ~rest_absent
            null_like = (
                ~rest_absent & ~blank & stripped.str.lower().isin(self.null_tokens).to_numpy()
            )
            state.missing += int(blank.sum())
            state.null_like += int(null_like.sum())
            retry = np.flatnonzero(~(rest_absent | blank | null_like))
            if retry.size:
                good = np.zeros(retry.size, dtype=bool)
                # Only strings that stripping changed can parse differently; for the
                # rest the first parse already failed, so they are invalid as they are.
                changed = rest_text[retry] != text.to_numpy(dtype=object)[rest[retry]]
                if changed.any():
                    reparsed = _to_floats(
                        pd.to_numeric(
                            pd.Series(rest_text[retry[changed]], dtype=object), errors="coerce"
                        )
                    )
                    good[changed] = np.isfinite(reparsed)
                    where = rest[retry[good]]
                    if where.size:
                        if not numbers.flags.writeable:  # may be a view of the parsed Series
                            numbers = numbers.copy()
                        numbers[where] = reparsed[np.isfinite(reparsed)]
                        finite[where] = True
                state.invalid += int((~good).sum())
                if not good.all() and len(state.invalid_examples) < INVALID_EXAMPLE_LIMIT:
                    for value in rest_text[retry[~good]]:
                        if value not in state.invalid_examples:
                            state.invalid_examples.append(value)
                            if len(state.invalid_examples) == INVALID_EXAMPLE_LIMIT:
                                break
        state.values.append(numbers[finite])
        state.rows.append(finite)

    def result(self) -> AnomalyReport:
        warnings: list[str] = []
        if self.rows == 0:
            warnings.append("dataset has a header but no data rows")
        results: list[ColumnOutliers] = []
        for position, (name, header, state) in enumerate(
            zip(self.names, self.headers, self._states, strict=True), start=1
        ):
            count = state.values.size
            parsed = count + state.invalid
            share = count / parsed if parsed else 0.0
            if self.requested is None:
                if count == 0:
                    continue  # not a numeric column; nothing to scan
                if share < MOSTLY_NUMERIC:
                    shown = f"{share:.0%}"
                    if round(share * 100) >= MOSTLY_NUMERIC * 100:  # 89.6% must not say 90%
                        shown = f"{math.floor(share * 1000) / 10:.1f}%"
                    warnings.append(
                        f"column {name!r} skipped: only {shown} of its values are numeric"
                    )
                    continue
            elif name not in self.requested:
                continue
            results.append(self._finish(position, name, header, state))
        if not results and self.requested is None and self.rows:
            warnings.append("no numeric columns to analyze")
        return AnomalyReport(
            source=self.source,
            rows=self.rows,
            factor=self.factor if self.method == "iqr" else None,
            column_results=results,
            warnings=warnings,
            method=self.method,
            threshold=self.threshold if self.method == "modified_zscore" else None,
        )

    def _finish(self, position: int, name: str, header: str, state: _ColumnState) -> ColumnOutliers:
        warnings: list[str] = []
        if state.invalid:
            examples = ", ".join(repr(v) for v in state.invalid_examples)
            warnings.append(f"{state.invalid} non-numeric value(s) ignored (e.g. {examples})")
        if state.null_like:
            warnings.append(f"{state.null_like} placeholder value(s) such as NA/null/- ignored")
        count = state.values.size
        q1 = q3 = iqr = median = mad = lower = upper = spread = None
        low = high = 0
        examples_out: list[dict[str, Any]] = []
        if count == 0:
            warnings.append("no numeric values; outliers cannot be computed")
        elif count < MIN_VALUES:
            needed = (
                "meaningful quartiles"
                if self.method == "iqr"
                else "a meaningful median and deviation"
            )
            warnings.append(
                f"only {count} numeric value(s); at least {MIN_VALUES} are needed for {needed}"
            )
        else:
            # The only full-size temporaries: one copy of this column, reordered and
            # (for the modified z-score) rewritten with deviations in place.
            scratch = state.values.copy()
            with np.errstate(over="ignore", invalid="ignore"):
                if self.method == "iqr":
                    q1, q3 = (
                        float(q) for q in np.percentile(scratch, [25, 75], overwrite_input=True)
                    )
                    spread = iqr = q3 - q1
                    lower, upper = q1 - self.factor * iqr, q3 + self.factor * iqr
                    center = q1
                    zero_note = "interquartile range"
                else:
                    median = float(np.median(scratch, overwrite_input=True))
                    np.subtract(scratch, median, out=scratch)
                    np.abs(scratch, out=scratch)
                    spread = mad = float(np.median(scratch, overwrite_input=True))
                    half = self.threshold * mad / ZSCORE_SCALE
                    lower, upper = median - half, median + half
                    center = median
                    zero_note = "median absolute deviation"
            del scratch
            if spread == 0:
                warnings.append(
                    f"{zero_note} is 0; every value other than {center:g} is an outlier"
                )
            if not (math.isfinite(lower) and math.isfinite(upper)):
                warnings.append(
                    "the fence extends beyond the float range; nothing lies outside it "
                    "on that side (bound reported as null)"
                )
            low, high, examples_out = self._scan(state, lower, upper, spread)
        outliers = low + high
        return ColumnOutliers(
            position=position,
            name=name,
            header=header,
            values=count,
            missing=state.missing,
            null_like=state.null_like,
            invalid=state.invalid,
            q1=_number(q1, spread),
            q3=_number(q3, spread),
            iqr=_number(iqr, spread),
            median=_number(median, spread),
            mad=_number(mad, spread),
            lower_bound=_number(lower, spread),
            upper_bound=_number(upper, spread),
            outliers=outliers,
            outlier_percent=_percent(outliers, count),
            low_outliers=low,
            high_outliers=high,
            examples=examples_out,
            warnings=warnings,
        )

    def _scan(
        self, state: _ColumnState, lower: float, upper: float, spread: float
    ) -> tuple[int, int, list[dict[str, Any]]]:
        """Count values outside ``[lower, upper]`` and pick the most extreme examples.

        Works block by block and keeps at most :data:`EXAMPLE_LIMIT` candidates, so no
        full-size mask or distance array is built. Examples are ordered by distance
        outside the nearest bound, ties by row; values are stored in row order, so a
        value's ordinal breaks ties exactly like its row number.
        """
        low = high = 0
        candidates: list[tuple[float, int, float]] = []  # (-distance, ordinal, value)
        offset = 0
        for block in state.values.views():  # distances may overflow to inf: sorts first
            with np.errstate(over="ignore", invalid="ignore"):
                below, above = block < lower, block > upper
                low += int(below.sum())
                high += int(above.sum())
                picked = np.flatnonzero(below | above)
                if picked.size:
                    values = block[picked]
                    distance = np.where(below[picked], lower - values, values - upper)
                    best = np.lexsort((picked, -distance))[:EXAMPLE_LIMIT]
                    candidates.extend(
                        (-float(distance[i]), int(picked[i]) + offset, float(values[i]))
                        for i in best
                    )
                    candidates = sorted(candidates)[:EXAMPLE_LIMIT]
            offset += block.size
        rows = state.rows.rows_of([ordinal for _, ordinal, _ in candidates])
        examples = [
            {"row": row + self.first_row, "value": _number(value, spread)}
            for row, (_, _, value) in zip(rows, candidates, strict=True)
        ]
        return low, high, examples


def detect_outliers_frame(
    frame: pd.DataFrame,
    source: str = "<dataframe>",
    *,
    factor: float = DEFAULT_IQR_FACTOR,
    method: str = "iqr",
    threshold: float = DEFAULT_ZSCORE_THRESHOLD,
    columns: Iterable[str] | None = None,
    null_tokens: Iterable[str] | None = None,
) -> AnomalyReport:
    """Scan an in-memory DataFrame of raw values for outliers.

    ``method`` selects the fence: ``"iqr"`` (the default, using ``factor``) or
    ``"modified_zscore"`` (using ``threshold``); the other cutoff is ignored.

    Header text is taken from ``frame.columns`` (duplicate or blank headers are renamed
    uniquely as in the profiler); cells are treated as text, ``NaN``/``None`` as absent
    fields. Reported row numbers are 1-based positions in the frame.
    """
    detector = OutlierDetector(
        frame.columns,
        source,
        factor=factor,
        method=method,
        threshold=threshold,
        columns=columns,
        null_tokens=null_tokens,
    )
    detector.update(frame)
    return detector.result()


def detect_outliers_csv(
    path: Path | str,
    config: Config | None = None,
    *,
    factor: float = DEFAULT_IQR_FACTOR,
    method: str = "iqr",
    threshold: float = DEFAULT_ZSCORE_THRESHOLD,
    columns: Iterable[str] | None = None,
    chunk_size: int | None = None,
    null_tokens: Iterable[str] | None = None,
) -> AnomalyReport:
    """Stream ``path`` with the configured delimiter and encoding and scan for outliers.

    ``chunk_size`` defaults to ``config.csv_chunk_size``, else the profiler's default.
    Raises :class:`IngestionError` for a missing, empty, undecodable or malformed file,
    exactly like :func:`~automation_toolkit.processing.profiling.profile_csv`; a
    header-only file is a valid empty dataset. Reported row numbers are record numbers
    in the file, header included, matching ``source_row`` elsewhere in the toolkit.
    Raises :class:`ValueError` for an unknown column name, an unknown ``method`` or a
    negative ``factor`` or ``threshold``. ``method`` selects the fence: ``"iqr"`` (the
    default, using ``factor``) or ``"modified_zscore"`` (using ``threshold``).
    """
    path = Path(path)
    config = config or Config()
    size = check_chunk_size(chunk_size) if chunk_size is not None else config.csv_chunk_size
    if not path.is_file():
        raise IngestionError(f"Cannot scan {path}: file not found.")
    try:
        with path.open(newline="", encoding=config.csv_encoding) as handle:
            reader = csv.reader(handle, delimiter=config.csv_delimiter)
            header = next(reader, None)
            if header is None or not any(cell.strip() for cell in header):
                if header is None or not any(any(c.strip() for c in r) for r in reader):
                    raise IngestionError(
                        f"Cannot scan {path.name}: the file is empty (no header row)."
                    )
                raise IngestionError(
                    f"Cannot scan {path.name}: line 1 is blank; expected a header row."
                )
            detector = OutlierDetector(
                header,
                path.name,
                factor=factor,
                method=method,
                threshold=threshold,
                columns=columns,
                null_tokens=null_tokens,
                first_row=2,
            )
            effective = size or _default_chunk_size(len(header))
            # Like profile_csv, a default-size chunk is also capped at about 32 million
            # characters, so files with very long fields do not hold huge chunks; an
            # explicit chunk size is a promise about records per chunk and is kept.
            max_chars = None if size else DEFAULT_CHUNK_CHARS
            for chunk in _record_chunks(reader, len(header), effective, path.name, max_chars):
                detector.update(chunk)
    except IngestionError:
        raise
    except (OSError, UnicodeError, LookupError, csv.Error) as exc:
        raise IngestionError(f"Cannot read CSV {path.name}: {exc}") from exc
    return detector.result()
