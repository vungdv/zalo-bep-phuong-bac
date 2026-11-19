import asyncio
import json
import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from order_extractor import telemetry
from order_extractor.telemetry import (
    JsonFormatter,
    configure_logging,
    current_tick_id,
    get_logger,
    start_tick,
    tick,
)


def _reset(root):
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.propagate = True  # let caplog in other test modules see package logs again
    root.setLevel(logging.NOTSET)


@pytest.fixture
def configured(tmp_path):
    root = configure_logging(tmp_path, retention_days=7)
    yield tmp_path / telemetry.LOG_FILE_NAME
    _reset(root)


def read_lines(path):
    for handler in logging.getLogger(telemetry.ROOT_LOGGER).handlers:
        handler.flush()
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_lines_are_json_with_tick_id_and_extras(configured):
    log = get_logger("zalo.zalo_web")
    with tick() as tick_id:
        log.info("message_stored", extra={"conversation_key": "g1", "message_key": "1_2"})
        log.warning("record_failed", extra={"order_id": 42, "attempt": 2})
    lines = read_lines(configured)
    assert [line["event"] for line in lines] == ["message_stored", "record_failed"]
    assert {line["tick_id"] for line in lines} == {tick_id}
    assert lines[0]["conversation_key"] == "g1" and lines[0]["message_key"] == "1_2"
    assert lines[1] | {"order_id": 42, "attempt": 2, "level": "WARNING"} == lines[1]
    assert lines[0]["logger"] == "order_extractor.zalo.zalo_web"
    assert lines[0]["ts"].endswith("+00:00")


def test_sensitive_fields_only_at_debug(tmp_path):
    root = configure_logging(tmp_path, level=logging.DEBUG)
    try:
        log = get_logger()
        secret = {"text": "cho em 2 áo", "sender": "Lan", "conversation_title": "Nhóm", "title": "Nhóm"}
        log.info("info_line", extra=secret)
        log.debug("debug_line", extra=secret)
        lines = read_lines(tmp_path / telemetry.LOG_FILE_NAME)
    finally:
        _reset(root)
    info, debug = lines
    assert not set(telemetry.SENSITIVE_FIELDS) & set(info)
    assert "cho em" not in json.dumps(info, ensure_ascii=False)
    assert debug["text"] == "cho em 2 áo" and debug["sender"] == "Lan"


def test_info_file_threshold_drops_debug(configured):
    get_logger().debug("hidden", extra={"text": "x"})
    get_logger().info("shown")
    assert [line["event"] for line in read_lines(configured)] == ["shown"]


def test_nothing_on_stdout_and_only_warnings_on_stderr(configured, capsys):
    # configure again inside capsys so the stderr handler binds to the captured stream
    configure_logging(configured.parent)
    log = get_logger()
    log.info("quiet")
    log.error("loud", extra={"order_id": 1})
    out, err = capsys.readouterr()
    assert out == ""
    assert [json.loads(line)["event"] for line in err.splitlines()] == ["loud"]


TITLE, TEXT = "Nhóm Khách Sỉ Test", "cho em 2 áo"  # kept out of the raising lines: frames quote source code


def test_exceptions_above_debug_carry_type_and_frames_but_no_message(configured):
    def inner(title):
        raise KeyError(title)

    try:
        try:
            inner(TITLE)
        except KeyError as exc:
            raise ValueError(TEXT) from exc
    except ValueError:
        get_logger().exception("crashed")
    (line,) = read_lines(configured)
    assert line["exc_type"] == "ValueError"
    assert "in inner" in line["exc"] and "test_telemetry.py" in line["exc"]  # frames kept
    assert "KeyError" in line["exc"] and line["exc"].endswith("ValueError")
    dumped = json.dumps(line, ensure_ascii=False)
    assert TITLE not in dumped and TEXT not in dumped


def test_exception_type_names_include_the_module_outside_builtins(configured):
    from order_extractor.zalo.browser import EvalError

    secret = "".join(["sec", "ret"])
    try:
        raise EvalError(secret)
    except EvalError:
        get_logger().error("failed", exc_info=True)
    (line,) = read_lines(configured)
    assert line["exc_type"] == "order_extractor.zalo.browser.EvalError"
    assert secret not in line["exc"].replace("EvalError(secret)", "")


def test_exceptions_at_debug_are_serialised_in_full(tmp_path):
    root = configure_logging(tmp_path, level=logging.DEBUG)
    try:
        try:
            raise ValueError("boom")
        except ValueError:
            get_logger().debug("crashed", exc_info=True)
        (line,) = read_lines(tmp_path / telemetry.LOG_FILE_NAME)
        assert "ValueError: boom" in line["exc"]
        assert "exc_type" not in line
    finally:
        _reset(root)


def test_file_handler_rotates_daily_and_keeps_retention(configured):
    handlers = logging.getLogger(telemetry.ROOT_LOGGER).handlers
    (file_handler,) = [h for h in handlers if isinstance(h, TimedRotatingFileHandler)]
    assert file_handler.when == "MIDNIGHT"
    assert file_handler.backupCount == 7
    configure_logging(configured.parent, retention_days=7)
    ours = [h for h in logging.getLogger(telemetry.ROOT_LOGGER).handlers if isinstance(h.formatter, JsonFormatter)]
    assert len(ours) == 2  # idempotent configure: no duplicates


def test_tick_context_restores_and_start_tick_sets():
    with tick("outer"):
        with tick("inner"):
            assert current_tick_id() == "inner"
        assert current_tick_id() == "outer"


async def test_tasks_inherit_the_tick_id():
    async def job():
        await asyncio.sleep(0)
        return current_tick_id()

    tick_id = start_tick()
    task = asyncio.create_task(job())
    start_tick()  # the next tick must not leak into the earlier task
    assert await task == tick_id


def test_get_logger_prefixes_package():
    assert get_logger("order_extractor.db").name == "order_extractor.db"
    assert get_logger("db").name == "order_extractor.db"


def test_formatter_handles_non_json_values():
    record = logging.LogRecord("order_extractor", logging.INFO, __file__, 1, "evt", None, None)
    record.path = telemetry.Path("/tmp/x")
    assert json.loads(JsonFormatter().format(record))["path"] == "/tmp/x"
