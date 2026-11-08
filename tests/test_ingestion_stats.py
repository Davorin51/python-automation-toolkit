"""Ingestion statistics: whole-file, chunked and resumed reads must agree on what was read."""

import json
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook
from test_checkpoint import (
    checkpoint_folder,
    interrupt_after,
    interrupt_before_completion,
    write_source,
)

from automation_toolkit.cli import main
from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.ingestion import (
    FileStats,
    IngestionStats,
    load_files,
    load_files_with_stats,
)
from automation_toolkit.ingestion.checkpoint import MANIFEST
from automation_toolkit.processing.quality import build_quality_report
from automation_toolkit.processing.validation import validate
from automation_toolkit.reporting.quality_report import FILENAME

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def source(tmp_path):
    # 10 data records (one with a quoted line break), one empty line and one
    # whitespace-only record: 12 raw records, 2 blank, 10 data rows.
    return write_source(tmp_path / "sales.csv")


def only(stats: IngestionStats) -> FileStats:
    assert len(stats.files) == 1
    return stats.files[0]


def assert_invariants(stats: IngestionStats) -> None:
    for f in stats.files:
        assert f.records_read - f.blank_rows == f.data_rows
        assert f.records_restored <= f.records_read
        assert f.resumed_from_checkpoint == (f.chunks_restored > 0)
        assert not f.resumed_from_checkpoint or f.checkpointed
        assert f.chunks_processed >= 0 and f.chunks_restored >= 0
    assert stats.records_read == sum(f.records_read for f in stats.files)
    assert stats.data_rows == sum(f.data_rows for f in stats.files)


# --- load_files stays identical ---------------------------------------------------


@pytest.mark.parametrize("chunk_size", [None, 1, 3, 100])
def test_load_files_returns_the_same_frame_as_the_stats_variant(source, chunk_size):
    config = Config(csv_chunk_size=chunk_size)
    frame, stats = load_files_with_stats([source], config)
    pd.testing.assert_frame_equal(load_files([source], config), frame)
    assert_invariants(stats)


# --- Whole-file, chunked and Excel reads --------------------------------------------


def test_whole_file_csv_stats(source):
    stats = only(load_files_with_stats([source], Config())[1])
    assert stats.source_file == str(source)
    assert stats.file_name == "sales.csv"
    assert stats.file_format == "csv"
    assert (stats.records_read, stats.blank_rows, stats.data_rows) == (12, 2, 10)
    assert stats.chunked is False
    assert stats.chunks_processed == 1
    assert (stats.chunks_restored, stats.records_restored) == (0, 0)
    assert stats.checkpointed is False
    assert stats.resumed_from_checkpoint is False


@pytest.mark.parametrize(("chunk_size", "chunks"), [(1, 12), (3, 4), (5, 3), (12, 1), (100, 1)])
def test_chunked_csv_stats_match_whole_file_except_chunk_count(source, chunk_size, chunks):
    whole = only(load_files_with_stats([source], Config())[1])
    chunked = only(load_files_with_stats([source], Config(csv_chunk_size=chunk_size))[1])
    assert chunked.chunked is True
    assert chunked.chunks_processed == chunks
    assert (chunked.records_read, chunked.blank_rows, chunked.data_rows) == (
        whole.records_read,
        whole.blank_rows,
        whole.data_rows,
    )
    assert chunked.checkpointed is False and chunked.resumed_from_checkpoint is False


def test_blank_rows_exactly_on_chunk_boundaries(tmp_path):
    path = tmp_path / "sales.csv"
    rows = ["date,product,category,quantity,unit_price"]
    rows += ["2026-01-05,Laptop,Electronics,2,900", "", "", "2026-01-06,Mouse,Electronics,1,20", ""]
    path.write_text("\n".join(rows) + "\n")
    for size in (None, 1, 2, 3):
        stats = only(load_files_with_stats([path], Config(csv_chunk_size=size))[1])
        assert (stats.records_read, stats.blank_rows, stats.data_rows) == (5, 3, 2), size


def test_excel_stats_are_never_chunked(tmp_path):
    path = tmp_path / "sales.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Product", "Category", "Quantity", "Unit Price"])
    ws.append(["2026-01-05", "Laptop", "Electronics", 2, 900])
    ws.append([None, None, None, None, None])
    ws.append(["2026-01-06", "Mouse", "Electronics", 1, 20])
    wb.save(path)
    config = Config(csv_chunk_size=1, csv_checkpoint_dir=str(tmp_path / "ckpt"))
    stats = only(load_files_with_stats([path], config)[1])
    assert stats.file_format == "xlsx"
    assert (stats.records_read, stats.blank_rows, stats.data_rows) == (3, 1, 2)
    assert stats.chunked is False
    assert stats.chunks_processed == 1
    assert stats.checkpointed is False
    assert not (tmp_path / "ckpt").exists()


def test_multiple_files_keep_input_order_and_names(source, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    relative = Path("sales.csv")
    other = tmp_path / "other.csv"
    other.write_text(
        "date,product,category,quantity,unit_price,order_id\n2026-01-05,Laptop,E,2,900,X1\n\n"
    )
    frame, stats = load_files_with_stats([other, relative], Config(csv_chunk_size=4))
    assert [f.source_file for f in stats.files] == [str(other), "sales.csv"]
    assert [f.file_name for f in stats.files] == ["other.csv", "sales.csv"]
    assert set(stats.by_source()) == set(frame["source_file"])
    assert [f.chunks_processed for f in stats.files] == [1, 3]
    assert (stats.records_read, stats.blank_rows, stats.data_rows) == (14, 3, 11)
    assert stats.chunked_files == 2
    assert (stats.checkpointed_files, stats.resumed_files) == (0, 0)
    assert_invariants(stats)


def test_errors_still_raise_before_any_statistics(tmp_path):
    header_only = tmp_path / "empty.csv"
    header_only.write_text("date,product,category,quantity,unit_price\n")
    with pytest.raises(IngestionError, match="no data rows"):
        load_files_with_stats([header_only], Config())
    with pytest.raises(IngestionError, match="no data rows"):
        load_files_with_stats([header_only], Config(csv_chunk_size=2))
    with pytest.raises(IngestionError, match="at least one"):
        load_files_with_stats([], Config())


# --- Checkpoint and resume ------------------------------------------------------------


@pytest.fixture
def ckpt_config(tmp_path):
    return Config(csv_chunk_size=3, csv_checkpoint_dir=str(tmp_path / "ckpt"))


def test_fresh_checkpointed_run_is_not_a_resume(source, ckpt_config, tmp_path):
    stats = only(load_files_with_stats([source], ckpt_config)[1])
    assert stats.checkpointed is True
    assert stats.resumed_from_checkpoint is False
    assert (stats.chunks_processed, stats.chunks_restored, stats.records_restored) == (4, 0, 0)
    assert (stats.records_read, stats.blank_rows, stats.data_rows) == (12, 2, 10)
    assert not list((tmp_path / "ckpt").iterdir())


@pytest.mark.parametrize("done", [1, 2, 3])
def test_resumed_run_reports_restored_and_fresh_chunks(
    source, ckpt_config, tmp_path, monkeypatch, done
):
    expected_frame, expected = load_files_with_stats([source], Config())
    interrupt_after(monkeypatch, done)
    with pytest.raises(KeyboardInterrupt):
        load_files_with_stats([source], ckpt_config)
    manifest = json.loads((checkpoint_folder(tmp_path) / MANIFEST).read_text())
    assert len(manifest["chunks"]) == done
    monkeypatch.undo()
    frame, stats = load_files_with_stats([source], ckpt_config)
    pd.testing.assert_frame_equal(frame, expected_frame)
    resumed = only(stats)
    assert resumed.resumed_from_checkpoint is True
    assert resumed.checkpointed is True
    assert resumed.chunks_restored == done
    assert resumed.records_restored == sum(c["records"] for c in manifest["chunks"]) == 3 * done
    assert resumed.chunks_processed == 4 - done
    assert resumed.chunks_restored + resumed.chunks_processed == 4
    assert (resumed.records_read, resumed.blank_rows, resumed.data_rows) == (
        only(expected).records_read,
        only(expected).blank_rows,
        only(expected).data_rows,
    )
    assert stats.resumed_files == 1
    assert_invariants(stats)
    assert not list((tmp_path / "ckpt").iterdir())


def test_fully_checkpointed_file_reads_no_records(source, ckpt_config, tmp_path, monkeypatch):
    interrupt_before_completion(monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        load_files_with_stats([source], ckpt_config)
    monkeypatch.undo()
    stats = only(load_files_with_stats([source], ckpt_config)[1])
    assert (stats.chunks_restored, stats.chunks_processed) == (4, 0)
    assert stats.records_restored == stats.records_read == 12
    assert (stats.blank_rows, stats.data_rows) == (2, 10)
    assert stats.resumed_from_checkpoint is True


def test_discarded_checkpoint_counts_as_a_fresh_read(source, ckpt_config, tmp_path, monkeypatch):
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files_with_stats([source], ckpt_config)
    monkeypatch.undo()
    folder = checkpoint_folder(tmp_path)
    (folder / "chunk-000001.json").write_text("{corrupt")
    stats = only(load_files_with_stats([source], ckpt_config)[1])
    assert stats.resumed_from_checkpoint is False
    assert (stats.chunks_restored, stats.records_restored, stats.chunks_processed) == (0, 0, 4)
    assert (stats.records_read, stats.blank_rows, stats.data_rows) == (12, 2, 10)


# --- Dataclass rendering ------------------------------------------------------------


def test_stats_to_dict_keys_and_empty_totals(source):
    stats = load_files_with_stats([source], Config(csv_chunk_size=5))[1]
    payload = stats.to_dict()
    assert payload == {
        "files": 1,
        "records_read": 12,
        "blank_rows_skipped": 2,
        "data_rows": 10,
        "chunked_files": 1,
        "chunks_processed": 3,
        "chunks_restored": 0,
        "checkpointed_files": 0,
        "files_resumed_from_checkpoint": 0,
    }
    file_payload = stats.files[0].to_dict()
    assert list(file_payload) == [
        "source_file",
        "file_name",
        "format",
        "records_read",
        "blank_rows",
        "data_rows",
        "chunked",
        "chunks_processed",
        "chunks_restored",
        "records_restored",
        "checkpointed",
        "resumed_from_checkpoint",
    ]
    assert file_payload["format"] == "csv" and file_payload["resumed_from_checkpoint"] is False
    empty = IngestionStats([])
    assert empty.to_dict()["records_read"] == 0 and empty.resumed_files == 0
    assert empty.by_source() == {}


# --- Quality report integration -----------------------------------------------------


def test_quality_report_includes_ingestion_statistics(source):
    config = Config(csv_chunk_size=3)
    frame, stats = load_files_with_stats([source], config)
    report = build_quality_report(validate(frame, config), config, ingestion=stats)
    assert report.ingestion is stats
    payload = report.to_dict()
    assert payload["ingestion"] == stats.to_dict()
    assert payload["ingestion"]["blank_rows_skipped"] == 2
    assert payload["summary"]["input_rows"] == payload["ingestion"]["data_rows"] == 10
    file_entry = payload["by_file"][0]
    assert file_entry["input_rows"] == file_entry["ingestion"]["data_rows"]
    assert file_entry["ingestion"]["chunks_processed"] == 4
    assert report.files[0].ingestion is stats.files[0]


def test_quality_report_without_statistics_is_unchanged(source):
    config = Config()
    frame, stats = load_files_with_stats([source], config)
    validation = validate(frame, config)
    without = build_quality_report(validation, config).to_dict()
    with_stats = build_quality_report(validation, config, ingestion=stats).to_dict()
    assert without["ingestion"] is None
    assert without["by_file"][0]["ingestion"] is None
    without.pop("ingestion"), with_stats.pop("ingestion")
    for entry in without["by_file"] + with_stats["by_file"]:
        entry.pop("ingestion")
    assert without == with_stats


def test_quality_report_rejects_statistics_for_unknown_files(source, tmp_path):
    config = Config()
    frame, stats = load_files_with_stats([source], config)
    other = tmp_path / "other.csv"
    other.write_text("date,product,category,quantity,unit_price\n2026-01-05,Laptop,E,2,900\n")
    _, other_stats = load_files_with_stats([other], config)
    with pytest.raises(ValueError, match="other.csv"):
        build_quality_report(validate(frame, config), config, ingestion=other_stats)


def test_partial_statistics_leave_other_files_without_ingestion(source, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text(
        "date,product,category,quantity,unit_price,order_id\n2026-01-05,Laptop,E,2,900,X1\n"
    )
    config = Config()
    frame, stats = load_files_with_stats([source, other], config)
    partial = IngestionStats([stats.files[0]])
    report = build_quality_report(validate(frame, config), config, ingestion=partial)
    assert report.files[0].ingestion is stats.files[0]
    assert report.files[1].ingestion is None


@pytest.mark.parametrize("chunk_size", [None, 1, 50, 10_000])
def test_exported_quality_report_is_consistent_across_read_modes(chunk_size):
    config = Config(csv_chunk_size=chunk_size)
    frame, stats = load_files_with_stats([EXAMPLES / "sample_sales_dirty.csv"], config)
    payload = build_quality_report(validate(frame, config), config, ingestion=stats).to_dict()
    assert payload["ingestion"]["records_read"] == 357
    assert payload["ingestion"]["blank_rows_skipped"] == 0
    assert payload["summary"]["input_rows"] == 357
    expected_chunks = 1 if chunk_size is None else -(-357 // chunk_size)
    assert payload["ingestion"]["chunks_processed"] == expected_chunks
    assert payload["by_file"][0]["ingestion"]["chunked"] is (chunk_size is not None)


def test_cli_quality_summary_mentions_blank_rows_and_resume(source, tmp_path, monkeypatch, capsys):
    output = tmp_path / "reports"
    ckpt = tmp_path / "ckpt"
    args = ["--input", str(source), "--output", str(output), "--format", "excel"]
    args += ["--chunk-size", "3", "--checkpoint-dir", str(ckpt), "--quality-report"]
    interrupt_after(monkeypatch, 2, error=RuntimeError)
    with pytest.raises(RuntimeError):
        main(args)
    monkeypatch.undo()
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "; 2 blank row(s) skipped; 1 file(s) resumed from checkpoint" in out
    exported = json.loads(next(output.rglob(FILENAME)).read_text(encoding="utf-8"))
    assert exported["ingestion"]["files_resumed_from_checkpoint"] == 1
    assert exported["ingestion"]["chunks_restored"] == 2
    assert exported["ingestion"]["chunks_processed"] == 2
    assert exported["by_file"][0]["ingestion"]["resumed_from_checkpoint"] is True
    assert exported["by_file"][0]["ingestion"]["records_restored"] == 6
    assert exported["summary"]["input_rows"] == 10
    assert not list(ckpt.iterdir())
    # A clean, uninterrupted run says nothing about blanks or checkpoints.
    clean = tmp_path / "clean.csv"
    clean.write_text("date,product,category,quantity,unit_price\n2026-01-05,Laptop,E,2,900\n")
    assert main(["--input", str(clean), "--output", str(output), "--quality-report"]) == 0
    assert "blank row" not in capsys.readouterr().out
