"""The automation-toolkit-anomalies command: output, JSON export and exit codes."""

import json
from pathlib import Path

import pytest

from automation_toolkit.anomaly_cli import EXIT_FAILED, EXIT_OK, EXIT_OUTLIERS, main


def csv_of(tmp_path: Path, text: str, name: str = "data.csv") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


OUTLIER_CSV = "x\n1\n2\n3\n4\n5\n6\n7\n8\n9\n1000\n"
CLEAN_CSV = "x\n1\n2\n3\n4\n5\n"


def test_scan_prints_summary_and_exits_zero(tmp_path, capsys) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    assert main(["--input", str(path)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Outlier scan of data.csv" in out
    assert "1000" in out


def test_json_export_writes_one_object_for_one_input(tmp_path, capsys) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    target = tmp_path / "out" / "anomalies.json"
    assert main(["--input", str(path), "--json", str(target)]) == EXIT_OK
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["source"] == "data.csv"
    assert data["summary"]["total_outliers"] == 1
    assert str(target.resolve()) in capsys.readouterr().out


def test_json_export_writes_a_list_for_several_inputs(tmp_path) -> None:
    first = csv_of(tmp_path, OUTLIER_CSV, "a.csv")
    second = csv_of(tmp_path, CLEAN_CSV, "b.csv")
    target = tmp_path / "anomalies.json"
    assert main(["--input", str(first), str(second), "--json", str(target)]) == EXIT_OK
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [entry["source"] for entry in data] == ["a.csv", "b.csv"]


def test_strict_exits_two_only_when_outliers_were_found(tmp_path) -> None:
    dirty = csv_of(tmp_path, OUTLIER_CSV, "dirty.csv")
    clean = csv_of(tmp_path, CLEAN_CSV, "clean.csv")
    assert main(["--input", str(clean), "--strict"]) == EXIT_OK
    assert main(["--input", str(dirty), "--strict"]) == EXIT_OUTLIERS


def test_missing_file_fails_but_other_inputs_are_still_scanned(tmp_path, capsys, caplog) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    assert main(["--input", str(tmp_path / "absent.csv"), str(path)]) == EXIT_FAILED
    assert "file not found" in caplog.text
    assert "Outlier scan of data.csv" in capsys.readouterr().out


def test_unknown_column_fails(tmp_path, caplog) -> None:
    path = csv_of(tmp_path, CLEAN_CSV)
    assert main(["--input", str(path), "--column", "nope"]) == EXIT_FAILED
    assert "Unknown column" in caplog.text


def test_negative_factor_fails(tmp_path, caplog) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    assert main(["--input", str(path), "--factor", "-1"]) == EXIT_FAILED
    assert "factor" in caplog.text


def test_factor_option_changes_the_fence(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n10\n11\n12\n13\n14\n15\n16\n17\n18\n19\n40\n")
    assert main(["--input", str(path), "--strict"]) == EXIT_OUTLIERS
    assert main(["--input", str(path), "--strict", "--factor", "10"]) == EXIT_OK


def test_delimiter_override_and_column_selection(tmp_path, capsys) -> None:
    path = csv_of(tmp_path, "x;note\n1;a\n2;b\n3;c\n4;d\n900;e\n")
    assert main(["--input", str(path), "--delimiter", ";", "--column", "x"]) == EXIT_OK
    assert "900" in capsys.readouterr().out


def test_invalid_chunk_size_fails(tmp_path, caplog) -> None:
    path = csv_of(tmp_path, CLEAN_CSV)
    assert main(["--input", str(path), "--chunk-size", "0"]) == EXIT_FAILED
    assert "csv_chunk_size" in caplog.text


def test_unwritable_json_target_fails(tmp_path, caplog) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    blocker = tmp_path / "blocker"
    blocker.write_text("file, not a directory", encoding="utf-8")
    target = blocker / "sub" / "anomalies.json"
    assert main(["--input", str(path), "--json", str(target)]) == EXIT_FAILED
    assert "Cannot write" in caplog.text


def test_method_modified_zscore(tmp_path, capsys) -> None:
    path = csv_of(tmp_path, "x\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n100\n")
    assert main(["--input", str(path), "--method", "modified_zscore"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "modified z-score threshold 3.5" in out and "100 (row 12)" in out


def test_threshold_option_changes_the_cutoff(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n100\n")
    strict = ["--input", str(path), "--method", "modified_zscore", "--strict"]
    assert main(strict) == EXIT_OUTLIERS
    assert main([*strict, "--threshold", "25"]) == EXIT_OK


def test_mismatched_cutoff_options_fail(tmp_path, caplog) -> None:
    path = csv_of(tmp_path, CLEAN_CSV)
    assert (
        main(["--input", str(path), "--method", "modified_zscore", "--factor", "2"]) == EXIT_FAILED
    )
    assert "--factor applies to --method iqr" in caplog.text
    caplog.clear()
    assert main(["--input", str(path), "--threshold", "2"]) == EXIT_FAILED
    assert "--threshold applies to --method modified_zscore" in caplog.text


def test_invalid_method_is_an_argparse_error(tmp_path, capsys) -> None:
    path = csv_of(tmp_path, CLEAN_CSV)
    with pytest.raises(SystemExit) as info:
        main(["--input", str(path), "--method", "bogus"])
    assert info.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_json_export_carries_the_method(tmp_path) -> None:
    path = csv_of(tmp_path, OUTLIER_CSV)
    target = tmp_path / "z.json"
    args = ["--input", str(path), "--method", "modified_zscore", "--json", str(target)]
    assert main([*args, "--threshold", "10"]) == EXIT_OK
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["method"] == "modified_zscore" and data["threshold"] == 10.0
    assert data["factor"] is None
    assert data["columns"][0]["median"] is not None and data["columns"][0]["q1"] is None
