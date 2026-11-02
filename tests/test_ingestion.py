from datetime import datetime

import pandas as pd
import pytest
from openpyxl import Workbook

from automation_toolkit.config import Config
from automation_toolkit.exceptions import IngestionError
from automation_toolkit.ingestion import REQUIRED, load_files, normalize_columns
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


HEADER = "date,product,category,quantity,unit_price\n"


def test_blank_rows_are_skipped_and_source_rows_preserved(tmp_path, caplog):
    path = tmp_path / "blank.csv"
    path.write_text(
        HEADER
        + "2026-01-05,Laptop,Electronics,2,900\n\n ,,,,\n2026-01-06,Mouse,Electronics,1,20\n\n"
    )
    with caplog.at_level("INFO"):
        data = load_files([path], Config())
    assert data.source_row.tolist() == [2, 5]
    assert validate(data, Config()).invalid_rows == 0
    assert "Skipped 3 blank rows" in caplog.text


def test_file_with_only_blank_rows_has_no_data(tmp_path):
    path = tmp_path / "blank.csv"
    path.write_text(HEADER + "\n,,,,\n   ,,,,\n")
    with pytest.raises(IngestionError, match="no data rows"):
        load_files([path], Config())


def test_blank_header_columns_are_ignored(tmp_path):
    path = tmp_path / "trailing.csv"
    path.write_text(
        "date,product,category,quantity,unit_price,,\n2026-01-05,Laptop,Electronics,2,900,,\n"
    )
    data = load_files([path], Config())
    assert list(data.columns) == REQUIRED + ["source_file", "source_row"]
    assert validate(data, Config()).invalid_rows == 0


def test_excel_blank_rows_and_empty_header_columns(tmp_path):
    path = tmp_path / "sales.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Product", "Category", "Quantity", "Unit Price", None, None])
    ws.append([datetime(2026, 1, 5), "Laptop", "Electronics", 2, 899.99, None, None])
    ws.append([None] * 7)
    ws.append(["2026-01-06", "Mouse", "Electronics", "1", "24.90", None, None])
    ws.append([None, "", "   ", None, None, None, None])
    wb.save(path)
    data = load_files([path], Config())
    assert data.source_row.tolist() == [2, 4]
    result = validate(data, Config())
    assert result.invalid_rows == 0
    assert result.transactions.revenue_cents.tolist() == [179_998, 2_490]


def test_excel_missing_cells_are_reported_by_column(tmp_path):
    path = tmp_path / "sales.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "Product", "Category", "Quantity", "Unit Price", "Order ID"])
    ws.append([datetime(2026, 1, 5), None, "Electronics", None, 899.99, "0001"])
    ws.append([None, "Mouse", "Electronics", 1.5, "abc", "0002"])
    wb.save(path)
    result = validate(load_files([path], Config()), Config())
    assert result.errors.source_row.tolist() == [2, 3]
    assert result.errors.invalid_columns.tolist() == [
        "quantity; product",
        "date; quantity; unit_price",
    ]
    assert result.errors.reason.iloc[1].startswith("Missing date; Quantity must be")


def test_ambiguous_and_missing_column_errors_name_the_headers(tmp_path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("Date,date,product,category,quantity,unit_price\n2026-01-05,x,P,C,1,2\n")
    with pytest.raises(
        IngestionError, match=r"ambiguous.csv: Ambiguous columns.*'Date', 'date' -> date"
    ):
        load_files([path], Config())
    path = tmp_path / "missing.csv"
    path.write_text("When,Product,Qty\n2026-01-05,Laptop,2\n")
    with pytest.raises(IngestionError) as info:
        load_files([path], Config())
    message = str(info.value)
    assert message.startswith("missing.csv: Missing required columns: category, date, unit_price.")
    assert "Found headers: 'When', 'Product', 'Qty'" in message
    assert "column_mapping" in message


def test_directory_input_is_rejected(tmp_path):
    folder = tmp_path / "folder.csv"
    folder.mkdir()
    with pytest.raises(IngestionError, match="not a file"):
        load_files([folder], Config())
