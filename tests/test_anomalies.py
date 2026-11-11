"""IQR outlier detection: bounds, exclusions, column selection and chunk independence."""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.processing import anomalies
from automation_toolkit.processing.anomalies import (
    OutlierDetector,
    detect_outliers_csv,
    detect_outliers_frame,
)
from automation_toolkit.processing.profiling import _scalar

CHUNKS = [1, 3, 50, 100_000]


def csv_of(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "data.csv"
    path.write_text(text, encoding="utf-8")
    return path


def scan_values(tmp_path: Path, values, **kwargs):
    path = csv_of(tmp_path, "x\n" + "\n".join(map(str, values)) + "\n")
    return detect_outliers_csv(path, **kwargs)


# --- detection ------------------------------------------------------------------------


def test_detects_known_outliers_with_tukey_bounds(tmp_path) -> None:
    values = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 1000]
    report = scan_values(tmp_path, values)
    [column] = report.column_results
    q1, q3 = np.percentile(np.array(values, dtype=float), [25, 75])
    assert column.q1 == q1 and column.q3 == q3
    assert column.lower_bound == q1 - 1.5 * (q3 - q1)
    assert column.upper_bound == q3 + 1.5 * (q3 - q1)
    assert column.outliers == 1 and column.high_outliers == 1 and column.low_outliers == 0
    assert report.total_outliers == 1
    # Row numbers are file record numbers including the header: 1000 is record 12.
    assert column.examples == [{"row": 12, "value": 1000.0}]


def test_low_and_high_outliers_and_example_order(tmp_path) -> None:
    values = [-500, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 300, 900]
    [column] = scan_values(tmp_path, values).column_results
    assert column.low_outliers == 1 and column.high_outliers == 2
    # Most extreme first, by distance outside the nearest bound.
    assert [e["value"] for e in column.examples] == [900.0, -500.0, 300.0]


def test_no_outliers_in_uniform_data(tmp_path) -> None:
    [column] = scan_values(tmp_path, range(1, 101)).column_results
    assert column.outliers == 0 and column.examples == []
    assert column.values == 100


def test_factor_zero_flags_everything_outside_the_quartiles(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, 3, 4, 5, 6, 7, 8], factor=0).column_results
    assert (column.lower_bound, column.upper_bound) == (column.q1, column.q3)
    assert column.outliers == column.low_outliers + column.high_outliers > 0


def test_larger_factor_keeps_more_values(tmp_path) -> None:
    values = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 40]
    assert scan_values(tmp_path, values, factor=1.5).total_outliers == 1
    assert scan_values(tmp_path, values, factor=10).total_outliers == 0


@pytest.mark.parametrize("factor", [-1, float("nan"), float("inf"), "wide", True])
def test_invalid_factor_is_rejected(tmp_path, factor) -> None:
    with pytest.raises(ValueError, match="factor"):
        scan_values(tmp_path, [1, 2, 3, 4], factor=factor)


def test_constant_column_warns_about_zero_iqr(tmp_path) -> None:
    [column] = scan_values(tmp_path, [5, 5, 5, 5, 5, 7]).column_results
    assert column.iqr == 0 and column.outliers == 1
    assert any("interquartile range is 0" in w for w in column.warnings)


def test_too_few_values_reports_no_bounds(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, 3]).column_results
    assert column.values == 3 and column.outliers == 0
    assert column.q1 is None and column.lower_bound is None
    assert any("at least 4" in w for w in column.warnings)


# --- missing, placeholder and invalid values ------------------------------------------


def test_missing_and_placeholder_values_are_excluded_and_counted(tmp_path) -> None:
    path = csv_of(
        tmp_path,
        "x\n1\n\n   \n2\nNA\nnull\n3\n4\n",  # one blank row, one whitespace row
    )
    [column] = detect_outliers_csv(path).column_results
    assert column.values == 4
    assert column.missing == 2
    assert column.null_like == 2
    assert column.invalid == 0
    assert any("placeholder" in w for w in column.warnings)


def test_short_rows_count_absent_fields_as_missing(tmp_path) -> None:
    path = csv_of(tmp_path, "x,y\n1,2\n3\n4,5\n6,7\n8,9\n")
    report = detect_outliers_csv(path)
    y = next(c for c in report.column_results if c.name == "y")
    assert y.values == 4 and y.missing == 1


def test_invalid_values_are_ignored_with_a_warning(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, "oops", 3, 4, 5, 6, 7, 8, 9, 10]).column_results
    assert column.values == 10 and column.invalid == 1
    assert any("non-numeric" in w and "'oops'" in w for w in column.warnings)


def test_value_beyond_float_range_counts_as_invalid(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, 3, 4, "9" * 400, "1e400"], columns=["x"]).column_results
    assert column.values == 4 and column.invalid == 2


def test_custom_null_tokens_are_placeholders(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, 3, 4, "#N/A"], null_tokens=["#N/A"]).column_results
    assert column.null_like == 1 and column.invalid == 0


def test_mostly_non_numeric_column_is_skipped_with_a_warning(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n1\na\nb\nc\nd\n")
    report = detect_outliers_csv(path)
    assert report.column_results == []
    assert any("skipped" in w and "'x'" in w for w in report.warnings)


def test_pure_text_column_is_skipped_silently(tmp_path) -> None:
    path = csv_of(tmp_path, "name,amount\na,1\nb,2\nc,3\nd,4\ne,500\n")
    report = detect_outliers_csv(path)
    assert [c.name for c in report.column_results] == ["amount"]
    assert report.warnings == []


def test_no_numeric_columns_warns(tmp_path) -> None:
    path = csv_of(tmp_path, "a,b\nx,y\nz,w\n")
    report = detect_outliers_csv(path)
    assert report.column_results == []
    assert any("no numeric columns" in w for w in report.warnings)


# --- column selection -----------------------------------------------------------------


def test_requested_column_is_analyzed_even_when_mostly_text(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n1\n2\n3\n4\na\nb\nc\nd\n")
    report = detect_outliers_csv(path, columns=["x"])
    [column] = report.column_results
    assert column.values == 4 and column.invalid == 4
    assert report.warnings == []


def test_requested_column_without_numbers_reports_clearly(tmp_path) -> None:
    path = csv_of(tmp_path, "name\na\nb\n")
    [column] = detect_outliers_csv(path, columns=["name"]).column_results
    assert column.values == 0 and column.outliers == 0
    assert any("no numeric values" in w for w in column.warnings)


def test_unknown_column_raises(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n1\n")
    with pytest.raises(ValueError, match="Unknown column.*nope.*available: x"):
        detect_outliers_csv(path, columns=["nope"])


def test_duplicate_headers_get_unique_names(tmp_path) -> None:
    path = csv_of(tmp_path, "a,a\n1,10\n2,20\n3,30\n4,40\n5,5000\n")
    report = detect_outliers_csv(path, columns=["a_2"])
    [column] = report.column_results
    assert column.name == "a_2" and column.header == "a"
    assert column.outliers == 1


# --- file handling and the frame API --------------------------------------------------


def test_missing_file_raises(tmp_path) -> None:
    with pytest.raises(IngestionError, match="file not found"):
        detect_outliers_csv(tmp_path / "absent.csv")


def test_empty_file_raises(tmp_path) -> None:
    with pytest.raises(IngestionError, match="empty"):
        detect_outliers_csv(csv_of(tmp_path, ""))


def test_header_only_file_is_an_empty_dataset(tmp_path) -> None:
    report = detect_outliers_csv(csv_of(tmp_path, "x,y\n"))
    assert report.rows == 0 and report.column_results == []
    assert any("no data rows" in w for w in report.warnings)


def test_configured_delimiter_is_used(tmp_path) -> None:
    path = csv_of(tmp_path, "x;y\n1;10\n2;20\n3;30\n4;40\n5;900\n")
    report = detect_outliers_csv(path, Config(csv_delimiter=";"))
    assert {c.name for c in report.column_results} == {"x", "y"}


def test_frame_api_uses_one_based_rows(tmp_path) -> None:
    frame = pd.DataFrame({"x": ["1", "2", "3", "4", "5", "6", "7", "8", "9", "100"]})
    [column] = detect_outliers_frame(frame).column_results
    assert column.examples == [{"row": 10, "value": 100.0}]


@pytest.mark.parametrize("chunk_size", CHUNKS)
def test_result_is_independent_of_chunk_size(tmp_path, chunk_size) -> None:
    rng = np.random.default_rng(7)
    values = [str(v) for v in rng.normal(50, 10, 500).round(3)]
    values[100] = "999"
    values[200] = ""
    values[300] = "NA"
    values[400] = "bad"
    path = csv_of(tmp_path, "x\n" + "\n".join(values) + "\n")
    expected = detect_outliers_csv(path).to_dict()
    chunked = detect_outliers_csv(path, chunk_size=chunk_size).to_dict()
    del expected["generated_at"], chunked["generated_at"]
    assert chunked == expected


def test_to_dict_is_json_serializable_and_versioned(tmp_path) -> None:
    report = scan_values(tmp_path, [1, 2, 3, 4, 5, 1000])
    data = json.loads(json.dumps(report.to_dict()))
    assert data["schema_version"] == 2 and data["method"] == "iqr"
    assert data["summary"] == {"rows": 6, "columns_analyzed": 1, "total_outliers": 1}


def test_to_text_names_bounds_outliers_and_warnings(tmp_path) -> None:
    path = csv_of(tmp_path, "x\n1\n2\n3\n4\n5\nNA\n1000\n")
    text = detect_outliers_csv(path).to_text()
    assert "Outlier scan of data.csv" in text
    assert "1000 (row 8)" in text
    assert "Warnings:" in text and "placeholder" in text


def test_detector_rejects_mismatched_chunk_width() -> None:
    detector = OutlierDetector(["a", "b"])
    with pytest.raises(ValueError, match="expected 2"):
        detector.update(pd.DataFrame({"a": ["1"]}))


# --- regression: storage, chunk/block boundaries and row numbers ----------------------
# The detector stores values in fixed-size blocks and the rows that held them as a bit
# set. These tests shrink the blocks so every boundary (chunk, block, packed byte) is
# crossed many times, and compare against a straightforward reference implementation.


def reference(values: list[str], factor: float = 1.5, first_row: int = 2) -> dict:
    """Brute-force IQR result for one column of raw text cells."""
    rows, numbers = [], []
    for i, text in enumerate(values):
        try:
            number = float(text)
        except ValueError:
            continue
        if np.isfinite(number):
            rows.append(i + first_row)
            numbers.append(number)
    q1, q3 = np.percentile(np.array(numbers), [25, 75])
    lower, upper = q1 - factor * (q3 - q1), q3 + factor * (q3 - q1)
    outside = [
        (lower - v if v < lower else v - upper, r, v)
        for r, v in zip(rows, numbers, strict=True)
        if v < lower or v > upper
    ]
    outside.sort(key=lambda item: (-item[0], item[1]))
    return {
        "low": sum(v < lower for v in numbers),
        "high": sum(v > upper for v in numbers),
        "examples": [(r, v) for _, r, v in outside[:5]],
        "values": len(numbers),
    }


@pytest.fixture
def tiny_blocks(monkeypatch):
    monkeypatch.setattr(anomalies, "_BLOCK_MIN", 2)
    monkeypatch.setattr(anomalies, "_BLOCK_MAX", 5)


@pytest.mark.parametrize("chunk_size", [1, 3, 7, 8, 9, 100_000])
@pytest.mark.parametrize("seed", range(6))
def test_matches_reference_across_chunk_and_block_boundaries(
    tmp_path, tiny_blocks, chunk_size, seed
) -> None:
    rng = np.random.default_rng(seed)
    cells = [str(v) for v in rng.normal(0, 1, 120).round(2)]
    for i in rng.choice(120, 25, replace=False):
        cells[i] = str(rng.choice(["", "NA", "x", "50", "-40", "7.5"]))
    expected = reference(cells)
    [column] = scan_values(tmp_path, cells, chunk_size=chunk_size, columns=["x"]).column_results
    assert column.values == expected["values"]
    assert (column.low_outliers, column.high_outliers) == (expected["low"], expected["high"])
    assert [(e["row"], e["value"]) for e in column.examples] == expected["examples"]


def test_example_ties_are_ordered_by_row_across_chunks(tmp_path, tiny_blocks) -> None:
    values = [str(v) for v in range(1, 41)] + ["500"] * 7
    for chunk_size in (1, 2, 5, 100):
        [column] = scan_values(tmp_path, values, chunk_size=chunk_size).column_results
        assert column.high_outliers == 7
        # Equal distance: the earliest rows first (records 42..46; record 1 is the header).
        assert [e["row"] for e in column.examples] == [42, 43, 44, 45, 46]


def test_rows_count_blank_lines_and_short_records(tmp_path) -> None:
    # Record numbers count every record after the header, blank lines and short
    # records included, matching source_row in the pipeline.
    path = csv_of(tmp_path, "id,x\na,1\n\nb,2\nc\nd,3\ne,4\nf,5\ng,500\n")
    for chunk_size in (1, 2, 100):
        [column] = detect_outliers_csv(path, columns=["x"], chunk_size=chunk_size).column_results
        assert column.examples == [{"row": 9, "value": 500.0}]
        assert column.missing == 2  # the blank line and the absent field


def test_quoted_line_break_is_one_record(tmp_path) -> None:
    path = csv_of(tmp_path, 'note,x\n"two\nlines",1\nb,2\nc,3\nd,4\ne,999\n')
    [column] = detect_outliers_csv(path, columns=["x"]).column_results
    assert column.examples == [{"row": 6, "value": 999.0}]


def test_value_on_the_fence_is_not_an_outlier(tmp_path) -> None:
    # Q1 3.5, Q3 8.5, IQR 5: the upper fence is exactly 16.
    [column] = scan_values(tmp_path, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 16]).column_results
    assert column.upper_bound == 16.0 and column.outliers == 0
    [column] = scan_values(tmp_path, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 16.01]).column_results
    assert column.upper_bound == 16.0 and column.outliers == 1


def test_identical_values_have_no_outliers(tmp_path) -> None:
    [column] = scan_values(tmp_path, [3] * 10).column_results
    assert column.iqr == 0 and column.outliers == 0


def test_one_sided_fence_overflow_is_reported_not_crashing(tmp_path) -> None:
    """Used to raise TypeError in to_text(): the upper bound overflowed to None."""
    report = scan_values(tmp_path, ["0", "0", "1e308", "1e308", "1e308", "1e308"])
    [column] = report.column_results
    assert column.lower_bound is not None and column.upper_bound is None
    assert column.outliers == 0
    assert any("beyond the float range" in w for w in column.warnings)
    assert ", inf]" in report.to_text()


def test_two_sided_fence_overflow_warns(tmp_path) -> None:
    """Used to return null bounds and no outliers without any explanation."""
    report = scan_values(tmp_path, ["1e308", "-1e308"] * 3)
    [column] = report.column_results
    assert column.lower_bound is None and column.upper_bound is None
    assert any("beyond the float range" in w for w in column.warnings)
    assert "[-inf, inf]" in report.to_text()


def test_tiny_spread_keeps_enough_precision(tmp_path) -> None:
    """Used to report Q1, Q3 and both bounds as 0.123457 while flagging an outlier."""
    values = ["0.1234567891", "0.1234567892", "0.1234567893", "0.1234567894", "0.12345679"]
    [column] = scan_values(tmp_path, values).column_results
    assert column.q1 < column.q3 and column.lower_bound < column.upper_bound
    [example] = column.examples
    assert example["value"] > column.upper_bound


def test_ordinary_numbers_keep_the_previous_rounding(tmp_path) -> None:
    # Q1 - 1.5 * IQR is -0.20000000000000004 in floats; reported as before.
    [column] = scan_values(tmp_path, [0.1, 0.1, 0.2, 0.3, 0.3]).column_results
    assert column.lower_bound == -0.2
    [column] = scan_values(tmp_path, ["8.8122505"] * 3 + ["1", "2"] * 3).column_results
    assert column.q3 == _scalar(8.8122505)  # the 6-decimal rounding used before


def test_unrequested_columns_are_not_stored(tmp_path) -> None:
    detector = OutlierDetector(["a", "b"], columns=["b"])
    detector.update(pd.DataFrame([["1", "2"], ["3", "4"]]))
    assert detector._states[0].values.size == 0
    assert detector._states[1].values.size == 2


def test_result_can_be_called_repeatedly(tmp_path) -> None:
    detector = OutlierDetector(["x"])
    detector.update(pd.DataFrame({"x": [str(v) for v in [1, 2, 3, 4, 5, 6, 100]]}))
    first, second = detector.result().to_dict(), detector.result().to_dict()
    del first["generated_at"], second["generated_at"]
    assert first == second
    detector.update(pd.DataFrame({"x": ["-200"]}))  # still usable afterwards
    assert detector.result().column_results[0].low_outliers == 1


def test_block_buffer_and_row_bits(tiny_blocks) -> None:
    buffer = anomalies._Blocks(np.float64)
    bits = anomalies._RowBits()
    expected_values, expected_rows, row = [], [], 0
    rng = np.random.default_rng(3)
    for size in [0, 1, 3, 7, 8, 13, 2]:
        mask = rng.random(size) < 0.5
        values = rng.random(int(mask.sum()))
        buffer.append(values)
        bits.append(mask)
        expected_values.extend(values)
        expected_rows.extend(np.flatnonzero(mask) + row)
        row += size
    assert buffer.copy().tolist() == expected_values
    assert np.concatenate(buffer.views()).tolist() == expected_values
    ordinals = list(range(len(expected_rows)))[::-1]
    assert bits.rows_of(ordinals) == [expected_rows[o] for o in ordinals]


# --- regression: parsing fast path, extreme floats and empty datasets ------------------
# Parsing now tries every cell as-is and only strips/classifies the cells that failed,
# so these tests pin the classifications and orderings that must not drift.


def test_padded_numbers_are_numeric_in_row_order(tmp_path) -> None:
    # ASCII padding parses directly; the NBSP-padded value takes the strip-and-retry
    # path and must still land in row order between its neighbours.
    cells = ["1", " 2 ", "\t3", " 4 ", "5 ", "6", "1000"]
    for chunk_size in (1, 3, 100):
        [column] = scan_values(tmp_path, cells, chunk_size=chunk_size).column_results
        assert column.values == 7 and column.invalid == 0 and column.missing == 0
        assert column.examples == [{"row": 8, "value": 1000.0}]


@pytest.mark.parametrize(
    "text", ["inf", "-inf", "Infinity", "1e400", "0x10", "1_000", "true", "+-1"]
)
def test_classic_invalid_numerics_are_invalid(tmp_path, text) -> None:
    [column] = scan_values(tmp_path, ["1", "2", "3", "4", text], columns=["x"]).column_results
    assert column.values == 4 and column.invalid == 1
    assert any(repr(text) in w for w in column.warnings)


def test_decimal_comma_is_invalid(tmp_path) -> None:
    path = csv_of(tmp_path, 'x\n1\n2\n3\n4\n"1,5"\n')
    [column] = detect_outliers_csv(path, columns=["x"]).column_results
    assert column.values == 4 and column.invalid == 1
    assert any("'1,5'" in w for w in column.warnings)


@pytest.mark.parametrize("text", ["nan", "NaN", "NAN"])
def test_nan_text_is_a_placeholder_not_invalid(tmp_path, text) -> None:
    [column] = scan_values(tmp_path, ["1", "2", "3", "4", text]).column_results
    assert column.values == 4 and column.null_like == 1 and column.invalid == 0


def test_invalid_examples_keep_row_order_mixing_retried_and_direct(tmp_path) -> None:
    # "bad1" fails as-is; " x " is retried after stripping and still fails;
    # the examples must list them in row order regardless of which path found them.
    cells = ["1", "bad1", " x ", "bad2", "2", "3", "4"]
    [column] = scan_values(tmp_path, cells, columns=["x"]).column_results
    assert column.invalid == 3
    assert any("'bad1', 'x', 'bad2'" in w for w in column.warnings)


def test_duplicate_and_blank_headers_are_scanned_separately(tmp_path) -> None:
    path = csv_of(tmp_path, "a,a,\n1,100,7\n2,200,8\n3,300,9\n4,400,10\n5,9000,11\n")
    report = detect_outliers_csv(path)
    by_name = {c.name: c for c in report.column_results}
    assert set(by_name) == {"a", "a_2", "column_3"}
    assert by_name["a"].outliers == 0
    assert by_name["a_2"].outliers == 1 and by_name["a_2"].header == "a"
    [column] = detect_outliers_csv(path, columns=["column_3"]).column_results
    assert column.values == 5 and column.header == ""


def test_header_only_file_with_requested_column(tmp_path) -> None:
    report = detect_outliers_csv(csv_of(tmp_path, "x,y\n"), columns=["y"])
    assert report.rows == 0
    [column] = report.column_results
    assert column.values == 0 and column.outliers == 0
    assert any("no numeric values" in w for w in column.warnings)
    assert any("no data rows" in w for w in report.warnings)


def test_file_of_only_blank_rows(tmp_path) -> None:
    report = detect_outliers_csv(csv_of(tmp_path, "x\n\n\n\n"))
    assert report.rows == 3 and report.column_results == []
    assert any("no numeric columns" in w for w in report.warnings)


def test_example_near_float_max_does_not_overflow_rounding(tmp_path) -> None:
    """Used to raise OverflowError: rounding 1.7e308 to 15 significant digits of its
    magnitude can round past the float maximum."""
    cells = ["1e307", "2e307", "3e307", "4e307", "1.7976931348623157e308"]
    for chunk_size in (1, 100):
        report = scan_values(tmp_path, cells, chunk_size=chunk_size)
        [column] = report.column_results
        assert column.high_outliers == 1
        [example] = column.examples
        assert example["value"] == 1.7976931348623157e308
        json.dumps(report.to_dict())
        report.to_text()


def test_outlier_distance_overflow_is_silent(tmp_path, recwarn) -> None:
    """Used to emit RuntimeWarning: the distance of -1e308 beyond a 1e308 fence is inf."""
    import warnings as warnings_module

    path = csv_of(tmp_path, "v\n1e308\n1e308\n1e308\n1e308\n-1e308\n")
    with warnings_module.catch_warnings():
        warnings_module.simplefilter("error")
        [column] = detect_outliers_csv(path).column_results
    assert column.low_outliers == 1
    assert column.examples[0]["row"] == 6


def test_subnormal_and_negative_zero_values(tmp_path) -> None:
    [column] = scan_values(tmp_path, ["5e-324", "1e-323", "1.5e-323", "2e-323"]).column_results
    assert column.values == 4 and column.q1 is not None
    [column] = scan_values(tmp_path, ["-0.0", "0", "0", "0", "0.0", "5"]).column_results
    assert column.outliers == 1 and column.examples[0]["value"] == 5.0


def test_exponent_and_bare_point_notations_parse(tmp_path) -> None:
    cells = ["1E5", "+1e5", "1e+5", "10000e1", ".5", "5.", "100000", "9E99"]
    [column] = scan_values(tmp_path, cells).column_results
    assert column.values == 8 and column.invalid == 0
    assert column.high_outliers == 1  # 9E99


def test_frame_cells_that_are_not_strings_still_behave_as_text(tmp_path) -> None:
    # The fast path must not reclassify non-string objects: booleans read as text.
    frame = pd.DataFrame({"x": [1, 2.5, True, "7", None, 9]}, dtype=object)
    [column] = detect_outliers_frame(frame, columns=["x"]).column_results
    assert column.values == 4  # 1, 2.5, "7", 9
    assert column.invalid == 1  # True -> "True"
    assert column.missing == 1  # None


# --- regression: numeric placeholder tokens and the threshold message -----------------


@pytest.mark.parametrize("chunk_size", [1, 2, 5, 100_000])
def test_numeric_null_token_is_a_placeholder(tmp_path, chunk_size) -> None:
    """Used to be ignored: the parse-first fast path accepted -999 as a number, which
    then shifted the quartiles. Matching is on the stripped text, as for other tokens."""
    cells = ["1", "2", "3", "4", "5", "-999", " -999 ", "-999.0"]
    [column] = scan_values(
        tmp_path, cells, chunk_size=chunk_size, null_tokens=["-999"]
    ).column_results
    assert column.null_like == 2 and column.values == 6 and column.invalid == 0
    assert column.examples == [{"row": 9, "value": -999.0}]  # "-999.0" is not the token
    assert any("2 placeholder" in w for w in column.warnings)


def test_numeric_null_token_in_cli(tmp_path, capsys) -> None:
    from automation_toolkit.anomaly_cli import main

    path = csv_of(tmp_path, "x\n" + "".join(f"{i}\n" for i in range(1, 21)) + "-999\n")
    assert main(["--input", str(path), "--strict", "--null-token", "-999"]) == 0
    assert main(["--input", str(path), "--strict"]) == 2  # without it, -999 is an outlier


def test_skip_message_never_rounds_up_to_the_threshold(tmp_path) -> None:
    """Used to say "only 90% ... numeric" for a column skipped at 89.6%."""
    frame = pd.DataFrame({"x": [str(i) for i in range(896)] + ["t"] * 104})
    report = detect_outliers_frame(frame)
    assert report.column_results == []
    assert any("only 89.6% of its values" in w for w in report.warnings)
    frame = pd.DataFrame({"x": ["1", "a", "b"]})
    assert any("only 33% of its values" in w for w in detect_outliers_frame(frame).warnings)


# --- modified z-score method ------------------------------------------------------------


def test_modified_zscore_hand_computed(tmp_path) -> None:
    # median 6, MAD 3: the fence is 6 +/- 3.5 * 3 / 0.6745 and only 100 lies outside.
    cells = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100]
    report = scan_values(tmp_path, cells, method="modified_zscore")
    [column] = report.column_results
    assert (column.median, column.mad) == (6.0, 3.0)
    assert column.lower_bound == pytest.approx(6 - 3.5 * 3 / 0.6745, abs=1e-6)
    assert column.upper_bound == pytest.approx(6 + 3.5 * 3 / 0.6745, abs=1e-6)
    assert column.q1 is None and column.q3 is None and column.iqr is None
    assert column.outliers == 1 and column.examples == [{"row": 12, "value": 100.0}]
    data = report.to_dict()
    assert data["method"] == "modified_zscore"
    assert data["threshold"] == 3.5 and data["factor"] is None
    assert "modified z-score threshold 3.5" in report.to_text()


def test_iqr_stays_the_default_and_reports_no_median(tmp_path) -> None:
    report = scan_values(tmp_path, [1, 2, 3, 4, 5, 100])
    [column] = report.column_results
    assert column.median is None and column.mad is None and column.q1 is not None
    data = report.to_dict()
    assert data["method"] == "iqr" and data["factor"] == 1.5 and data["threshold"] is None


def test_modified_zscore_resists_heavy_contamination(tmp_path) -> None:
    # Two thirds extreme values swallow the quartile fence but not the median fence.
    cells = [str(v) for v in range(100)] + ["1e9"] * 66
    assert scan_values(tmp_path, cells).total_outliers == 0
    report = scan_values(tmp_path, cells, method="modified_zscore")
    assert report.total_outliers == 66


def test_modified_zscore_threshold_widens_the_fence(tmp_path) -> None:
    cells = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 100]
    assert scan_values(tmp_path, cells, method="modified_zscore").total_outliers == 1
    wide = scan_values(tmp_path, cells, method="modified_zscore", threshold=25)
    assert wide.total_outliers == 0
    tight = scan_values(tmp_path, cells, method="modified_zscore", threshold=0)
    [column] = tight.column_results
    assert (column.lower_bound, column.upper_bound) == (6.0, 6.0)
    assert column.outliers == 10  # everything except the median itself


def test_zero_mad_flags_deviants_with_a_warning(tmp_path) -> None:
    [column] = scan_values(tmp_path, [5, 5, 5, 5, 7], method="modified_zscore").column_results
    assert column.mad == 0 and column.outliers == 1
    assert any("median absolute deviation is 0" in w for w in column.warnings)


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 100_000])
def test_modified_zscore_is_chunk_size_independent(tmp_path, tiny_blocks, chunk_size) -> None:
    rng = np.random.default_rng(11)
    cells = [str(v) for v in rng.normal(50, 10, 300).round(3)]
    cells[50], cells[150], cells[250] = "", "NA", "bad"
    cells[200] = "999"
    expected = scan_values(tmp_path, cells, method="modified_zscore").to_dict()
    got = scan_values(tmp_path, cells, method="modified_zscore", chunk_size=chunk_size).to_dict()
    del expected["generated_at"], got["generated_at"]
    assert got == expected


def test_modified_zscore_excludes_missing_placeholder_invalid(tmp_path) -> None:
    cells = ["1", "2", "3", "4", "5", "", "NA", "bad", " 6 "]
    [column] = scan_values(tmp_path, cells, method="modified_zscore", columns=["x"]).column_results
    assert (column.values, column.missing, column.null_like, column.invalid) == (6, 1, 1, 1)


def test_modified_zscore_median_overflow_is_warned_not_crashing(tmp_path) -> None:
    # numpy averages the middle pair: (1e308 + 1.7e308) / 2 overflows to inf.
    report = scan_values(
        tmp_path, ["1e308", "1e308", "1.7e308", "1.7e308"], method="modified_zscore"
    )
    [column] = report.column_results
    assert column.median is None and column.outliers == 0
    assert any("beyond the float range" in w for w in column.warnings)
    json.dumps(report.to_dict())
    report.to_text()


def test_modified_zscore_too_few_values(tmp_path) -> None:
    [column] = scan_values(tmp_path, [1, 2, 3], method="modified_zscore").column_results
    assert column.median is None and column.outliers == 0
    assert any("a meaningful median and deviation" in w for w in column.warnings)


def test_unknown_method_and_bad_threshold_raise(tmp_path) -> None:
    with pytest.raises(ValueError, match="method must be one of: iqr, modified_zscore"):
        scan_values(tmp_path, [1, 2, 3, 4], method="zscore")
    for threshold in (-1, float("nan"), "big", True):
        with pytest.raises(ValueError, match="threshold"):
            scan_values(tmp_path, [1, 2, 3, 4], method="modified_zscore", threshold=threshold)


def test_modified_zscore_frame_api_and_duplicate_headers(tmp_path) -> None:
    frame = pd.DataFrame([["1", "10"], ["2", "20"], ["3", "30"], ["4", "40"], ["5", "9000"]])
    frame.columns = ["a", "a"]
    report = detect_outliers_frame(frame, method="modified_zscore")
    by_name = {c.name: c for c in report.column_results}
    assert by_name["a"].outliers == 0 and by_name["a_2"].outliers == 1
    assert by_name["a_2"].examples == [{"row": 5, "value": 9000.0}]


def test_modified_zscore_header_only_dataset(tmp_path) -> None:
    report = detect_outliers_csv(csv_of(tmp_path, "x\n"), method="modified_zscore", columns=["x"])
    assert report.rows == 0 and report.to_dict()["method"] == "modified_zscore"
    [column] = report.column_results
    assert any("no numeric values" in w for w in column.warnings)
