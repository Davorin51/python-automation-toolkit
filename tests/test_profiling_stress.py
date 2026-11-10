"""Regression tests from stress testing: statistics on unusual values, high cardinality,
memory retention and the chunking limits. Every test pins a bug that was found (or a
property that was only assumed) while stress testing the profiler."""

import gc
import random
import statistics
import tracemalloc
from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.processing import profiling
from automation_toolkit.processing.profiling import profile_csv, profile_frame


def write(tmp_path: Path, text: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def column(profile, name):
    return next(c for c in profile.column_profiles if c.name == name)


def lines(header: str, values) -> str:
    return header + "\n" + "\n".join(map(str, values)) + "\n"


# --- independent reference comparison ----------------------------------------------


def _messy_rows(seed: int, n: int) -> list[list[str]]:
    rng = random.Random(seed)
    rows = []
    for _ in range(n):
        roll = rng.random()
        if roll < 0.03:
            rows.append([])  # blank line
            continue
        row = [
            str(rng.randint(-50, 50)),
            rng.choice(["a", " a", "b ", "", "  ", "NA", "c", "Ä", "😀"]),
            f"{rng.uniform(-1e6, 1e6):.3f}",
        ]
        if roll < 0.08:
            row = row[: rng.randint(1, 2)]  # short row
        rows.append(row)
    rows += rows[:10]  # guaranteed duplicates
    return rows


@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("chunk_size", [1, 13, 500, 100_000])
def test_matches_an_independent_reference(tmp_path: Path, seed: int, chunk_size: int) -> None:
    rows = _messy_rows(seed, 1_500)
    text = "i,s,f\n" + "".join(",".join(r) + "\n" for r in rows)
    profile = profile_csv(write(tmp_path, text), chunk_size=chunk_size)

    padded = [r + [None] * (3 - len(r)) if r else ["", "", ""] for r in rows]
    stripped = [[(c or "").strip() for c in r] for r in padded]
    blank = [all(c == "" for c in r) for r in stripped]
    assert profile.rows == len(rows)
    assert profile.blank_rows == sum(blank)
    assert profile.short_rows == sum(
        1 for r, b in zip(rows, blank, strict=True) if r and len(r) < 3 and not b
    )
    content = [tuple(r) for r, b in zip(stripped, blank, strict=True) if not b]
    assert profile.duplicate_rows == len(content) - len(set(content))
    for index, name in enumerate("isf"):
        values = [r[index] for r in stripped]
        present = [v for v in values if v]
        col = column(profile, name)
        assert col.missing == len(values) - len(present)
        assert col.count == len(present)
        assert col.unique == len(set(present))
    numbers = [float(r[2]) for r in stripped if r[2]]
    stats = column(profile, "f").stats
    assert stats["min"] == pytest.approx(min(numbers))
    assert stats["max"] == pytest.approx(max(numbers))
    assert stats["mean"] == pytest.approx(statistics.fmean(numbers), abs=1e-6)
    assert stats["std"] == pytest.approx(statistics.stdev(numbers), abs=1e-6)
    assert stats["median"] == pytest.approx(statistics.median(numbers))
    ints = [int(r[0]) for r in stripped if r[0]]
    assert column(profile, "i").stats["min"] == min(ints)
    s_counts = pd.Series([v for v in (r[1] for r in stripped) if v]).value_counts()
    assert column(profile, "s").top_values[0]["count"] == int(s_counts.iloc[0])


# --- numeric edge cases ------------------------------------------------------------


def test_integer_extremes_are_exact_beyond_float_precision(tmp_path: Path) -> None:
    """2**53 + 1 used to come back as 2**53 because min/max went through float."""
    stats = column(profile_csv(write(tmp_path, lines("x", [2**53 + 1, 2**53 + 3]))), "x").stats
    assert (stats["min"], stats["max"]) == (2**53 + 1, 2**53 + 3)


def test_integer_extremes_are_exact_beyond_int64(tmp_path: Path) -> None:
    big = 123456789012345678901234567890
    path = write(tmp_path, lines("x", [5, big, -big]))
    stats = column(profile_csv(path, chunk_size=1), "x").stats
    assert (stats["min"], stats["max"]) == (-big, big)


@pytest.mark.parametrize("chunk_size", [1, 7, 10_000])
def test_std_is_accurate_and_chunk_independent_for_large_offsets(tmp_path, chunk_size) -> None:
    """Values near 1e9 with a tiny spread lost precision when merged one row at a time."""
    values = [1e9 + i * 0.001 for i in range(10_000)]
    path = write(tmp_path, lines("x", [repr(v) for v in values]))
    stats = column(profile_csv(path, chunk_size=chunk_size), "x").stats
    # Reported values are rounded to 6 decimals; before the fix chunk_size=1 gave 2.887031.
    assert stats["std"] == round(statistics.stdev(values), 6)
    assert stats["mean"] == round(statistics.fmean(values), 6)


def test_values_near_the_float_limit_have_exact_mean(tmp_path: Path) -> None:
    """The mean of [1e308, 1e308, -1e308] is representable; it used to be reported null."""
    col = column(profile_csv(write(tmp_path, lines("x", ["1e308", "1e308", "-1e308"]))), "x")
    assert col.stats["mean"] == pytest.approx(1e308 / 3)
    assert col.stats["std"] == pytest.approx(1.1547005383792517e308)
    assert not any("too large" in w for w in col.warnings)
    assert col.stats["min"] == -1e308 and col.stats["max"] == 1e308


def test_float_overflow_is_reported_not_silent(tmp_path: Path) -> None:
    col = column(profile_csv(write(tmp_path, lines("x", ["1.7e308", "-1.7e308"]))), "x")
    assert col.stats["mean"] == 0.0
    assert col.stats["std"] is None  # 2.4e308 exceeds the float range
    assert col.stats["min"] == -1.7e308 and col.stats["max"] == 1.7e308
    assert any("too large" in w for w in col.warnings)


def test_no_overflow_warning_for_ordinary_numbers(tmp_path: Path) -> None:
    col = column(profile_csv(write(tmp_path, lines("x", [1, 2, 3]))), "x")
    assert not any("too large" in w for w in col.warnings)


# --- date edge cases ---------------------------------------------------------------


def test_mixed_timezone_offsets_do_not_crash(tmp_path: Path) -> None:
    """Different UTC offsets made pandas raise even with errors='coerce'."""
    values = ["2026-01-01T10:00:00+02:00", "2026-01-01T09:00:00Z", "2026-01-02"]
    col = column(profile_csv(write(tmp_path, lines("d", values))), "d")
    assert col.inferred_type == "date"
    assert col.stats == {"min": "2026-01-01T08:00:00", "max": "2026-01-02T00:00:00"}


@pytest.mark.parametrize("chunk_size", [1, 1000])
def test_aware_and_naive_dates_across_chunks(tmp_path: Path, chunk_size: int) -> None:
    values = ["2026-03-01", "2026-03-02T00:00:00+05:00", "2026-01-15"]
    col = column(profile_csv(write(tmp_path, lines("d", values)), chunk_size=chunk_size), "d")
    assert col.stats["min"] == "2026-01-15T00:00:00"
    assert col.inferred_type == "date"


def test_extreme_and_invalid_calendar_dates(tmp_path: Path) -> None:
    values = ["0001-01-01", "9999-12-31", "2026-02-29", "2024-02-29"]
    col = column(profile_csv(write(tmp_path, lines("d", values))), "d")
    assert col.type_counts["date"] == 3 and col.type_counts["string"] == 1  # 2026 not leap
    assert col.stats["min"].startswith("0001-01-01") and col.stats["max"].startswith("9999")


# --- high cardinality and distinct-value handling ----------------------------------


def test_high_cardinality_column_counts_stay_exact(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "TRACKED_VALUE_LIMIT", 100)
    values = [f"id-{i}" for i in range(20_000)] + ["id-7"] * 3
    col = column(profile_csv(write(tmp_path, lines("v", values)), chunk_size=999), "v")
    assert col.unique == 20_000 and col.count == 20_003
    assert col.top_values == [{"value": "id-7", "count": 4}]
    assert col.top_values_exact is True


@pytest.mark.parametrize("chunk_size", [1, 2, 1000])
def test_values_differing_only_in_padding_merge(tmp_path: Path, chunk_size: int) -> None:
    col = column(
        profile_csv(write(tmp_path, lines("v", ["a", " a", "a ", "b"])), chunk_size=chunk_size), "v"
    )
    assert col.unique == 2
    assert col.top_values[0] == {"value": "a", "count": 3}
    assert col.whitespace_padded == 2


@pytest.mark.parametrize("chunk_size", [1, 2, 1000])
def test_type_examples_are_unique_across_chunks(tmp_path: Path, chunk_size: int) -> None:
    """A rewrite once appended a value again when it reappeared in a later chunk."""
    path = write(tmp_path, lines("v", ["yes", "no", "yes", "no", "y"]))
    col = column(profile_csv(path, chunk_size=chunk_size), "v")
    assert col.type_examples["boolean"] == ["yes", "no", "y"]


def test_frame_cells_with_equal_text_are_one_value() -> None:
    frame = pd.DataFrame({"v": pd.Series([1, "1", " 1", None, 2], dtype=object)})
    col = profile_frame(frame).column_profiles[0]
    assert col.unique == 2 and col.missing_breakdown["absent"] == 1
    assert col.top_values[0] == {"value": "1", "count": 3}


def test_null_tokens_are_matched_after_stripping_and_case_folding(tmp_path: Path) -> None:
    col = column(
        profile_csv(write(tmp_path, lines("v", [" NULL ", "null", "N/A", "x"])), chunk_size=1), "v"
    )
    assert col.null_like == 3
    assert col.whitespace_padded == 1


# --- chunking limits and memory ----------------------------------------------------


def test_default_chunks_are_split_by_text_size(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "DEFAULT_CHUNK_CHARS", 10_000)
    path = write(tmp_path, lines("a,b", [f"{i},{'x' * 1_000}" for i in range(50)]))
    profile = profile_csv(path)
    assert profile.chunks == 5
    assert profile.rows == 50
    assert column(profile, "b").stats["max_length"] == 1_000


def test_explicit_chunk_size_is_not_split_by_text_size(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "DEFAULT_CHUNK_CHARS", 10_000)
    path = write(tmp_path, lines("a,b", [f"{i},{'x' * 1_000}" for i in range(50)]))
    assert profile_csv(path, chunk_size=50).chunks == 1


def test_text_size_split_does_not_change_results(tmp_path: Path, monkeypatch) -> None:
    path = write(tmp_path, lines("a,b", [f"{i % 9},{'x' * (i % 300)}" for i in range(600)]))
    whole = profile_csv(path).to_dict()
    monkeypatch.setattr(profiling, "DEFAULT_CHUNK_CHARS", 2_000)
    split = profile_csv(path).to_dict()
    assert split["processing"]["chunks"] > 1
    for data in (whole, split):
        del data["generated_at"], data["processing"]
    assert split == whole


def test_repeated_profiling_does_not_retain_memory(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        lines("a,b,c", [f"{i},v{i % 500},2026-01-{i % 28 + 1:02d}" for i in range(20_000)]),
    )
    profile_csv(path, chunk_size=3_000)  # warm up imports and pandas caches
    gc.collect()
    tracemalloc.start()
    baseline, _ = tracemalloc.get_traced_memory()
    for _ in range(5):
        profile_csv(path, chunk_size=3_000)
        gc.collect()
    retained, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert retained - baseline < 2 * 2**20, f"retained {(retained - baseline) / 2**20:.2f} MB"


def test_wide_file_profiles_every_column(tmp_path: Path) -> None:
    width = 300
    header = ",".join(f"c{j}" for j in range(width))
    body = [",".join(str((i * j) % 7) for j in range(width)) for i in range(200)]
    profile = profile_csv(write(tmp_path, lines(header, body)))
    assert profile.columns == width and profile.chunk_size == profiling.DEFAULT_CHUNK_CELLS // width
    assert column(profile, "c0").unique == 1 and column(profile, "c1").unique == 7
