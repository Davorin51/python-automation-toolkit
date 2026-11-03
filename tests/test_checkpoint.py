"""Checkpoint and resume for chunked CSV loading: interruption, recovery and corruption."""

import json
import logging
import os
from pathlib import Path

import pandas as pd
import pytest

import automation_toolkit.ingestion as ingestion
from automation_toolkit.cli import main
from automation_toolkit.config import Config
from automation_toolkit.exceptions import ConfigurationError, IngestionError
from automation_toolkit.ingestion import iter_csv_chunks, load_files
from automation_toolkit.ingestion.checkpoint import MANIFEST, CsvCheckpoint
from automation_toolkit.processing.validation import validate

HEADER = "Date,Product,Category,Quantity,Unit Price,Order ID,Comment\n"
ROW = "2026-01-{day:02d},Product {day},Cat {cat},{qty},{price},ORD-{day:04d},note {day}\n"


def write_source(path: Path, rows: int = 10) -> Path:
    lines = [HEADER]
    for day in range(1, rows + 1):
        if day == 4:
            lines.append("\n")  # blank record inside the file
        if day == 6:
            lines.append('2026-01-06,"Desk\nwith shelf",Cat 1,1,300.00,ORD-0006,quoted\n')
            continue
        lines.append(ROW.format(day=day, cat=day % 3, qty=day % 5 + 1, price=f"{day * 1.5:.2f}"))
    lines.append(" ,,,,,,\n")
    path.write_text("".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def source(tmp_path):
    return write_source(tmp_path / "sales.csv")


@pytest.fixture
def config(tmp_path):
    return Config(csv_chunk_size=3, csv_checkpoint_dir=str(tmp_path / "ckpt"))


def checkpoint_folder(tmp_path: Path) -> Path:
    folders = list((tmp_path / "ckpt").iterdir())
    assert len(folders) == 1
    return folders[0]


def interrupt_after(monkeypatch, chunks_before_failure: int, error=KeyboardInterrupt):
    """Make load_files fail once ``chunks_before_failure`` chunks (across files) are processed.

    The failure is raised when the next chunk is requested, i.e. after the previous chunk was
    fully processed and checkpointed, which is where a real interruption leaves durable state.
    """
    real = ingestion.iter_csv_chunks
    processed = 0

    def failing(path, config, chunk_size=None, skip_records=0):
        nonlocal processed
        for chunk in real(path, config, chunk_size, skip_records):
            if processed >= chunks_before_failure:
                raise error("simulated interruption")
            processed += 1
            yield chunk

    monkeypatch.setattr(ingestion, "iter_csv_chunks", failing)


def interrupt_before_completion(monkeypatch, error=KeyboardInterrupt):
    """Crash after every chunk was checkpointed but before the checkpoint is removed."""

    def fail(self):
        raise error("simulated interruption")

    monkeypatch.setattr(ingestion.CsvCheckpoint, "complete", fail)


def count_chunks(monkeypatch):
    """Count the chunks the resumed run actually parses and processes."""
    real = ingestion.iter_csv_chunks
    seen: list[int] = []

    def counting(path, config, chunk_size=None, skip_records=0):
        for chunk in real(path, config, chunk_size, skip_records):
            seen.append(len(chunk))
            yield chunk

    monkeypatch.setattr(ingestion, "iter_csv_chunks", counting)
    return seen


def test_interrupted_load_leaves_checkpoint_and_resume_matches_whole_file(
    source, config, tmp_path, monkeypatch, caplog
):
    expected = load_files([source], Config())
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    folder = checkpoint_folder(tmp_path)
    manifest = json.loads((folder / MANIFEST).read_text())
    assert [c["records"] for c in manifest["chunks"]] == [3, 3]
    assert sorted(p.name for p in folder.iterdir()) == [
        MANIFEST,
        "chunk-000001.json",
        "chunk-000002.json",
    ]
    monkeypatch.undo()
    seen = count_chunks(monkeypatch)
    with caplog.at_level(logging.INFO):
        resumed = load_files([source], config)
    # 12 raw records (10 rows + 2 blanks + 1 quoted record... counted as one) minus 6 done.
    assert seen == [3, 3]
    assert "Resuming sales.csv from checkpoint: 2 chunks (6 records)" in caplog.text
    assert "Skipped 2 blank rows in sales.csv" in caplog.text
    pd.testing.assert_frame_equal(resumed, expected)
    assert resumed.source_row.tolist() == expected.source_row.tolist()
    assert resumed["product"].tolist()[5] == "Desk\nwith shelf"
    assert not (tmp_path / "ckpt").exists() or not list((tmp_path / "ckpt").iterdir())
    assert validate(resumed, config).invalid_rows == 0


@pytest.mark.parametrize("done", [1, 2, 3])
def test_resume_from_every_chunk_boundary(source, config, tmp_path, monkeypatch, done):
    expected = load_files([source], Config())
    interrupt_after(monkeypatch, done)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    seen = count_chunks(monkeypatch)
    resumed = load_files([source], config)
    pd.testing.assert_frame_equal(resumed, expected)
    assert sum(seen) == 12 - 3 * done
    assert not list((tmp_path / "ckpt").iterdir())


def test_interrupted_twice_then_completes(source, config, tmp_path, monkeypatch):
    expected = load_files([source], Config())
    interrupt_after(monkeypatch, 1)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    interrupt_after(monkeypatch, 1)  # one more chunk, then interrupted again
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    manifest = json.loads((checkpoint_folder(tmp_path) / MANIFEST).read_text())
    assert len(manifest["chunks"]) == 2
    monkeypatch.undo()
    pd.testing.assert_frame_equal(load_files([source], config), expected)


def test_fully_checkpointed_file_resumes_without_reading_records(
    source, config, tmp_path, monkeypatch
):
    expected = load_files([source], Config())
    interrupt_before_completion(monkeypatch)  # all four chunks saved, crash before cleanup
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    assert len(json.loads((checkpoint_folder(tmp_path) / MANIFEST).read_text())["chunks"]) == 4
    monkeypatch.undo()
    seen = count_chunks(monkeypatch)
    pd.testing.assert_frame_equal(load_files([source], config), expected)
    assert seen == []


def test_large_file_interrupted_midway_resumes_exactly(tmp_path, monkeypatch):
    path = tmp_path / "large.csv"
    with path.open("w", encoding="utf-8") as stream:
        stream.write(HEADER)
        for i in range(20_000):
            stream.write(
                f"2026-{i % 12 + 1:02d}-{i % 28 + 1:02d},P{i % 50},C{i % 4},{i % 7 + 1},"
                f"{(i % 900) / 100 + 1:.2f},ORD-{i:06d},{'x' * (i % 30)}\n"
            )
    config = Config(csv_chunk_size=1_000, csv_checkpoint_dir=str(tmp_path / "ckpt"))
    expected = load_files([path], Config())
    interrupt_after(monkeypatch, 7)
    with pytest.raises(KeyboardInterrupt):
        load_files([path], config)
    monkeypatch.undo()
    seen = count_chunks(monkeypatch)
    resumed = load_files([path], config)
    assert len(seen) == 13 and sum(seen) == 13_000
    pd.testing.assert_frame_equal(resumed, expected)
    assert validate(resumed, config).invalid_rows == 0


def test_malformed_row_fails_at_same_line_after_resume_and_fixed_file_starts_fresh(
    tmp_path, monkeypatch, caplog
):
    path = write_source(tmp_path / "ragged.csv")
    text = path.read_text()
    path.write_text(text.replace("ORD-0009,note 9\n", "ORD-0009,note 9,extra,fields\n"))
    config = Config(csv_chunk_size=3, csv_checkpoint_dir=str(tmp_path / "ckpt"))
    with pytest.raises(IngestionError, match=r"ragged.csv: .*line 11"):
        load_files([path], config)
    manifest = json.loads((checkpoint_folder(tmp_path) / MANIFEST).read_text())
    assert len(manifest["chunks"]) == 3  # chunks before the bad record were kept
    with pytest.raises(IngestionError, match=r"ragged.csv: .*line 11"):
        load_files([path], config)
    # Repairing the file changes its fingerprint, so the stale checkpoint is discarded.
    path.write_text(text)
    with caplog.at_level(logging.WARNING):
        data = load_files([path], config)
    assert "Ignoring unusable checkpoint for ragged.csv" in caplog.text
    pd.testing.assert_frame_equal(data, load_files([path], Config()))


def _corrupt_manifest(folder: Path, text: str) -> None:
    (folder / MANIFEST).write_text(text)


def _edit_manifest(folder: Path, edit) -> None:
    manifest = json.loads((folder / MANIFEST).read_text())
    edit(manifest)
    (folder / MANIFEST).write_text(json.dumps(manifest))


CORRUPTIONS = {
    "invalid json": lambda folder, _: _corrupt_manifest(folder, "{not json"),
    "empty manifest": lambda folder, _: _corrupt_manifest(folder, ""),
    "manifest is a list": lambda folder, _: _corrupt_manifest(folder, "[]"),
    "manifest is empty mapping": lambda folder, _: _corrupt_manifest(folder, "{}"),
    "wrong version": lambda folder, _: _edit_manifest(folder, lambda m: m.update(version=99)),
    "chunks not a list": lambda folder, _: _edit_manifest(folder, lambda m: m.update(chunks={})),
    "chunk entry missing keys": lambda folder, _: _edit_manifest(
        folder, lambda m: m["chunks"].__setitem__(0, {"file": "chunk-000001.json"})
    ),
    "record count not int": lambda folder, _: _edit_manifest(
        folder, lambda m: m["chunks"][0].update(records="3")
    ),
    "record count below rows": lambda folder, _: _edit_manifest(
        folder, lambda m: m["chunks"][0].update(records=0)
    ),
    "row count tampered": lambda folder, _: _edit_manifest(
        folder, lambda m: m["chunks"][0].update(rows=1)
    ),
    "chunk file missing": lambda folder, _: (folder / "chunk-000002.json").unlink(),
    "chunk file truncated": lambda folder, _: (folder / "chunk-000002.json").write_text(
        (folder / "chunk-000002.json").read_text()[:-20]
    ),
    "chunk file emptied": lambda folder, _: (folder / "chunk-000001.json").write_text(""),
    "chunk value tampered": lambda folder, _: (folder / "chunk-000001.json").write_text(
        (folder / "chunk-000001.json").read_text().replace("Product 1", "Product X")
    ),
    "chunk renamed in manifest": lambda folder, _: _edit_manifest(
        folder, lambda m: m["chunks"][0].update(file="../chunk-000001.json")
    ),
    "columns tampered": lambda folder, _: _edit_manifest(
        folder, lambda m: m["columns"].__setitem__(0, "when")
    ),
    "source modified": lambda folder, source: os.utime(source, ns=(1, 1)),
    "source grew": lambda folder, source: source.write_text(
        source.read_text() + ROW.format(day=28, cat=1, qty=1, price="1.00")
    ),
}


@pytest.mark.parametrize("corruption", sorted(CORRUPTIONS))
def test_corrupted_checkpoint_is_discarded_and_result_is_correct(
    source, config, tmp_path, monkeypatch, caplog, corruption
):
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    folder = checkpoint_folder(tmp_path)
    CORRUPTIONS[corruption](folder, source)
    expected = load_files([source], Config())
    seen = count_chunks(monkeypatch)
    with caplog.at_level(logging.WARNING):
        data = load_files([source], config)
    assert "Ignoring unusable checkpoint for sales.csv" in caplog.text
    assert sum(seen) == len(expected) + 2  # every record re-read, blanks included
    pd.testing.assert_frame_equal(data, expected)
    assert not list((tmp_path / "ckpt").iterdir())


def test_changed_settings_invalidate_checkpoint(source, config, tmp_path, monkeypatch, caplog):
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    expected = load_files([source], Config())
    for changed in (
        Config(csv_chunk_size=4, csv_checkpoint_dir=config.csv_checkpoint_dir),
        Config(
            csv_chunk_size=3,
            csv_checkpoint_dir=config.csv_checkpoint_dir,
            column_mapping={"Invoice": "order_id"},
        ),
    ):
        interrupt_after(monkeypatch, 2)
        caplog.clear()
        with caplog.at_level(logging.WARNING), pytest.raises(KeyboardInterrupt):
            load_files([source], changed)
        assert "Ignoring unusable checkpoint" in caplog.text
        monkeypatch.undo()
    # The last checkpoint was written with the mapping; the original settings reject it.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        pd.testing.assert_frame_equal(load_files([source], config), expected)
    assert "settings does not match" in caplog.text


def test_orphan_files_from_a_crash_mid_write_are_harmless(source, config, tmp_path, monkeypatch):
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    folder = checkpoint_folder(tmp_path)
    (folder / "chunk-000003.json").write_text('{"columns": [], "rows": [[1, 2')  # unlisted
    (folder / "checkpoint.json.tmp").write_text("garbage")
    (folder / "notes.txt").write_text("unrelated")
    expected = load_files([source], Config())
    seen = count_chunks(monkeypatch)
    pd.testing.assert_frame_equal(load_files([source], config), expected)
    assert sum(seen) == 6
    assert not list((tmp_path / "ckpt").iterdir())


def test_checkpoints_are_isolated_per_file_and_per_path(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = write_source(tmp_path / "a" / "sales.csv")
    b = write_source(tmp_path / "b" / "sales.csv", rows=7)
    config = Config(csv_chunk_size=3, csv_checkpoint_dir=str(tmp_path / "ckpt"))
    expected = load_files([a, b], Config())
    interrupt_after(monkeypatch, 5)  # file a completes (4 chunks); b is interrupted
    with pytest.raises(KeyboardInterrupt):
        load_files([a, b], config)
    folders = sorted(p.name for p in (tmp_path / "ckpt").iterdir())
    assert len(folders) == 1 and folders[0].startswith("sales-")
    monkeypatch.undo()
    seen = count_chunks(monkeypatch)
    pd.testing.assert_frame_equal(load_files([a, b], config), expected)
    assert sum(seen) == 12 + (9 - 3)  # a is re-read in full; b resumes after one chunk


def test_resume_with_differently_spelled_path_keeps_provenance_consistent(
    source, config, tmp_path, monkeypatch
):
    interrupt_after(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        load_files([source], config)
    monkeypatch.undo()
    monkeypatch.chdir(tmp_path)
    relative = Path("sales.csv")
    data = load_files([relative], config)
    assert data.source_file.unique().tolist() == ["sales.csv"]
    pd.testing.assert_frame_equal(data, load_files([relative], Config()))


def test_unwritable_checkpoint_dir_is_an_ingestion_error(source, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    config = Config(csv_chunk_size=3, csv_checkpoint_dir=str(blocker))
    with pytest.raises(IngestionError, match="Cannot write checkpoint for sales.csv"):
        load_files([source], config)


def test_no_checkpoint_without_configuration(source, tmp_path):
    data = load_files([source], Config(csv_chunk_size=3))
    assert not (tmp_path / "ckpt").exists()
    pd.testing.assert_frame_equal(data, load_files([source], Config()))


def test_excel_inputs_never_checkpoint(tmp_path):
    examples = Path(__file__).resolve().parent.parent / "examples"
    config = Config(csv_chunk_size=5, csv_checkpoint_dir=str(tmp_path / "ckpt"))
    data = load_files([examples / "sample_sales.xlsx"], config)
    assert not (tmp_path / "ckpt").exists()
    pd.testing.assert_frame_equal(data, load_files([examples / "sample_sales.xlsx"], Config()))


def test_skip_records_contract(source):
    config = Config(csv_chunk_size=5)
    chunks = list(iter_csv_chunks(source, config, skip_records=6))
    assert [len(c) for c in chunks] == [5, 1]
    assert list(chunks[0].index) == [6, 7, 8, 9, 10]
    assert list(iter_csv_chunks(source, config, skip_records=12)) == []
    assert list(iter_csv_chunks(source, config, skip_records=999)) == []
    whole = list(iter_csv_chunks(source, Config(), skip_records=6))
    assert len(whole) == 1 and len(whole[0]) == 6
    for bad in (-1, 2.0, True, "3"):
        with pytest.raises(ConfigurationError, match="skip_records"):
            list(iter_csv_chunks(source, config, skip_records=bad))


@pytest.mark.parametrize(
    "settings",
    [
        {"csv_checkpoint_dir": "ckpt"},  # requires a chunk size
        {"csv_chunk_size": 10, "csv_checkpoint_dir": ""},
        {"csv_chunk_size": 10, "csv_checkpoint_dir": "   "},
        {"csv_chunk_size": 10, "csv_checkpoint_dir": 5},
        {"csv_chunk_size": 10, "csv_checkpoint_dir": ["ckpt"]},
    ],
)
def test_invalid_checkpoint_settings(settings):
    with pytest.raises(ConfigurationError, match="csv_checkpoint_dir"):
        Config(**settings)


def test_checkpoint_open_without_folder_starts_fresh(source, tmp_path):
    checkpoint = CsvCheckpoint.open(tmp_path / "ckpt", source, Config(csv_chunk_size=3))
    assert checkpoint.records == 0 and checkpoint.pieces == [] and checkpoint.columns is None
    assert not (tmp_path / "ckpt").exists()


def test_cli_checkpoint_dir_flag_resumes_interrupted_run(source, tmp_path, monkeypatch, capsys):
    output = tmp_path / "reports"
    ckpt = tmp_path / "ckpt"
    args = ["--input", str(source), "--output", str(output), "--format", "excel"]
    args += ["--chunk-size", "3", "--checkpoint-dir", str(ckpt)]
    interrupt_after(monkeypatch, 2, error=RuntimeError)
    with pytest.raises(RuntimeError):
        main(args)
    assert len(list(ckpt.iterdir())) == 1
    monkeypatch.undo()
    assert main(args) == 0
    assert "Revenue:" in capsys.readouterr().out
    assert not list(ckpt.iterdir())
    assert main(args[:-4] + ["--checkpoint-dir", str(ckpt)]) == 1  # needs a chunk size
