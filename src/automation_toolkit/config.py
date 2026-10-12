"""Load a small, strictly validated YAML configuration."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from automation_toolkit.exceptions import ConfigurationError


@dataclass(frozen=True)
class Config:
    company_name: str = "Demo Retail"
    currency: str = "EUR"
    validation_policy: str = "quarantine"
    date_format: str = "%Y-%m-%d"
    csv_delimiter: str = ","
    csv_encoding: str = "utf-8-sig"
    excel_sheet: str | int = 0
    top_n: int = 10
    column_mapping: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("company_name", "currency", "date_format", "csv_encoding"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ConfigurationError(f"{name} must be a non-empty string.")
        if not isinstance(self.validation_policy, str) or self.validation_policy not in {
            "quarantine",
            "skip",
            "reject_file",
        }:
            raise ConfigurationError("validation_policy must be quarantine, skip or reject_file.")
        if not isinstance(self.csv_delimiter, str) or len(self.csv_delimiter) != 1:
            raise ConfigurationError("csv_delimiter must contain exactly one character.")
        if type(self.top_n) is not int or not 1 <= self.top_n <= 50:
            raise ConfigurationError("top_n must be an integer between 1 and 50.")
        if type(self.excel_sheet) not in (str, int) or (
            isinstance(self.excel_sheet, int) and self.excel_sheet < 0
        ):
            raise ConfigurationError("excel_sheet must be a sheet name or non-negative index.")
        fields = {"date", "product", "category", "quantity", "unit_price", "order_id"}
        if not isinstance(self.column_mapping, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or v not in fields
            for k, v in self.column_mapping.items()
        ):
            raise ConfigurationError("column_mapping must map source headers to supported fields.")


def load_config(path: Path | None = None) -> Config:
    if path is None:
        return Config()
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigurationError("Configuration must be a YAML mapping.")
        return Config(**data)
    except ConfigurationError:
        raise
    except (OSError, yaml.YAMLError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"Cannot load configuration {path}: {exc}") from exc
