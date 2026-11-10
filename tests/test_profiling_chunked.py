"""Chunked (streaming) profiling and the improved header, missing-value and type handling."""

import json
from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import ConfigurationError, IngestionError
from automation_toolkit.processing import profiling
from automation_toolkit.processing.profiling import DatasetProfiler, profile_csv, profile_frame
from automation_toolkit.profile_cli import EXIT_FAILED, EXIT_OK, main


def write(tmp_path: Path, text: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def column(profile, name):
    return next(c for c in profile.column_profiles if c.name == name)


def comparable(profile) -> dict:
    data = profile.to_dict()
    del data["generated_at"], data["processing"]
    return data


@pytest.fixture
def varied(tmp_path: Path) -> Path:
    lines = ["id,amount,day,flag,note,id"]
    for i in range(1, 101):
        amount = "oops" if i == 50 else f"{i * 1.5:.2f}"
        note = ["", "  ", "NA", f"n{i % 7}"][i % 4]
        lines.append(
            f"{i:03d},{amount},2026-01-{i % 28 + 1:02d},{'yes' if i % 2 else 'no'},{note},{i}"
        )
    lines += ["1,2,2026-01-01,yes", ",,,,,", "005,7.50,2026-01-06,yes,  ,5"]  # short, blank, dup
    return write(tmp_path, "\n".join(lines) + "\n")


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 33, 103, 10_000])
def test_result_does_not_depend_on_chunk_size(varied: Path, chunk_size: int) -> None:
    whole = profile_csv(varied, chunk_size=10**6)
    chunked = profile_csv(varied, chunk_size=chunk_size)
    assert comparable(chunked) == comparable(whole)
    assert chunked.chunks == -(-chunked.rows // chunk_size)
    assert chunked.chunk_size == chunk_size


def test_chunked_counts(varied: Path) -> None:
    profile = profile_csv(varied, chunk_size=10)
    assert profile.rows == 103
    assert profile.short_rows == 1
    assert profile.blank_rows == 1
    assert profile.duplicate_rows == 1  # "005,...,5" repeats row 5 across chunks
    assert any("mostly numeric" in w and "'oops'" in w for w in column(profile, "amount").warnings)


def test_chunk_size_comes_from_config_or_default(varied: Path) -> None:
    assert profile_csv(varied).chunk_size == profiling.DEFAULT_CHUNK_SIZE
    assert profile_csv(varied, Config(csv_chunk_size=20)).chunks == 6
    with pytest.raises(ConfigurationError):
        profile_csv(varied, chunk_size=0)


def test_large_file_streams_in_bounded_chunks(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "big.csv"
    with path.open("w", encoding="utf-8") as handle:
        handle.write("n,group\n")
        for i in range(25_000):
            handle.write(f"{i},{i % 3}\n")
    seen: list[int] = []
    original = DatasetProfiler.update

    def spy(self, chunk):
        seen.append(len(chunk))
        original(self, chunk)

    monkeypatch.setattr(DatasetProfiler, "update", spy)
    profile = profile_csv(path, chunk_size=4_000)
    assert max(seen) == 4_000 and sum(seen) == 25_000
    n = column(profile, "n")
    assert (n.stats["min"], n.stats["max"], n.unique) == (0, 24_999, 25_000)
    assert n.stats["mean"] == pytest.approx(12_499.5)
    assert n.stats["std"] == pytest.approx(pd.Series(range(25_000)).std())
    assert column(profile, "group").unique == 3


def test_median_is_estimated_beyond_sample_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(profiling, "MEDIAN_SAMPLE_LIMIT", 500)
    path = write(tmp_path, "x\n" + "\n".join(str(i) for i in range(5_000)) + "\n")
    stats = column(profile_csv(path, chunk_size=300), "x").stats
    assert stats["median_approximate"] is True
    assert 2_000 < stats["median"] < 3_000
    small = column(profile_csv(write(tmp_path, "x\n1\n2\n10\n", "s.csv")), "x").stats
    assert (small["median"], small["median_approximate"]) == (2, False)


def test_top_values_stay_exact_beyond_tracking_limit(tmp_path, monkeypatch) -> None:
    """Once beyond the limit, top values used to be marked approximate and could miss
    values; only the text kept is limited now, counts stay exact."""
    monkeypatch.setattr(profiling, "TRACKED_VALUE_LIMIT", 10)
    path = write(tmp_path, "v\n" + "\n".join(f"v{i}" for i in range(50)) + "\nv0\nv49\nv49\n")
    col = column(profile_csv(path, chunk_size=7), "v")
    assert col.unique == 50
    assert col.top_values == [{"value": "v49", "count": 3}, {"value": "v0", "count": 2}]
    assert col.top_values_exact is True
    assert not any("approximate" in w for w in col.warnings)


def test_malformed_record_in_a_later_chunk(tmp_path: Path) -> None:
    rows = "\n".join("1,2" for _ in range(20))
    path = write(tmp_path, f"a,b\n{rows}\n1,2,3\n")
    with pytest.raises(IngestionError, match="Expected 2 fields in line 22, saw 3"):
        profile_csv(path, chunk_size=5)


def test_blank_header_line_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="line 1 is blank"):
        profile_csv(write(tmp_path, "\na,b\n1,2\n"))


def test_bad_encoding_name(tmp_path: Path) -> None:
    with pytest.raises(IngestionError):
        profile_csv(write(tmp_path, "a\n1\n"), Config(csv_encoding="no-such-codec"))


# Duplicate headers -----------------------------------------------------------------


def test_duplicate_headers_get_unique_names(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,a,a_2,a,,b\n1,x,2,y,3,4\n"))
    names = [c.name for c in profile.column_profiles]
    assert names == ["a", "a_3", "a_2", "a_4", "column_5", "b"]
    assert len(set(names)) == len(names)
    assert [c.header for c in profile.column_profiles][:4] == ["a", "a", "a_2", "a"]
    assert column(profile, "a_3").inferred_type == "string"  # kept separate from "a"
    assert column(profile, "a").inferred_type == "integer"
    warning = next(w for w in profile.warnings if "duplicate header" in w)
    assert "'a' at position 2 -> a_3" in warning
    data = profile.to_dict()["columns"]
    assert {c["name"] for c in data} == set(names)


def test_headers_differing_only_in_case_or_spacing(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "Order ID,order  id,Total\n1,2,3\n"))
    assert any("case or spacing" in w and "'Order ID'" in w for w in profile.warnings)
    assert [c.name for c in profile.column_profiles] == ["Order ID", "order  id", "Total"]


def test_blank_header_name_does_not_collide(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "column_2,\n1,2\n"))
    names = [c.name for c in profile.column_profiles]
    assert names[0] == "column_2" and names[1] != "column_2"


# Missing values --------------------------------------------------------------------


def test_missing_breakdown(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,b\n1,\n2,   \n3\n4,NA\n5,x\n"))
    b = column(profile, "b")
    assert b.missing_breakdown == {"empty": 1, "whitespace": 1, "absent": 1, "null_like": 0}
    assert b.missing == 3 and b.null_like == 1 and b.count == 2
    assert profile.rows_with_missing == 3
    assert profile.complete_rows == 2
    assert any("whitespace-only" in w for w in b.warnings)
    assert profile.to_dict()["summary"]["complete_rows"] == 2


def test_null_as_missing_and_extra_tokens(tmp_path: Path) -> None:
    path = write(tmp_path, "a,b\n1,NA\n2,#N/A\n3,x\n4,null\n")
    default = column(profile_csv(path), "b")
    assert (default.missing, default.null_like) == (0, 2)
    strict = profile_csv(path, null_as_missing=True, null_tokens=["#N/A"])
    b = column(strict, "b")
    assert b.null_like == 3
    assert b.missing == 3 and b.missing_breakdown["null_like"] == 3
    assert b.count == 1 and b.unique == 1
    assert strict.rows_with_missing == 3
    assert strict.to_dict()["processing"]["null_as_missing"] is True


# Mixed column types ----------------------------------------------------------------


def test_mixed_column(tmp_path: Path) -> None:
    values = ["1", "2", "3", "abc", "def", "2026-01-01", "yes"]
    path = write(tmp_path, "m\n" + "\n".join(values) + "\n")
    m = column(profile_csv(path, chunk_size=2), "m")
    assert m.inferred_type == "mixed"
    assert m.dominant_type == "integer"
    assert m.dominant_share == pytest.approx(3 / 7, abs=1e-4)
    assert m.type_examples["string"] == ["abc", "def"]
    assert m.stats["of"] == "integer values only" and m.stats["max"] == 3
    assert any(w.startswith("mixed types: 42.9% numeric") for w in m.warnings)


def test_mostly_typed_column_stays_string_with_outliers(tmp_path: Path) -> None:
    path = write(tmp_path, "d\n" + "\n".join(["2026-01-01"] * 19 + ["2026-02-30"]) + "\n")
    d = column(profile_csv(path, chunk_size=3), "d")
    assert d.inferred_type == "string"
    assert d.dominant_type == "date"
    assert any("mostly date (95.0%)" in w and "'2026-02-30'" in w for w in d.warnings)


def test_integers_and_floats_combine_into_float(tmp_path: Path) -> None:
    x = column(profile_csv(write(tmp_path, "x\n1\n2.5\n-3\n"), chunk_size=1), "x")
    assert x.inferred_type == "float" and x.dominant_type == "float"
    assert x.dominant_share == 1.0


def test_leading_zero_identifiers(tmp_path: Path) -> None:
    col = column(profile_csv(write(tmp_path, "zip\n01234\n10115\n00501\n")), "zip")
    assert col.inferred_type == "integer"
    assert any("2 integer value(s) with leading zeros" in w for w in col.warnings)


# In-memory API and CLI -------------------------------------------------------------


def test_profile_frame_and_incremental_profiler_agree() -> None:
    frame = pd.DataFrame({"a": ["1", "2", "2", None], "b": ["x", "", "", "y"]})
    one = profile_frame(frame)
    profiler = DatasetProfiler(frame.columns)
    for start in range(0, 4, 3):
        profiler.update(frame.iloc[start : start + 3])
    assert comparable(profiler.result()) == comparable(one)
    assert one.short_rows == 1 and one.duplicate_rows == 1
    with pytest.raises(ValueError, match="expected 2"):
        profiler.update(frame[["a"]])


def test_cli_chunk_and_null_options(tmp_path: Path, varied: Path, caplog) -> None:
    out = tmp_path / "p.json"
    args = ["--input", str(varied), "--chunk-size", "9", "--null-as-missing", "--json", str(out)]
    assert main([*args, "--null-token", "oops"]) == EXIT_OK
    data = json.loads(out.read_text())
    assert data["processing"] == {"chunks": 12, "chunk_size": 9, "null_as_missing": True}
    amount = next(c for c in data["columns"] if c["name"] == "amount")
    assert amount["inferred_type"] == "float" and amount["missing"] == 2
    assert main(["--input", str(varied), "--chunk-size", "0"]) == EXIT_FAILED
    assert "chunk" in caplog.text.lower()
