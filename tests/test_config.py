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


@pytest.mark.parametrize("content", ["- unexpected", "unknown: value", "broken: ["])
def test_bad_yaml(tmp_path: Path, content):
    path = tmp_path / "config.yaml"
    path.write_text(content)
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_missing_config(tmp_path):
    with pytest.raises(ConfigurationError):
        load_config(tmp_path / "missing.yaml")
