"""GoogleSheet adapter against a mocked gspread worksheet: no real Google API call is made."""

from __future__ import annotations

from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import gspread
import pytest
import requests
from gspread.utils import InsertDataOption, ValidationConditionType, ValueInputOption

from order_extractor.config import Settings
from order_extractor.recorder import sheet as sheet_module
from order_extractor.recorder.orders_repo import Order
from order_extractor.recorder.sheet import (
    HEADER,
    GoogleSheet,
    SheetsError,
    format_time,
    normalize_order_id,
    order_row,
)

TZ = ZoneInfo("Asia/Ho_Chi_Minh")
SENT = 1_795_059_480_000  # 2026-11-19 03:38 UTC = 10:38 in Asia/Ho_Chi_Minh


def order(order_id: int = 42, **kw) -> Order:
    fields = {
        "id": order_id,
        "conversation_key": "g1",
        "message_key": "k",
        "number": 2,
        "sent_at_ms": SENT,
        "conversation_title": "Nhóm khách sỉ",
        "sender": "Lan",
        "text": "Shop ơi cho em 2 áo thun đen size M",
        "status": "NEW",
        "attempts": 0,
        "created_at_ms": SENT,
        "next_attempt_at_ms": SENT,
        "sheet_row": None,
        "error": None,
    }
    fields.update(kw)
    return Order(**fields)


@pytest.fixture
def ws() -> MagicMock:
    return MagicMock(spec=gspread.Worksheet)


@pytest.fixture
def sheet(ws) -> GoogleSheet:
    return GoogleSheet(lambda: ws, TZ)


def test_order_row_matches_requirements_example():
    assert order_row(order(), TZ) == [
        "42",
        "2026-11-19 10:38",
        "Nhóm khách sỉ",
        "Lan",
        "Shop ơi cho em 2 áo thun đen size M",
        2,
        False,
    ]
    purged = order_row(order(conversation_title=None, sender=None, text=None), TZ)
    assert purged[2:5] == ["", "", ""]


def test_format_time_uses_display_tz():
    assert format_time(SENT, ZoneInfo("UTC")) == "2026-11-19 03:38"
    assert format_time(SENT, TZ) == "2026-11-19 10:38"


def test_normalize_order_id():
    assert normalize_order_id(" 42 ") == "42"
    assert normalize_order_id(42) == "42"
    assert normalize_order_id(None) == ""


def test_read_header_trims_and_drops_trailing_blanks(sheet, ws):
    ws.row_values.return_value = [" order_id", "time ", "conversation", "sender", "messages", "order", "served", ""]
    assert sheet.read_header() == list(HEADER)
    ws.row_values.assert_called_once_with(1)
    ws.row_values.return_value = []
    assert sheet.read_header() == []


def test_write_header_writes_row_1_raw_and_checkbox_on_served(sheet, ws):
    sheet.write_header()
    ws.update.assert_called_once_with([list(HEADER)], "A1:G1", value_input_option=ValueInputOption.raw)
    ws.add_validation.assert_called_once_with("G2:G", ValidationConditionType.boolean, [], strict=True)


def test_read_order_ids_skips_header_and_blanks_and_trims(sheet, ws):
    ws.col_values.return_value = ["order_id", "1", " 2 ", "", "41", 7]
    assert sheet.read_order_ids() == {"1", "2", "41", "7"}
    ws.col_values.assert_called_once_with(1)


def test_append_rows_is_one_raw_call_and_returns_row_numbers(sheet, ws):
    ws.append_rows.return_value = {"updates": {"updatedRange": "'Orders'!A5:G7"}}
    rows = sheet.append_rows([order(1, text='=HYPERLINK("x")'), order(2), order(3)])
    assert rows == [5, 6, 7]
    ws.append_rows.assert_called_once()
    (values,), kwargs = ws.append_rows.call_args
    assert kwargs == {
        "value_input_option": ValueInputOption.raw,
        "insert_data_option": InsertDataOption.insert_rows,
        "table_range": "A1",
    }
    assert [v[0] for v in values] == ["1", "2", "3"]  # order_id written as a string
    assert values[0][4] == '=HYPERLINK("x")'  # RAW: never evaluated
    assert all(v[6] is False for v in values)  # served starts unticked
    # nothing but the append touched the sheet: served is never written on an existing row
    ws.update.assert_not_called()
    ws.batch_update.assert_not_called()
    ws.update_cell.assert_not_called()


def test_append_rows_single_row_range_and_unknown_range(sheet, ws):
    ws.append_rows.return_value = {"updates": {"updatedRange": "Orders!A9"}}
    assert sheet.append_rows([order(1)]) == [9]
    ws.append_rows.return_value = {}
    assert sheet.append_rows([order(1), order(2)]) == [None, None]
    assert sheet.append_rows([]) == []


def test_worksheet_is_opened_once_lazily():
    opener = MagicMock(return_value=MagicMock(spec=gspread.Worksheet))
    sheet = GoogleSheet(opener, TZ)
    opener.assert_not_called()
    sheet.read_header()
    sheet.read_order_ids()
    opener.assert_called_once()


def _api_error() -> gspread.exceptions.APIError:
    response = MagicMock()
    response.json.return_value = {"error": {"code": 503, "message": "unavailable", "status": "UNAVAILABLE"}}
    response.status_code = 503
    return gspread.exceptions.APIError(response)


@pytest.mark.parametrize(
    ("exc", "timeout"),
    [
        (requests.exceptions.ReadTimeout("read timed out"), True),
        (requests.exceptions.ConnectTimeout("connect timed out"), True),
        (TimeoutError("timed out"), True),
        (requests.exceptions.ConnectionError("reset"), False),
        (OSError("no such file"), False),
        (RuntimeError("anything"), False),
    ],
)
def test_errors_are_wrapped_as_sheets_error(sheet, ws, exc, timeout):
    ws.append_rows.side_effect = exc
    with pytest.raises(SheetsError) as info:
        sheet.append_rows([order()])
    assert info.value.timeout is timeout
    assert info.value.__cause__ is exc
    assert "append_rows" in str(info.value)


def test_api_error_and_wrapped_timeout(sheet, ws):
    ws.col_values.side_effect = _api_error()
    with pytest.raises(SheetsError) as info:
        sheet.read_order_ids()
    assert not info.value.timeout
    assert "APIError" in str(info.value)

    wrapper = RuntimeError("outer")
    wrapper.__cause__ = requests.exceptions.ReadTimeout("inner")
    ws.row_values.side_effect = wrapper
    with pytest.raises(SheetsError) as info:
        sheet.read_header()
    assert info.value.timeout


def test_open_failure_is_a_sheets_error():
    def opener():
        raise FileNotFoundError("secrets/sa.json")

    with pytest.raises(SheetsError):
        GoogleSheet(opener, TZ).write_header()


def test_forbidden_without_message_explains_how_to_fix():
    def opener():
        raise PermissionError  # what gspread raises for HTTP 403 on open_by_key

    with pytest.raises(SheetsError, match="share the sheet with the service account") as info:
        GoogleSheet(opener, TZ).read_header()
    assert not info.value.timeout
    assert "PermissionError" in str(info.value)


def test_from_settings_sets_client_timeout_and_opens_tab(monkeypatch):
    client = MagicMock()
    service_account = MagicMock(return_value=client)
    monkeypatch.setattr(sheet_module.gspread, "service_account", service_account)
    settings = Settings(
        sheet_id="abc", sheet_orders_tab="Đơn", sheets_timeout_seconds=7, google_service_account_file="sa.json"
    )
    sheet = GoogleSheet.from_settings(settings)
    service_account.assert_not_called()  # no network until the first (threaded) call
    client.open_by_key.return_value.worksheet.return_value.row_values.return_value = list(HEADER)
    assert sheet.read_header() == list(HEADER)
    service_account.assert_called_once_with(filename=settings.google_service_account_file)
    client.set_timeout.assert_called_once_with(7.0)
    client.open_by_key.assert_called_once_with("abc")
    client.open_by_key.return_value.worksheet.assert_called_once_with("Đơn")


def test_from_settings_without_sheet_id_fails_on_use(monkeypatch):
    monkeypatch.setattr(sheet_module.gspread, "service_account", MagicMock())
    sheet = GoogleSheet.from_settings(Settings(sheet_id=""))
    with pytest.raises(SheetsError, match="SHEET_ID"):
        sheet.read_header()
    sheet_module.gspread.service_account.assert_not_called()
