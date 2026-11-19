import pytest
from typer.testing import CliRunner

from order_extractor.cli import NOT_IMPLEMENTED_EXIT, app

runner = CliRunner()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("start", "status", "reprocess"):
        assert command in result.output


@pytest.mark.parametrize("args", [["start", "--since", "2026-11-19"], ["status"], ["reprocess", "--order", "1"]])
def test_stub_commands_say_not_implemented(args):
    result = runner.invoke(app, args)
    assert result.exit_code == NOT_IMPLEMENTED_EXIT
    assert "not implemented yet" in result.output
