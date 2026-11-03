"""Incremental (chunked) CSV ingestion must match whole-file reads exactly."""

import json
from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.cli import main
from automation_toolkit.config import Config
from automation_toolkit.exceptions import ConfigurationError, IngestionError
from automation_toolkit.ingestion import REQUIRED, iter_csv_chunks, load_files, read_csv
from automation_toolkit.processing.validation import validate

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
HEADER = "date,product,category,quantity,unit_price\n"


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 100, 356, 357, 358, 10_000])
def test_chunked_read_matches_whole_file(chunk_size):
    path = EXAMPLES / "sample_sales_dirty.csv"
    whole = read_csv(path, Config())
    chunked = read_csv(path, Config(csv_chunk_size=chunk_size))
    pd.testing.assert_frame_equal(chunked, whole)
    assert whole.index.equals(chunked.index)


@pytest.mark.parametrize("chunk_size", [1, 50, 10_000])
def test_chunked_validation_reproduces_control_totals(chunk_size):
    expected = json.loads((EXAMPLES / "expected_results.json").read_text())
    for name in ("sample_sales.csv", "sample_sales_dirty.csv"):
        config = Config(csv_chunk_size=chunk_size)
        result = validate(load_files([EXAMPLES / name], config), config)
        assert result.transactions.revenue_cents.sum() == expected["total_revenue_cents"]
        assert len(result.transactions) == expected["accepted_rows"]
        assert result.transactions.order_id.nunique() == expected["orders"]


def test_iter_csv_chunks_sizes_columns_and_running_index(tmp_path):
    path = tmp_path / "sales.csv"
    rows = [f"2026-01-{day:02d},P{day},C,{day},1.50" for day in range(1, 8)]
    path.write_text(HEADER + "\n".join(rows) + "\n")
    chunks = list(iter_csv_chunks(path, Config(), chunk_size=3))
    assert [len(c) for c in chunks] == [3, 3, 1]
    assert all(list(c.columns) == HEADER.strip().split(",") for c in chunks)
    assert [list(c.index) for c in chunks] == [[0, 1, 2], [3, 4, 5], [6]]
    assert chunks[1]["product"].tolist() == ["P4", "P5", "P6"]
    assert chunks[2]["unit_price"].iloc[0] == "1.50"
    assert all(c.dtypes.equals(read_csv(path, Config()).dtypes) for c in chunks)
    # Without a size anywhere, the whole file is one chunk; the config default is honored too.
    assert [len(c) for c in iter_csv_chunks(path, Config())] == [7]
    assert [len(c) for c in iter_csv_chunks(path, Config(csv_chunk_size=4))] == [4, 3]
    assert [len(c) for c in iter_csv_chunks(path, Config(csv_chunk_size=4), chunk_size=7)] == [7]


def test_source_rows_blank_lines_and_quoted_newlines_across_chunk_boundaries(tmp_path):
    path = tmp_path / "boundary.csv"
    path.write_text(
        HEADER
        + "2026-01-05,Laptop,Electronics,2,900\n"
        + "\n"
        + '2026-01-06,"Desk\nwith shelf",Furniture,1,300\n'
        + " ,,,,\n"
        + "2026-01-07,Mouse,Electronics,3,20\n"
        + "\n",
        encoding="utf-8",
    )
    whole = load_files([path], Config())
    for chunk_size in (1, 2, 3, 4):
        config = Config(csv_chunk_size=chunk_size)
        with_chunks = load_files([path], config)
        pd.testing.assert_frame_equal(with_chunks, whole)
        assert with_chunks.source_row.tolist() == [2, 4, 6]
        assert with_chunks["product"].iloc[1] == "Desk\nwith shelf"
        assert validate(with_chunks, config).invalid_rows == 0


def test_header_only_file_yields_one_empty_chunk_and_no_data_rows_error(tmp_path):
    path = tmp_path / "header.csv"
    path.write_text(HEADER)
    chunks = list(iter_csv_chunks(path, Config(), chunk_size=10))
    assert len(chunks) == 1 and chunks[0].empty
    assert list(chunks[0].columns) == HEADER.strip().split(",")
    pd.testing.assert_frame_equal(
        read_csv(path, Config(csv_chunk_size=10)), read_csv(path, Config())
    )
    with pytest.raises(IngestionError, match="no data rows"):
        load_files([path], Config(csv_chunk_size=10))


def test_empty_file_and_missing_columns_are_reported_in_chunk_mode(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    with pytest.raises(IngestionError, match="Cannot read CSV empty.csv"):
        list(iter_csv_chunks(path, Config(), chunk_size=5))
    with pytest.raises(IngestionError, match="Cannot read CSV empty.csv"):
        load_files([path], Config(csv_chunk_size=5))
    path = tmp_path / "missing.csv"
    path.write_text("Product,Quantity\nLaptop,2\nMouse,1\n")
    with pytest.raises(IngestionError, match="missing.csv: Missing required columns"):
        load_files([path], Config(csv_chunk_size=1))
    path = tmp_path / "ambiguous.csv"
    path.write_text("Date,date,product,category,quantity,unit_price\n2026-01-05,x,P,C,1,2\n")
    with pytest.raises(IngestionError, match="Ambiguous"):
        load_files([path], Config(csv_chunk_size=1))


def test_parser_error_in_a_later_chunk_is_an_ingestion_error(tmp_path):
    path = tmp_path / "ragged.csv"
    good = "2026-01-05,Laptop,Electronics,2,900\n"
    path.write_text(HEADER + good * 5 + "2026-01-06,Mouse,Electronics,1,20,extra,fields\n")
    chunks = iter_csv_chunks(path, Config(), chunk_size=2)
    assert len(next(chunks)) == 2
    assert len(next(chunks)) == 2
    with pytest.raises(IngestionError, match=r"Cannot read CSV ragged.csv: .*line 7"):
        next(chunks)
    with pytest.raises(IngestionError, match="ragged.csv"):
        load_files([path], Config(csv_chunk_size=2))
    # The whole-file reader rejects the same file, so both modes agree.
    with pytest.raises(IngestionError, match="ragged.csv"):
        load_files([path], Config())


def test_decoding_error_in_a_later_chunk_is_an_ingestion_error(tmp_path):
    path = tmp_path / "latin.csv"
    good = "2026-01-05,Laptop,Electronics,2,900\n"
    path.write_bytes((HEADER + good * 3).encode("utf-8") + b"2026-01-06,Caf\xe9,Food,1,3\n")
    with pytest.raises(IngestionError, match="Cannot read CSV latin.csv"):
        load_files([path], Config(csv_chunk_size=2))
    with pytest.raises(IngestionError, match="Cannot read CSV latin.csv"):
        load_files([path], Config(csv_encoding="no-such-encoding", csv_chunk_size=2))
    data = load_files([path], Config(csv_encoding="latin-1", csv_chunk_size=2))
    assert data["product"].tolist() == ["Laptop"] * 3 + ["Café"]


def test_delimiter_and_literal_na_survive_chunking(tmp_path):
    path = tmp_path / "semicolon.csv"
    path.write_text("date;product;category;quantity;unit_price\n2026-01-01;NA;C;1;0.10\n")
    data = load_files([path], Config(csv_delimiter=";", csv_chunk_size=1))
    assert data["product"].iloc[0] == "NA"
    assert data["unit_price"].iloc[0] == "0.10"


@pytest.mark.parametrize("chunk_size", [0, -1, True, "5", 2.0])
def test_invalid_chunk_size_override_is_rejected_before_opening_the_file(tmp_path, chunk_size):
    path = tmp_path / "missing.csv"
    with pytest.raises(ConfigurationError, match="csv_chunk_size"):
        list(iter_csv_chunks(path, Config(), chunk_size=chunk_size))
    assert not path.exists()


def test_chunked_reader_closes_file_when_consumer_stops_early(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text(HEADER + "2026-01-05,Laptop,Electronics,2,900\n" * 10)
    chunks = iter_csv_chunks(path, Config(), chunk_size=3)
    next(chunks)
    chunks.close()
    # Renaming would fail on platforms that lock open files; on all platforms the generator
    # must be finished and the file must still be readable afterwards.
    assert len(read_csv(path, Config(csv_chunk_size=3))) == 10


def test_chunk_mode_logs_a_summary(tmp_path, caplog):
    path = tmp_path / "sales.csv"
    path.write_text(HEADER + "2026-01-05,Laptop,Electronics,2,900\n" * 5)
    with caplog.at_level("INFO"):
        read_csv(path, Config(csv_chunk_size=2))
    assert "Read 5 rows from sales.csv in 3 chunks of up to 2" in caplog.text


def test_excel_inputs_ignore_chunk_size(tmp_path):
    whole = load_files([EXAMPLES / "sample_sales.xlsx"], Config())
    chunked = load_files([EXAMPLES / "sample_sales.xlsx"], Config(csv_chunk_size=5))
    pd.testing.assert_frame_equal(chunked, whole)


def test_cli_chunk_size_flag_and_config_key(sales_file, tmp_path, capsys):
    output = tmp_path / "reports"
    args = ["--input", str(sales_file), "--output", str(output), "--format", "excel"]
    assert main(args + ["--chunk-size", "2"]) == 0
    assert "2,950.00" in capsys.readouterr().out
    config = tmp_path / "config.yaml"
    config.write_text("csv_chunk_size: 1\n")
    assert main(args + ["--config", str(config)]) == 0
    assert "2,950.00" in capsys.readouterr().out
    # An invalid size is a configuration error (exit 1), not a crash; argparse rejects text (2).
    assert main(args + ["--chunk-size", "0"]) == 1
    with pytest.raises(SystemExit) as info:
        main(args + ["--chunk-size", "ten"])
    assert info.value.code == 2
    assert len([p for p in output.iterdir() if p.name.startswith("run-")]) == 2


def _write_large_csv(path, rows, extra_width=0):
    pad = "," + ",".join(f"ignored free-text note {i:04d}" for i in range(extra_width))
    pad = pad if extra_width else ""
    extra_headers = "," + ",".join(f"Extra {i}" for i in range(extra_width)) if extra_width else ""
    with path.open("w", encoding="utf-8") as stream:
        stream.write("Date,Product,Category,Quantity,Unit Price,Order ID" + extra_headers + "\n")
        for i in range(rows):
            stream.write(
                f"2026-{i % 12 + 1:02d}-{i % 28 + 1:02d},Product {i % 97},Cat {i % 7},"
                f"{i % 9 + 1},{(i % 5000) / 100 + 1:.2f},ORD-{i:07d}{pad}\n"
            )


def test_large_file_chunked_load_matches_whole_file(tmp_path):
    path = tmp_path / "large.csv"
    _write_large_csv(path, 120_000)
    whole = load_files([path], Config())
    chunked = load_files([path], Config(csv_chunk_size=9_973))
    pd.testing.assert_frame_equal(chunked, whole)
    assert len(chunked) == 120_000
    assert chunked.source_row.iloc[-1] == 120_001
    result = validate(chunked, Config(csv_chunk_size=9_973))
    assert result.invalid_rows == 0
    assert len(result.transactions) == 120_000


def test_chunked_load_uses_less_peak_memory_than_whole_file(tmp_path):
    import gc
    import tracemalloc

    path = tmp_path / "wide.csv"
    # The dropped extra columns dominate the raw text, so the whole-file read (full raw
    # table plus its normalization copy) must peak far above the chunked read, which only
    # accumulates the required fields and holds one raw chunk at a time.
    _write_large_csv(path, 20_000, extra_width=40)

    def peak(config):
        gc.collect()
        tracemalloc.start()
        frame = load_files([path], config)
        _, high = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return frame, high

    whole, whole_peak = peak(Config())
    chunked, chunked_peak = peak(Config(csv_chunk_size=2_000))
    pd.testing.assert_frame_equal(chunked, whole)
    # Generous margin: the raw table alone is several times the final required-field table.
    assert chunked_peak < whole_peak * 0.6, (chunked_peak, whole_peak)


def test_blank_rows_exactly_on_chunk_boundaries(tmp_path):
    path = tmp_path / "edges.csv"
    data = "2026-01-05,Laptop,Electronics,2,900\n"
    # Records 2-9: blanks land on the last row of chunk 1 and the first row of chunk 3.
    path.write_text(HEADER + data + " ,,,,\n" + data + data + "\n" + data + data + "\n")
    whole = load_files([path], Config())
    for chunk_size in (2, 3, 4):
        chunked = load_files([path], Config(csv_chunk_size=chunk_size))
        pd.testing.assert_frame_equal(chunked, whole)
        assert chunked.source_row.tolist() == [2, 4, 5, 7, 8]


def test_file_of_only_blank_rows_errors_in_chunk_mode(tmp_path):
    path = tmp_path / "blank.csv"
    path.write_text(HEADER + "\n,,,,\n   ,,,,\n\n")
    with pytest.raises(IngestionError, match="no data rows"):
        load_files([path], Config(csv_chunk_size=2))


def test_ragged_row_exactly_at_chunk_boundary(tmp_path):
    good = "2026-01-05,Laptop,Electronics,2,900\n"
    for position in (4, 5):
        path = tmp_path / f"ragged{position}.csv"
        rows = [good] * 6
        rows[position - 2] = "2026-01-06,Mouse,Electronics,1,20,extra,fields\n"
        path.write_text(HEADER + "".join(rows))
        with pytest.raises(IngestionError, match=rf"ragged{position}.csv: .*line {position}"):
            load_files([path], Config(csv_chunk_size=3))


def test_extra_columns_are_dropped_per_chunk(tmp_path):
    path = tmp_path / "extra.csv"
    path.write_text(
        "date,product,category,quantity,unit_price,comment\n"
        + "2026-01-05,Laptop,Electronics,2,900,long note one\n"
        + "2026-01-06,Mouse,Electronics,1,20,long note two\n"
        + "2026-01-07,Desk,Furniture,1,300,long note three\n"
    )
    data = load_files([path], Config(csv_chunk_size=2))
    assert list(data.columns) == REQUIRED + ["source_file", "source_row"]
    pd.testing.assert_frame_equal(data, load_files([path], Config()))


def test_multi_file_order_id_mismatch_still_detected_in_chunk_mode(sales_file, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text(HEADER + "2026-01-01,P,C,1,2\n")
    with pytest.raises(IngestionError, match="every input"):
        load_files([sales_file, other], Config(csv_chunk_size=1))


def test_read_csv_reports_unreadable_files_in_both_modes(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    for config in (Config(), Config(csv_chunk_size=3)):
        with pytest.raises(IngestionError, match="Cannot read CSV empty.csv"):
            read_csv(path, config)
    with pytest.raises(IngestionError, match="Cannot read CSV"):
        read_csv(tmp_path / "missing.csv", Config())
