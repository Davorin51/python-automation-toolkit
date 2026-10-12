from pathlib import Path

import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError


def read_csv(path: Path, config: Config) -> pd.DataFrame:
    try:
        # Preserve IDs, strings such as NA, and raw values for error diagnostics.
        frame = pd.read_csv(
            path,
            sep=config.csv_delimiter,
            encoding=config.csv_encoding,
            dtype=str,
            keep_default_na=False,
            skip_blank_lines=False,
            header=None,
        )
        frame.columns = frame.iloc[0].tolist()
        return frame.iloc[1:].reset_index(drop=True)
    except (OSError, ValueError, UnicodeError, LookupError, pd.errors.ParserError) as exc:
        raise IngestionError(f"Cannot read CSV {path.name}: {exc}") from exc
