"""CLI clean-exit behaviour and headless GUI import safety."""

import pytest
import sys
import budgetbook.__main__ as cli
from budgetbook import gui


def test_cli_add_list_and_clean_error(tmp_path, capsys):
    dbp = str(tmp_path / "cli.db")
    rc = cli.main(["--db", dbp, "add", "-42.5", "--payee", "Store",
                   "--category", "Groceries", "--date", "2026-01-05"])
    assert rc == 0
    rc = cli.main(["--db", dbp, "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Store" in out

    # a bad date is a BudgetBookError -> clean exit code 1, message on stderr
    rc = cli.main(["--db", dbp, "add", "-1", "--date", "not-a-date"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "error:" in err


def test_cli_report_writes_chart(tmp_path):
    dbp = str(tmp_path / "cli.db")
    cli.main(["--db", dbp, "add", "-20", "--payee", "Cafe",
              "--category", "Coffee", "--date", "2026-01-05"])
    png = str(tmp_path / "spend.png")
    rc = cli.main(["--db", dbp, "report", "spending", "--out", png])
    assert rc == 0
    import os
    assert os.path.getsize(png) > 0


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_gui_import_is_side_effect_free():
    # importing the module must not create a Tk root or require a display
    assert hasattr(gui, "main")
    assert callable(gui.main)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows CI has a real display; main() would open a window and block")
def test_gui_main_headless_returns_zero(monkeypatch):
    # simulate a headless Linux box: no DISPLAY -> main() returns 0, not raise
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(gui.sys, "platform", "linux")
    monkeypatch.setattr(gui.os, "name", "posix")
    assert gui.main() == 0
