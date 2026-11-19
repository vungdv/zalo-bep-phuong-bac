"""Status line (design §8, R6.4; task T14/T15)."""

from __future__ import annotations

import io

from order_extractor.status import LOGIN_PROMPT, NO_CHAT_PROMPT, Status, StatusPrinter


class TtyStream(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_render_shows_every_field():
    s = Status(
        login="reading",
        conversation="Nhóm khách sỉ",
        messages_read=12,
        orders_found=3,
        sheets="flushing",
    )
    s.set_counts({"NEW": 1, "RECORDED": 2, "FAILED": 4})
    assert s.render() == (
        "Zalo: reading · Nhóm khách sỉ | read 12 · found 3 · recorded 2 · failed 4 · pending 1 | sheets: flushing"
    )


def test_render_login_and_no_chat_prompts_and_drift():
    assert Status(login=LOGIN_PROMPT).render().startswith(f"Zalo: {LOGIN_PROMPT} |")
    s = Status(login=NO_CHAT_PROMPT, drift=True)
    assert s.render().startswith(f"Zalo: {NO_CHAT_PROMPT} |")
    assert s.render().endswith("| selectors may be outdated")


def test_set_counts_defaults_missing_statuses_to_zero():
    s = Status(orders_recorded=9)
    s.set_counts({})
    assert (s.orders_recorded, s.orders_failed, s.orders_pending) == (0, 0, 0)


def test_plain_stream_writes_a_line_only_when_it_changes():
    out = io.StringIO()
    p = StatusPrinter(out)
    assert p.tty is False
    s = Status()
    p.show(s)
    p.show(s)
    s.messages_read = 1
    p.show(s)
    p.close()
    lines = out.getvalue().splitlines()
    assert len(lines) == 2
    assert "read 0" in lines[0] and "read 1" in lines[1]
    assert "\r" not in out.getvalue()


def test_tty_redraws_in_place_and_ends_with_newline():
    out = TtyStream()
    p = StatusPrinter(out)
    assert p.tty is True
    s = Status()
    p.show(s)
    p.show(s)
    p.close()
    text = out.getvalue()
    assert text.count("\r\x1b[K") == 2
    assert text.endswith("\n") and text.count("\n") == 1


def test_close_without_output_writes_nothing():
    out = TtyStream()
    StatusPrinter(out).close()
    assert out.getvalue() == ""


def test_defaults_to_stdout(capsys):
    p = StatusPrinter(tty=False)
    p.show(Status())
    captured = capsys.readouterr()
    assert captured.out.startswith("Zalo: starting")
    assert captured.err == ""
