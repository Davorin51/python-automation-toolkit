import json
from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError, ToolkitError
from automation_toolkit.processing.profiling import profile_csv, profile_frame
from automation_toolkit.profile_cli import EXIT_FAILED, EXIT_OK, EXIT_WARNINGS, main
from automation_toolkit.reporting.profile_report import write_profile_json

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path: Path, text: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def column(profile, name):
    return next(c for c in profile.column_profiles if c.name == name)


@pytest.fixture
def messy(tmp_path: Path) -> Path:
    return write(
        tmp_path,
        "id,name,price,joined,active,notes\n"
        "1,Ann,9.50,2026-01-01,yes,\n"
        "2,Bob,10,2026-01-02,no,\n"
        "2,Bob,10,2026-01-02,no,\n"
        ",,,,,\n"
        "4, Cy ,abc,2026-01-04,yes,\n"
        "5,Dee,7.25,2026-01-05,no,NA\n",
    )


def test_dataset_summary(messy: Path) -> None:
    profile = profile_csv(messy)
    assert (profile.rows, profile.columns) == (6, 6)
    assert profile.duplicate_rows == 1
    assert profile.blank_rows == 1
    assert profile.short_rows == 0
    assert profile.total_cells == 36
    assert profile.missing_cells == sum(c.missing for c in profile.column_profiles)
    assert profile.duplicate_rows_percent == pytest.approx(16.67)


def test_column_types_and_stats(messy: Path) -> None:
    profile = profile_csv(messy)
    ident = column(profile, "id")
    assert ident.inferred_type == "integer"
    assert ident.missing == 1
    assert ident.stats["min"] == 1 and ident.stats["max"] == 5
    assert column(profile, "joined").inferred_type == "date"
    assert column(profile, "joined").stats["min"].startswith("2026-01-01")
    assert column(profile, "active").inferred_type == "boolean"
    assert column(profile, "name").inferred_type == "string"
    assert column(profile, "name").whitespace_padded == 1


def test_mostly_numeric_column_is_flagged(tmp_path: Path) -> None:
    rows = "\n".join(str(i) for i in range(1, 20))
    profile = profile_csv(write(tmp_path, f"amount\n{rows}\noops\n"))
    amount = column(profile, "amount")
    assert amount.inferred_type == "string"
    assert any("mostly numeric" in w and "'oops'" in w for w in amount.warnings)


def test_float_and_placeholders(messy: Path) -> None:
    profile = profile_csv(messy)
    notes = column(profile, "notes")
    assert notes.null_like == 1
    assert notes.inferred_type == "empty"
    assert any("placeholder" in w for w in notes.warnings)
    mixed = profile_frame(pd.DataFrame({"x": ["1", "2.5", "3"]}))
    assert mixed.column_profiles[0].inferred_type == "float"
    assert mixed.column_profiles[0].stats["mean"] == pytest.approx(2.166667)


def test_constant_and_empty_columns(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "k,e\nA,\nA,\nA,\n"))
    assert any("constant" in w for w in column(profile, "k").warnings)
    assert column(profile, "e").warnings == ["column is entirely empty"]


def test_header_only_is_an_empty_dataset(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,b\n"))
    assert profile.empty
    assert profile.columns == 2
    assert profile.missing_cells_percent == 0.0
    assert [c.inferred_type for c in profile.column_profiles] == ["empty", "empty"]
    assert "no data rows" in profile.warnings[0]
    json.dumps(profile.to_dict())  # still serializable


@pytest.mark.parametrize("content", [b"", b"\n\n  \n"])
def test_empty_file_raises(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "empty.csv"
    path.write_bytes(content)
    with pytest.raises(IngestionError, match="empty"):
        profile_csv(path)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="not found"):
        profile_csv(tmp_path / "nope.csv")


def test_too_many_fields_raises(tmp_path: Path) -> None:
    with pytest.raises(IngestionError, match="Expected 2 fields"):
        profile_csv(write(tmp_path, "a,b\n1,2\n1,2,3\n"))


def test_undecodable_file_raises(tmp_path: Path) -> None:
    path = tmp_path / "binary.csv"
    path.write_bytes(b"\xff\xfe\x00a,b\n")
    with pytest.raises(ToolkitError):
        profile_csv(path)


def test_short_rows_are_reported(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, 'a,b,c\n1,2,3\n4\n"5,x",6,7\n'))
    assert profile.short_rows == 1
    assert profile.rows == 3
    assert any("fewer fields" in w for w in profile.warnings)


def test_duplicate_and_blank_headers(tmp_path: Path) -> None:
    profile = profile_csv(write(tmp_path, "a,a,\n1,2,3\n"))
    assert [c.name for c in profile.column_profiles] == ["a", "a_2", "column_3"]
    assert [c.header for c in profile.column_profiles] == ["a", "a", ""]
    assert any("duplicate header" in w for w in profile.warnings)
    assert any("column_3" in w for w in profile.warnings)


def test_config_delimiter_and_date_format(tmp_path: Path) -> None:
    path = write(tmp_path, "d;n\n05.01.2026;1\n06.01.2026;2\n")
    config = Config(csv_delimiter=";", date_format="%d.%m.%Y")
    profile = profile_csv(path, config)
    assert column(profile, "d").inferred_type == "date"
    assert column(profile, "n").inferred_type == "integer"


def test_profiles_bundled_sales_fixture() -> None:
    profile = profile_csv(ROOT / "examples" / "sample_sales_dirty.csv")
    assert profile.rows == 357
    assert profile.duplicate_rows >= 1
    assert profile.to_dict()["summary"]["rows"] == 357


def test_json_output_is_plain(tmp_path: Path, messy: Path) -> None:
    out = tmp_path / "nested" / "profile.json"
    write_profile_json(out, profile_csv(messy))
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["schema_version"] == 2
    assert data["summary"]["duplicate_rows"] == 1
    assert [c["name"] for c in data["columns"]][:2] == ["id", "name"]


def test_text_report(messy: Path) -> None:
    text = profile_csv(messy).to_text()
    assert "Rows: 6" in text and "Duplicate rows: 1" in text and "Warnings:" in text


def test_cli_success_and_json(tmp_path: Path, messy: Path, capsys) -> None:
    out = tmp_path / "p.json"
    assert main(["--input", str(messy), "--json", str(out)]) == EXIT_OK
    assert "Profile of data.csv" in capsys.readouterr().out
    assert json.loads(out.read_text())["source"] == "data.csv"


def test_cli_multiple_inputs_writes_list(tmp_path: Path, messy: Path) -> None:
    other = write(tmp_path, "x\n1\n", "other.csv")
    out = tmp_path / "all.json"
    assert main(["--input", str(messy), str(other), "--json", str(out)]) == EXIT_OK
    assert [p["source"] for p in json.loads(out.read_text())] == ["data.csv", "other.csv"]


def test_cli_strict_and_failures(tmp_path: Path, messy: Path, capsys) -> None:
    clean = write(tmp_path, "x,y\n1,a\n2,b\n", "clean.csv")
    assert main(["--input", str(clean), "--strict"]) == EXIT_OK
    assert main(["--input", str(messy), "--strict"]) == EXIT_WARNINGS
    bad = write(tmp_path, "", "bad.csv")
    assert main(["--input", str(clean), str(bad)]) == EXIT_FAILED
    assert "Profile of clean.csv" in capsys.readouterr().out


def test_cli_delimiter_override_and_bad_config(tmp_path: Path) -> None:
    path = write(tmp_path, "a;b\n1;2\n")
    assert main(["--input", str(path), "--delimiter", ";"]) == EXIT_OK
    assert main(["--input", str(path), "--delimiter", ";;"]) == EXIT_FAILED
