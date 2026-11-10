"""Regression tests for numerical edge cases and for the accuracy of top values and the
approximate median. Each "used to" note records the wrong result seen before the fix."""

import json
import statistics
from pathlib import Path

import numpy as np
import pytest

from automation_toolkit.config import Config
from automation_toolkit.processing import profiling
from automation_toolkit.processing.profiling import _Numbers, profile_csv

CHUNKS = [1, 3, 50, 100_000]


def stats_of(tmp_path: Path, values, chunk_size: int = 100_000, **kwargs) -> dict:
    return column_of(tmp_path, values, chunk_size, **kwargs).stats


def column_of(tmp_path: Path, values, chunk_size: int = 100_000, **kwargs):
    path = tmp_path / "x.csv"
    path.write_text("x\n" + "\n".join(map(str, values)) + "\n", encoding="utf-8")
    return profile_csv(path, chunk_size=chunk_size, **kwargs).column_profiles[0]


def comparable(profile) -> dict:
    data = profile.to_dict()
    del data["generated_at"], data["processing"]
    return data


# --- large integers ----------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_std_of_large_integers_uses_exact_differences(tmp_path, chunk_size) -> None:
    """Used to be 0.0: 10**20 + 1 and 10**20 + 2 are the same float."""
    stats = stats_of(tmp_path, [10**20 + 1, 10**20 + 2, 10**20 + 3], chunk_size)
    assert stats["std"] == 1.0
    assert (stats["min"], stats["max"]) == (10**20 + 1, 10**20 + 3)


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_median_of_integers_beyond_float_precision(tmp_path, chunk_size) -> None:
    """Used to be 9007199254740996.0 (float rounding); the true median is ...995."""
    stats = stats_of(tmp_path, [2**53 + 1, 2**53 + 3, 2**53 + 5, 2**53 + 7], chunk_size)
    assert stats["median"] == 2**53 + 4
    assert isinstance(stats["median"], int)


def test_median_of_ordinary_integers_stays_a_float(tmp_path) -> None:
    stats = stats_of(tmp_path, [1, 2, 10])
    assert stats["median"] == 2.0 and isinstance(stats["median"], float)


def test_half_median_of_large_integers(tmp_path) -> None:
    assert stats_of(tmp_path, [2**60, 2**60 + 1])["median"] == pytest.approx(2**60 + 0.5)


def test_integers_beyond_int64_mixed_across_chunks(tmp_path) -> None:
    values = [5, -(10**25), 3, 10**25 + 1]
    for chunk_size in CHUNKS:
        stats = stats_of(tmp_path, values, chunk_size)
        assert (stats["min"], stats["max"]) == (-(10**25), 10**25 + 1)
        assert stats["median"] == 4.0
        # float(sum)/n cancels the 1e25 terms (fmean gives 2.0); the exact mean is 2.25
        assert stats["mean"] == 2.25


def test_integer_too_long_for_a_float_is_still_an_integer(tmp_path) -> None:
    """Used to be classified as text because the value overflows a float."""
    col = column_of(tmp_path, ["9" * 400, "1", "2"])
    assert col.inferred_type == "integer"
    assert col.stats["max"] == int("9" * 400) and col.stats["median"] == 2.0
    assert col.stats["mean"] is None
    assert any("too large" in w for w in col.warnings)
    json.dumps(col.to_dict())  # big ints stay JSON serializable


def test_non_ascii_digits_are_not_integers(tmp_path) -> None:
    col = column_of(tmp_path, ["١٢٣", "٤٥٦"])
    assert col.inferred_type == "string"


def test_integer_column_turning_float_in_a_later_chunk(tmp_path) -> None:
    values = [10**20, 1, 2, 2.5]
    for chunk_size in CHUNKS:
        stats = stats_of(tmp_path, values, chunk_size)
        assert stats["median"] == pytest.approx(2.25)
        assert stats["max"] == pytest.approx(1e20)


# --- extreme floats ----------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_tiny_values_are_not_rounded_to_zero(tmp_path, chunk_size) -> None:
    """Used to report min/max/mean/std of 1e-9..3e-9 as 0.0."""
    stats = stats_of(tmp_path, ["1e-9", "2e-9", "3e-9"], chunk_size)
    assert stats["min"] == 1e-9 and stats["max"] == 3e-9
    assert stats["mean"] == pytest.approx(2e-9) and stats["std"] == pytest.approx(1e-9)


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_tiny_spread_does_not_underflow(tmp_path, chunk_size) -> None:
    """Squares of 1e-200 underflow to 0; the std used to come out as 0."""
    stats = stats_of(tmp_path, ["1e-200", "2e-200", "4e-200"], chunk_size)
    assert stats["std"] == pytest.approx(statistics.stdev([1e-200, 2e-200, 4e-200]), rel=1e-5)


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_opposite_values_at_the_float_limit(tmp_path, chunk_size) -> None:
    """Used to be mean null: 1e308 - (-1e308) overflowed though the mean is 0."""
    stats = stats_of(tmp_path, ["1e308", "-1e308"], chunk_size)
    assert stats["mean"] == 0.0
    assert stats["std"] == pytest.approx(2**0.5 * 1e308)


def test_ordinary_values_keep_six_decimal_rounding(tmp_path) -> None:
    stats = stats_of(tmp_path, ["1", "2.5", "3"])
    assert stats["mean"] == 2.166667
    assert stats["std"] == round(statistics.stdev([1, 2.5, 3]), 6)


def test_negative_zero_and_subnormals(tmp_path) -> None:
    stats = stats_of(tmp_path, ["-0.0", "5e-324", "0"])
    assert stats["min"] == 0.0 and stats["max"] == 5e-324
    assert stats["median"] == 0.0


def test_overflowing_float_literal_is_not_a_number(tmp_path) -> None:
    col = column_of(tmp_path, ["1e400"] + ["1.5"] * 19)
    assert col.inferred_type == "string"
    assert any("mostly numeric" in w and "'1e400'" in w for w in col.warnings)


# --- mixed dates -------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_naive_aware_and_datetime_values_compare_in_utc(tmp_path, chunk_size) -> None:
    values = ["2026-01-02 12:00", "2026-01-01T23:30:00-05:00", "2026-01-01"]
    col = column_of(tmp_path, values, chunk_size)
    assert col.inferred_type == "date"
    assert col.stats == {"min": "2026-01-01T00:00:00", "max": "2026-01-02T12:00:00"}


def test_dates_outside_the_nanosecond_range(tmp_path) -> None:
    col = column_of(tmp_path, ["2300-01-01", "1500-06-01", "2026-05-05"], chunk_size=1)
    assert col.stats == {"min": "1500-06-01T00:00:00", "max": "2300-01-01T00:00:00"}


def test_configured_format_and_iso_dates_combine(tmp_path) -> None:
    col = column_of(
        tmp_path, ["05/01/2026", "2026-01-03"], chunk_size=1, config=Config(date_format="%d/%m/%Y")
    )
    assert col.inferred_type == "date"
    assert col.stats == {"min": "2026-01-03T00:00:00", "max": "2026-01-05T00:00:00"}


def test_sub_second_precision_is_kept(tmp_path) -> None:
    col = column_of(tmp_path, ["2026-01-01T00:00:00.123456789", "2026-01-01"])
    assert col.stats["max"] == "2026-01-01T00:00:00.123456789"


# --- top values --------------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 999, 5_000, 100_000])
def test_heavy_hitter_first_seen_after_many_distinct_values(tmp_path, chunk_size) -> None:
    """Used to be missing entirely: after 10,000 distinct values, new ones were dropped."""
    values = [f"id{i}" for i in range(15_000)] + ["HOT"] * 500 + ["id3"] * 2
    col = column_of(tmp_path, values, chunk_size)
    assert col.top_values == [{"value": "HOT", "count": 500}, {"value": "id3", "count": 3}]
    assert col.top_values_exact is True
    assert col.unique == 15_001


def test_top_values_match_exact_counts_with_ties(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "TRACKED_VALUE_LIMIT", 5)
    rng = np.random.default_rng(7)
    values = [f"v{int(v)}" for v in rng.zipf(1.3, 30_000) % 5_000]
    expected = {}
    for value in values:
        expected[value] = expected.get(value, 0) + 1
    order = {v: i for i, v in reversed(list(enumerate(values)))}
    ranked = sorted(expected.items(), key=lambda kv: (-kv[1], order[kv[0]]))[:5]
    for chunk_size in [1, 7, 1_000, 100_000]:
        col = column_of(tmp_path, values, chunk_size)
        assert [(t["value"], t["count"]) for t in col.top_values] == ranked
        assert col.unique == len(expected)


def test_equal_counts_list_in_file_order_across_chunks(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "TRACKED_VALUE_LIMIT", 5)
    values = [f"u{i}" for i in range(300)] + [f"t{i}" for i in range(10)] * 2
    for chunk_size in [1, 11, 100_000]:
        col = column_of(tmp_path, values, chunk_size)
        assert [t["value"] for t in col.top_values] == ["t0", "t1", "t2", "t3", "t4"]


def test_retained_text_is_bounded(tmp_path, monkeypatch) -> None:
    values = [f"{'x' * 200}{i}" for i in range(20_000)]
    path = tmp_path / "x.csv"
    path.write_text("x\n" + "\n".join(values) + "\n", encoding="utf-8")
    counter_cls = profiling._ValueCounter
    captured = []
    original = counter_cls._compact

    def spy(self):
        original(self)
        captured.append(len(self._text))

    monkeypatch.setattr(counter_cls, "_compact", spy)
    profile_csv(path, chunk_size=1_000)
    assert captured and max(captured) <= profiling.TRACKED_VALUE_LIMIT


# --- approximate median accuracy ---------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        np.arange(30_000.0),
        np.random.default_rng(3).lognormal(0, 2, 30_000),
        np.r_[np.zeros(15_001), np.full(14_999, 1e6)],
    ],
    ids=["sorted", "lognormal", "bimodal"],
)
def test_approximate_median_rank_error_is_small(monkeypatch, data) -> None:
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 3_000)
    numbers = _Numbers()
    for start in range(0, data.size, 2_500):
        numbers.add(data[start : start + 2_500])
    median = numbers.stats()["median"]
    assert numbers.stats()["median_approximate"] is True
    # 3 standard errors of a 3,000-value sample around the 50% rank
    assert abs((data < median).mean() - 0.5) <= 3 * (0.25 / 3_000) ** 0.5 or median == np.median(
        data
    )


def test_approximate_median_is_chunk_independent_for_integers(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 100)
    values = [10**20 + i for i in range(1_000)]
    medians = {stats_of(tmp_path, values, size)["median"] for size in [1, 33, 100_000]}
    assert len(medians) == 1
    (median,) = medians
    assert isinstance(median, int) and 10**20 + 350 < median < 10**20 + 650


# --- end to end across chunk sizes -------------------------------------------------


def test_extreme_column_mix_is_chunk_independent(tmp_path) -> None:
    rows = ["big,tiny,huge,when"]
    for i in range(400):
        rows.append(
            f"{10**22 + i * 7},{(i + 1) * 1e-250!r},{(-1) ** i * 1.5e307!r},"
            f"{['2026-01-01', '2026-01-01T05:00:00+03:00', '2025-12-31 23:00'][i % 3]}"
        )
    path = tmp_path / "x.csv"
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    reference = comparable(profile_csv(path, chunk_size=100_000))
    for chunk_size in [1, 7, 64]:
        assert comparable(profile_csv(path, chunk_size=chunk_size)) == reference
    big, tiny, huge, when = profile_csv(path).column_profiles
    assert big.stats["std"] == pytest.approx(statistics.stdev(range(0, 2800, 7)), rel=1e-9)
    assert tiny.stats["min"] == 1e-250 and tiny.stats["std"] > 0
    assert huge.stats["mean"] == pytest.approx(0.0, abs=1e295)
    assert when.inferred_type == "date"


def test_dates_with_different_resolutions_do_not_rely_on_deprecated_fillna(tmp_path) -> None:
    """pandas warns (and will raise) when merging datetime arrays of different units."""
    import warnings

    values = ["1500-06-01", "2026-01-01T00:00:00.123456789", "2026-01-01T10:00:00+02:00"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        col = column_of(tmp_path, values, chunk_size=1)
        col_whole = column_of(tmp_path, values)
    assert col.stats == col_whole.stats
    assert col.stats == {"min": "1500-06-01T00:00:00", "max": "2026-01-01T08:00:00"}


# --- found while reviewing ---------------------------------------------------------


@pytest.mark.parametrize("values", [[10**400, 1.5], [1.5, 10**400], [10**400, 2.5, -(10**400), 1]])
def test_integers_beyond_floats_next_to_floats(tmp_path, values) -> None:
    """Crashed with OverflowError when such an integer and a float met in the sample."""
    results = [stats_of(tmp_path, values, size) for size in (1, 2, 100)]
    assert results[0] == results[1] == results[2]
    stats = results[0]
    assert stats["max"] == max(values) and stats["min"] == min(values)
    assert stats["mean"] is None


def test_exact_integer_mean_survives_overflowing_moments(tmp_path) -> None:
    """The mean of [10**400, -10**400] is 0; the float moments overflow but the sum does not."""
    col = column_of(tmp_path, [10**400, -(10**400)], chunk_size=1)
    assert col.stats["mean"] == 0.0 and col.stats["median"] == 0.0
    assert col.stats["std"] is None
    assert any("too large" in w for w in col.warnings)


def test_float_column_extremes_are_floats_at_every_chunk_size(tmp_path) -> None:
    """A chunk holding only the integers of a float column reported min 2 instead of 2.0."""
    values = ["7", "7.5", "2", "9"] * 3
    for chunk_size in CHUNKS:
        stats = stats_of(tmp_path, values, chunk_size)
        assert (stats["min"], stats["max"]) == (2.0, 9.0)
        assert isinstance(stats["min"], float) and isinstance(stats["max"], float)


def test_median_sample_budget_is_shared_by_wide_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_BUDGET", 400)
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_MIN", 20)
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 1_000)
    header = ",".join(f"c{j}" for j in range(10))
    body = "\n".join(",".join(str(i * j) for j in range(10)) for i in range(100))
    path = tmp_path / "w.csv"
    path.write_text(f"{header}\n{body}\n", encoding="utf-8")
    columns = profile_csv(path, chunk_size=7).column_profiles
    assert all(c.stats["median_approximate"] for c in columns[1:])  # 40 per column < 100
    narrow = tmp_path / "n.csv"
    narrow.write_text("x\n" + "\n".join(map(str, range(100))) + "\n", encoding="utf-8")
    assert profile_csv(narrow).column_profiles[0].stats["median_approximate"] is False


def test_value_counter_widens_counts_and_ranks(monkeypatch) -> None:
    monkeypatch.setattr(profiling, "_U32_MAX", 5)
    counter = profiling._ValueCounter(pending_cap=4)
    for i in range(12):
        value = np.array(["hot" if i % 2 else f"v{i}"], dtype=object)
        hashes = profiling.pd.util.hash_array(value)
        counter.add(hashes, np.array([3]), np.array([i]), value)
    assert counter.top(2)[0] == ("hot", 18)
    assert counter._counts.dtype == np.int64 and counter._rank.dtype == np.int64
    assert len(counter) == 7


def test_text_held_between_compactions_is_shared_by_columns(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "_PENDING_BUDGET", 4_096)
    monkeypatch.setattr(profiling, "_PENDING_MIN", 64)
    width = 32
    header = ",".join(f"c{j}" for j in range(width))
    body = "\n".join(",".join(f"{j}-{i}" for j in range(width)) for i in range(2_000))
    path = tmp_path / "w.csv"
    path.write_text(f"{header}\n{body}\n", encoding="utf-8")
    peaks = []
    original = profiling._ValueCounter._compact

    def spy(self):
        peaks.append(self._pending_size)
        original(self)

    monkeypatch.setattr(profiling._ValueCounter, "_compact", spy)
    profile = profile_csv(path, chunk_size=500)
    assert all(c.unique == 2_000 for c in profile.column_profiles)
    assert max(peaks) <= 2 * 4_096 // width + 500  # cap per column, plus one chunk
