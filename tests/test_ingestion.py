from datetime import datetime

import pandas as pd
import pytest
from openpyxl import Workbook

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.ingestion import load_files, normalize_columns
from automation_toolkit.processing.validation import validate


def test_csv_aliases_and_provenance(sales_file):
    data = load_files([sales_file], Config())
    assert len(data) == 3
    assert data.source_row.tolist() == [2, 3, 4]
    assert data.order_id.tolist() == ["A001", "A001", "A002"]


def test_excel_native_dates_and_text_ids(tmp_path):
    path = tmp_path / "sales.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    ws.append(["Date", "Product", "Category", "Quantity", "Unit Price", "Order ID"])
    ws.append([datetime(2026, 1, 5), "Laptop", "Electronics", 2, 900, "0001"])
    wb.save(path)
    cfg = Config(excel_sheet="Sales")
    result = validate(load_files([path], cfg), cfg)
    assert result.invalid_rows == 0
    assert result.transactions.order_id.iloc[0] == "0001"
    with pytest.raises(IngestionError, match="Cannot read XLSX"):
        load_files([path], Config(excel_sheet="Missing"))


def test_multiple_files(sales_file, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text(sales_file.read_text().replace("A00", "B00"))
    assert len(load_files([sales_file, other], Config())) == 6
    other.write_text("date,product,category,quantity,unit_price\n2026-01-01,P,C,1,2\n")
    with pytest.raises(IngestionError, match="every input"):
        load_files([sales_file, other], Config())


@pytest.mark.parametrize(
    "content", ["", "date,product,category,quantity,unit_price\n", "Product,Quantity\nLaptop,2\n"]
)
def test_empty_and_missing_columns(tmp_path, content):
    path = tmp_path / "bad.csv"
    path.write_text(content)
    with pytest.raises(IngestionError):
        load_files([path], Config())


def test_missing_unsupported_repeated_files(sales_file, tmp_path):
    for paths in [[], [tmp_path / "missing.csv"], [sales_file, sales_file]]:
        with pytest.raises(IngestionError):
            load_files(paths, Config())
    path = tmp_path / "file.json"
    path.write_text("{}")
    with pytest.raises(IngestionError, match="Unsupported"):
        load_files([path], Config())
    path = tmp_path / "broken.xlsx"
    path.write_text("invalid zip")
    with pytest.raises(IngestionError):
        load_files([path], Config())


def test_ambiguous_columns_and_explicit_mapping():
    with pytest.raises(IngestionError, match="Ambiguous"):
        normalize_columns(pd.DataFrame(columns=["Date", "date"]), Config())
    columns = ["When", "product", "category", "quantity", "unit_price"]
    assert "date" in normalize_columns(
        pd.DataFrame(columns=columns), Config(column_mapping={"When": "date"})
    )


def test_csv_delimiter_and_encoding(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("date;product;category;quantity;unit_price\n2026-01-01;NA;C;1;0.10\n")
    data = load_files([path], Config(csv_delimiter=";"))
    assert data["product"].iloc[0] == "NA"
    with pytest.raises(IngestionError):
        load_files([path], Config(csv_encoding="no-such-encoding"))


def test_identical_source_headers_are_not_silently_renamed(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text(
        "date,date,product,category,quantity,unit_price\n2026-01-01,2026-02-01,P,C,1,2\n"
    )
    with pytest.raises(IngestionError, match="Ambiguous"):
        load_files([path], Config())
