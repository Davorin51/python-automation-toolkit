from pathlib import Path

import pandas as pd
import pytest

from automation_toolkit.config import Config
from automation_toolkit.ingestion import load_files
from automation_toolkit.processing.validation import validate


@pytest.fixture
def sales_file(tmp_path: Path) -> Path:
    path = tmp_path / "sales.csv"
    path.write_text(
        "Date,Product,Category,Quantity,Unit Price,Order ID\n"
        "2026-01-05,Laptop,Electronics,2,900,A001\n"
        "2026-01-05,Mouse,Electronics,10,25,A001\n"
        "2026-02-07,Desk,Furniture,3,300,A002\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def clean_sales(sales_file: Path) -> pd.DataFrame:
    return validate(load_files([sales_file], Config()), Config()).transactions
