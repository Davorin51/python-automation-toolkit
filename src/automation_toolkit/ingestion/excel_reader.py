from pathlib import Path

import pandas as pd

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError


def read_excel(path: Path, config: Config) -> pd.DataFrame:
    try:
        frame = pd.read_excel(
            path,
            sheet_name=config.excel_sheet,
            engine="openpyxl",
            dtype=object,
            keep_default_na=False,
            header=None,
        )
        if frame.empty:
            raise ValueError("The worksheet is empty.")
        frame.columns = frame.iloc[0].tolist()
        return frame.iloc[1:].reset_index(drop=True)
    except Exception as exc:
        raise IngestionError(f"Cannot read XLSX {path.name}: {exc}") from exc
