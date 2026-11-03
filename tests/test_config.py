from pathlib import Path

import pytest

from automation_toolkit.config import Config, load_config
from automation_toolkit.exceptions import ConfigurationError


@pytest.mark.parametrize(
    "settings",
    [
        {"validation_policy": "delete"},
        {"validation_policy": []},
        {"top_n": True},
        {"top_n": 0},
        {"top_n": 51},
        {"csv_delimiter": "::"},
        {"csv_chunk_size": 0},
        {"csv_chunk_size": -5},
        {"csv_chunk_size": True},
        {"csv_chunk_size": "100"},
        {"csv_chunk_size": 2.5},
        {"company_name": ""},
        {"currency": 1},
        {"excel_sheet": -1},
        {"excel_sheet": []},
        {"column_mapping": {"price": []}},
        {"column_mapping": []},
    ],
)
def test_invalid_settings(settings):
    with pytest.raises(ConfigurationError):
        Config(**settings)


def test_yaml_configuration(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("currency: USD\nvalidation_policy: skip\ntop_n: 5\n")
    assert load_config(path).currency == "USD"
    assert load_config().top_n == 10
    path.write_text("")
    assert load_config(path) == Config()
    assert Config().csv_chunk_size is None
    path.write_text("csv_chunk_size: 500\n")
    assert load_config(path).csv_chunk_size == 500
    path.write_text("csv_chunk_size: null\n")
    assert load_config(path).csv_chunk_size is None
    assert load_config(path).csv_checkpoint_dir is None
    path.write_text("csv_chunk_size: 500\ncsv_checkpoint_dir: .checkpoints\n")
    assert load_config(path).csv_checkpoint_dir == ".checkpoints"


@pytest.mark.parametrize("content", ["- unexpected", "unknown: value", "broken: ["])
def test_bad_yaml(tmp_path: Path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_missing_config(tmp_path):
    with pytest.raises(ConfigurationError):
        load_config(tmp_path / "missing.yaml")
