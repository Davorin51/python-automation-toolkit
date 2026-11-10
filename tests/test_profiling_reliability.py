"""Regression tests for chunk boundaries, messy headers, missing values and malformed CSVs.

Each test here pins behavior that was either wrong or unverified before; the recurring
theme is that the profile must not depend on how the file is split into chunks.
"""

import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.processing import profiling
from automation_toolkit.processing.profiling import (
    DatasetProfiler,
    _HashSet,
    _Numbers,
    profile_csv,
    profile_frame,
)

ROOT = Path(__file__).resolve().parents[1]
CHUNK_SIZES = [1, 2, 3, 7, 64, 1000, 100_000]


def write(tmp_path: Path, text: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def column(profile, name):
    return next(c for c in profile.column_profiles if c.name == name)


def comparable(profile) -> str:
    data = profile.to_dict()
    del data["generated_at"], data["processing"]
    return json.dumps(data, sort_keys=True)


# --- chunk boundaries --------------------------------------------------------------


@pytest.fixture
def mixed_bag(tmp_path: Path) -> Path:
    """A file whose every interesting feature is spread across row 1..N."""
    rows = ["id,amount,day,note,id"]
    for i in range(200):
        amount = ["7", "7.5", "oops", "NA"][i % 4]
        note = ["", "  ", f"n{i % 11}", " padded ", "-"][i % 5]
        rows.append(f"{i},{amount},2026-{1 + i % 12:02d}-{1 + i % 28:02d},{note},{i}")
    rows += ["", "0,7,2026-01-01,,0", "1,2", "   ", "9,9"]
    return write(tmp_path, "\n".join(rows) + "\n")


@pytest.mark.parametrize("chunk_size", CHUNK_SIZES)
def test_every_statistic_is_chunk_size_independent(mixed_bag: Path, chunk_size: int) -> None:
    reference = comparable(profile_csv(mixed_bag, chunk_size=1_000_000))
    assert comparable(profile_csv(mixed_bag, chunk_size=chunk_size)) == reference


@pytest.mark.parametrize("chunk_size", [1, 2, 5, 1000])
def test_duplicate_rows_detected_across_chunk_boundaries(tmp_path: Path, chunk_size: int) -> None:
    # First and last data rows are identical, so a per-chunk check would miss them.
    rows = ["a,b", "x,1", *[f"y,{i}" for i in range(20)], "x,1"]
    profile = profile_csv(write(tmp_path, "\n".join(rows) + "\n"), chunk_size=chunk_size)
    assert profile.duplicate_rows == 1
    assert profile.rows == 22


def test_row_hash_respects_column_order(tmp_path: Path) -> None:
    """'1,2' and '2,1' share values but are different rows; they must not collide."""
    profile = profile_csv(write(tmp_path, "a,b\n1,2\n2,1\n1,2\n"), chunk_size=1)
    assert profile.duplicate_rows == 1


@pytest.mark.parametrize("chunk_size", [1, 4, 1000])
def test_type_examples_and_top_values_follow_file_order(tmp_path: Path, chunk_size: int) -> None:
    rows = ["v", "10", "10", "abc", "2026-01-01", "xyz", "10", "abc"]
    col = column(profile_csv(write(tmp_path, "\n".join(rows) + "\n"), chunk_size=chunk_size), "v")
    assert col.type_examples["string"] == ["abc", "xyz"]
    assert col.type_examples["integer"] == ["10"]
    assert col.top_values[0] == {"value": "10", "count": 3}


@pytest.mark.parametrize("chunk_size", [1, 3, 10_000])
def test_unique_counts_exact_with_many_tiny_chunks(tmp_path: Path, chunk_size: int) -> None:
    values = [f"v{i % 137}" for i in range(2_000)]
    path = write(tmp_path, "v\n" + "\n".join(values) + "\n")
    col = column(profile_csv(path, chunk_size=chunk_size), "v")
    assert col.unique == 137


def test_hashset_deduplicates_across_many_parts() -> None:
    hashes = _HashSet()
    for i in range(500):
        hashes.add(np.array([i % 20, i % 20], dtype=np.uint64))
    assert len(hashes) == 20
    assert len(hashes._parts) == 1  # compacted rather than hoarding 500 arrays


# --- median sketch -----------------------------------------------------------------


@pytest.mark.parametrize("chunk_size", [1, 17, 250, 10_000])
def test_median_sketch_is_chunk_size_independent(tmp_path, monkeypatch, chunk_size: int) -> None:
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 101)
    path = write(tmp_path, "x\n" + "\n".join(str(i) for i in range(3_000)) + "\n")
    reference = column(profile_csv(path, chunk_size=10_000), "x").stats
    stats = column(profile_csv(path, chunk_size=chunk_size), "x").stats
    assert stats == reference
    assert stats["median_approximate"] is True
    assert stats["min"] == 0 and stats["max"] == 2_999  # exact regardless of sampling


def test_median_sketch_is_not_biased_towards_later_rows(monkeypatch) -> None:
    """Sorted input: a reservoir that over-weights late rows pulls the median up."""
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 2_000)
    numbers = _Numbers()
    for start in range(0, 100_000, 999):
        numbers.add(np.arange(start, min(start + 999, 100_000), dtype=float))
    stats = numbers.stats()
    assert stats["median"] == pytest.approx(49_999.5, rel=0.05)
    assert stats["mean"] == pytest.approx(49_999.5)
    assert numbers.sample.size == 2_000


def test_exact_median_and_std_below_the_limit(tmp_path: Path) -> None:
    path = write(tmp_path, "x\n" + "\n".join(str(i) for i in range(1, 1_001)) + "\n")
    stats = column(profile_csv(path, chunk_size=7), "x").stats
    assert stats["median"] == 500.5 and stats["median_approximate"] is False
    assert stats["mean"] == pytest.approx(500.5)
    assert stats["std"] == pytest.approx(pd.Series(range(1, 1_001)).std())


# --- headers -----------------------------------------------------------------------


def test_duplicate_headers_keep_independent_statistics(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "v,v\n1,a\n2,b\n3,c\n"), chunk_size=2)
    first, second = profile.column_profiles
    assert (first.name, second.name) == ("v", "v_2")
    assert (first.inferred_type, second.inferred_type) == ("integer", "string")
    assert first.stats["max"] == 3 and second.stats["max_length"] == 1


def test_many_duplicate_headers_all_get_distinct_names(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, ",".join(["c"] * 6) + "\n" + ",".join("123456") + "\n"))
    names = [c.name for c in profile.column_profiles]
    assert names == ["c", "c_2", "c_3", "c_4", "c_5", "c_6"]
    assert len(set(names)) == 6
    assert all(c.header == "c" for c in profile.column_profiles)


def test_rename_skips_names_that_already_exist(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "c,c_2,c,c_3,c\n1,2,3,4,5\n"))
    names = [c.name for c in profile.column_profiles]
    assert names == ["c", "c_2", "c_4", "c_3", "c_5"]
    assert len(set(names)) == 5


def test_blank_headers_do_not_collide_with_each_other(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "column_2,,,x\n1,2,3,4\n"))
    names = [c.name for c in profile.column_profiles]
    assert len(set(names)) == 4 and names[0] == "column_2"


def test_whitespace_only_header_counts_as_blank(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,   \n1,2\n"))
    assert profile.column_profiles[1].name == "column_2"
    assert profile.column_profiles[1].header == ""


# --- missing values ----------------------------------------------------------------


def test_blank_line_and_all_empty_fields_are_reported_the_same_way(tmp_path: Path) -> None:
    """A bare '\\n' and a ',,' line are both blank rows of empty cells, not short rows."""
    profile = profile_csv(write(tmp_path, "a,b,c\n1,2,3\n\n,,\n"), chunk_size=1)
    assert profile.blank_rows == 2
    assert profile.short_rows == 0
    assert profile.rows == 3
    for col in profile.column_profiles:
        assert col.missing_breakdown["empty"] == 2
        assert col.missing_breakdown["absent"] == 0


def test_short_row_marks_absent_not_empty(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,b,c\n1\n"), chunk_size=1)
    assert profile.short_rows == 1
    assert column(profile, "b").missing_breakdown == {
        "empty": 0,
        "whitespace": 0,
        "absent": 1,
        "null_like": 0,
    }


def test_quoted_empty_string_is_an_empty_cell(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, 'a,b\n"",x\n'))
    assert column(profile, "a").missing_breakdown["empty"] == 1


def test_missing_cells_equal_the_sum_of_the_breakdown(mixed_bag: Path) -> None:
    profile = profile_csv(mixed_bag, chunk_size=13)
    for col in profile.column_profiles:
        assert col.missing == sum(col.missing_breakdown.values())
        assert col.count + col.missing == profile.rows
    assert profile.missing_cells == sum(c.missing for c in profile.column_profiles)
    assert 0 <= profile.rows_with_missing <= profile.rows
    assert profile.complete_rows == profile.rows - profile.rows_with_missing


def test_null_as_missing_keeps_counts_consistent(tmp_path: Path) -> None:
    path = write(tmp_path, "a\n1\nNA\n\n  \n2\n")
    profile = profile_csv(path, null_as_missing=True, chunk_size=2)
    col = column(profile, "a")
    assert col.missing_breakdown == {"empty": 1, "whitespace": 1, "absent": 0, "null_like": 1}
    assert col.missing == 3 and col.count == 2 and col.unique == 2
    assert profile.rows_with_missing == 3


# --- malformed and awkward files ---------------------------------------------------


def test_embedded_newlines_and_delimiters_are_one_record(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, 'a,b\n"line1\nline2","x,y"\n1,2\n'), chunk_size=1)
    assert profile.rows == 2
    assert profile.short_rows == 0 and profile.blank_rows == 0


def test_error_line_number_accounts_for_quoted_newlines(tmp_path: Path) -> None:
    path = write(tmp_path, 'a,b\n"multi\nline",2\n1,2,3\n')
    with pytest.raises(IngestionError, match="line 4, saw 3"):
        profile_csv(path, chunk_size=1)


def test_crlf_and_bom(tmp_path: Path) -> None:
    path = tmp_path / "crlf.csv"
    path.write_bytes("﻿a,b\r\n1,2\r\n3,4\r\n".encode())
    profile = profile_csv(path)
    assert [c.name for c in profile.column_profiles] == ["a", "b"]
    assert profile.rows == 2 and column(profile, "a").inferred_type == "integer"


def test_nul_byte_is_flagged_not_crashed(tmp_path: Path) -> None:
    """Python's csv module accepts NUL as data; the profile must say so."""
    path = tmp_path / "nul.csv"
    path.write_bytes(b"a,b\n1,2\n\x003,4\n")
    profile = profile_csv(path, chunk_size=1)
    assert profile.rows == 2
    assert any("control characters" in w for w in column(profile, "a").warnings)


def test_oversized_field_is_reported(tmp_path: Path) -> None:
    path = write(tmp_path, "a\n" + "x" * 200_000 + "\n")
    try:
        profile = profile_csv(path)
    except IngestionError as exc:
        assert "Cannot read CSV" in str(exc)
    else:
        assert column(profile, "a").stats["max_length"] == 200_000


def test_decoding_error_in_a_later_chunk(tmp_path: Path) -> None:
    path = tmp_path / "late.csv"
    path.write_bytes(b"a\n" + b"1\n" * 50 + b"\xff\xfe\n")
    with pytest.raises(IngestionError, match="Cannot read CSV"):
        profile_csv(path, chunk_size=5)


def test_ragged_file_where_every_row_differs_in_width(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,b,c,d\n1\n1,2\n1,2,3\n1,2,3,4\n"), chunk_size=2)
    assert profile.short_rows == 3
    assert column(profile, "d").missing_breakdown["absent"] == 3
    assert any("fewer fields" in w for w in profile.warnings)


def test_header_only_file_with_chunking(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,b\n"), chunk_size=1)
    assert profile.empty and profile.chunks == 1 and profile.rows == 0
    assert profile.missing_cells_percent == 0.0


# --- memory and chunk sizing -------------------------------------------------------


def test_default_chunk_size_is_capped_by_cell_count(tmp_path: Path) -> None:
    wide = ",".join(f"c{i}" for i in range(100))
    row = ",".join("1" for _ in range(100))
    profile = profile_csv(write(tmp_path, f"{wide}\n{row}\n"))
    assert profile.chunk_size == profiling.DEFAULT_CHUNK_CELLS // 100
    narrow = profile_csv(write(tmp_path, "a\n1\n", "narrow.csv"))
    assert narrow.chunk_size == profiling.DEFAULT_CHUNK_SIZE


def test_explicit_chunk_size_is_never_overridden(tmp_path: Path) -> None:
    wide = ",".join(f"c{i}" for i in range(100))
    row = ",".join("1" for _ in range(100))
    path = write(tmp_path, f"{wide}\n" + f"{row}\n" * 10)
    assert profile_csv(path, chunk_size=3).chunk_size == 3
    assert profile_csv(path, Config(csv_chunk_size=4)).chunk_size == 4


def test_memory_stays_bounded_for_a_high_cardinality_column(tmp_path: Path) -> None:
    import tracemalloc

    path = tmp_path / "big.csv"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("id,constant\n")
        for i in range(120_000):
            handle.write(f"id-{i},same\n")
    tracemalloc.start()
    profile = profile_csv(path, chunk_size=5_000)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert profile.rows == 120_000
    assert column(profile, "id").unique == 120_000
    assert column(profile, "constant").unique == 1
    # The file alone is ~1.5 MB of text; a non-streaming profiler would hold all
    # 120k values. Allow generous headroom for the chunk and the hash sets.
    assert peak < 60 * 2**20, f"peak {peak / 2**20:.1f} MB"


# --- in-memory frames --------------------------------------------------------------


def test_frame_with_a_non_unique_index(tmp_path: Path) -> None:
    """pd.concat leaves repeated labels; label-based assignment used to raise."""
    frame = pd.concat(
        [
            pd.DataFrame({"a": ["2026-01-01", "x"], "b": ["1", ""]}),
            pd.DataFrame({"a": ["2026-01-02", "y"], "b": ["2", "3"]}),
        ]
    )
    assert len(set(frame.index)) < len(frame)
    assert comparable(profile_frame(frame)) == comparable(
        profile_frame(frame.reset_index(drop=True))
    )
    assert column(profile_frame(frame), "a").type_counts["date"] == 2


def test_frame_with_non_string_cells(tmp_path: Path) -> None:
    frame = pd.DataFrame({"n": [1, 2, None], "s": ["a", None, "c"]})
    profile = profile_frame(frame)
    assert column(profile, "n").missing_breakdown["absent"] == 1
    assert profile.rows == 3


def test_profiler_rejects_a_chunk_with_the_wrong_width() -> None:
    profiler = DatasetProfiler(["a", "b"])
    with pytest.raises(ValueError, match="expected 2"):
        profiler.update(pd.DataFrame({"a": ["1"]}))


# --- benchmark ---------------------------------------------------------------------


def test_benchmark_script_runs_and_reports_time_and_memory(tmp_path: Path) -> None:
    out = tmp_path / "bench.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "benchmark_profiling.py"),
            "--rows",
            "2000",
            "--chunk-sizes",
            "500",
            "0",
            "--json",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    data = json.loads(out.read_text())
    assert data["rows"] == 2000
    assert data["identical_across_chunk_sizes"] is True
    assert len(data["runs"]) == 2
    for run in data["runs"]:
        assert run["seconds"] > 0
        assert run["tracemalloc_peak_mb"] > 0
        assert run["summary"]["duplicate_rows"] >= 1
        assert run["summary"]["blank_rows"] >= 1
        assert run["summary"]["short_rows"] >= 1
