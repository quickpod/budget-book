"""GUI tests for the 1.1.0 Aura layout-language rework (YNAB benchmark).

Pure checks run anywhere; the App tests need a display (run the suite under
``xvfb-run -a python3 -m pytest``) and are skipped headless, mirroring the
house pattern.  Everything is hermetic: BUDGETBOOK_HOME + a per-test db.
"""

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from budgetbook import gui, guiconfig  # noqa: E402


def test_theme_defaults_to_system(tmp_path, monkeypatch):
    monkeypatch.setenv("BUDGETBOOK_HOME", str(tmp_path))
    assert guiconfig.get_theme() == "system"
    guiconfig.set_theme("dark")
    assert guiconfig.get_theme() == "dark"
    guiconfig.set_theme("nope")
    assert guiconfig.get_theme() == "dark"
    guiconfig.set_theme("system")
    assert guiconfig.get_theme() == "system"


def test_sections_include_about():
    assert ("about", "About") in gui.SECTIONS
    assert len(gui.SECTIONS) <= 7


# ---------------------------------------------------------------------------
# the real window (Xvfb)
# ---------------------------------------------------------------------------
needs_display = pytest.mark.skipif(
    sys.platform == "win32" or not os.environ.get("DISPLAY"),
    reason="needs a display (run under xvfb-run)")


def _pump(a, seconds=0.5):
    end = time.time() + seconds
    while time.time() < end:
        a.update()
        time.sleep(0.02)


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    home = tmp_path_factory.mktemp("bb-home")
    old = os.environ.get("BUDGETBOOK_HOME")
    os.environ["BUDGETBOOK_HOME"] = str(home)
    App = gui.build_app()
    a = App(db_path=str(home / "test.db"))

    def sync_bg(work, on_ok, button=None, busy="Working…"):
        try:
            res = work()
        except Exception as ex:
            a.set_error(str(ex))
            return
        on_ok(res)
    a._bg = sync_bg
    _pump(a, 0.8)
    yield a
    try:
        a.destroy()
    except Exception:
        pass
    if old is None:
        os.environ.pop("BUDGETBOOK_HOME", None)
    else:
        os.environ["BUDGETBOOK_HOME"] = old


def _seed(db):
    db.add_transaction(date="2026-08-01", amount=-42.50, payee="Grocers Ltd",
                       category="Food", account="Checking")
    db.add_transaction(date="2026-08-02", amount=-9.99, payee="Coffee Shop",
                       category="Food", account="Checking")
    db.add_transaction(date="2026-08-03", amount=2500.0, payee="Employer",
                       category="Salary", account="Savings")
    db.add_category("Food")
    db.add_category("Salary")


@needs_display
def test_shell_empty_state_then_data(app):
    assert app.active_section == "transactions"
    # empty at first — the empty state is placed
    assert not app.tx_tree.get_children()
    _seed(app.db)
    app._refresh_transactions()
    _pump(app, 0.3)
    assert len(app.tx_tree.get_children()) == 3


@needs_display
def test_account_sidebar_filter(app):
    app._refresh_accounts_sidebar()
    _pump(app, 0.2)
    texts = [b.cget("text") for b in app._acct_scroll.winfo_children()
             if hasattr(b, "cget")]
    assert any("All accounts" in t for t in texts)
    assert any("Checking" in t for t in texts)
    app._set_account_filter("Checking")
    _pump(app, 0.2)
    assert len(app.tx_tree.get_children()) == 2
    app._set_account_filter(None)
    _pump(app, 0.2)
    assert len(app.tx_tree.get_children()) == 3


@needs_display
def test_payee_search_and_category_filter(app):
    app.tx_search.set("coffee")
    app._refresh_transactions()
    assert len(app.tx_tree.get_children()) == 1
    app.tx_search.set("")
    app.tx_cat_filter.set("Salary")
    app._refresh_transactions()
    assert len(app.tx_tree.get_children()) == 1
    app.tx_cat_filter.set("All categories")
    app._refresh_transactions()
    assert len(app.tx_tree.get_children()) == 3


@needs_display
def test_reports_segmented(app):
    app.show("reports")
    _pump(app, 0.5)
    assert app._last_report is not None


@needs_display
def test_both_themes_no_crash(app):
    for theme in ("light", "dark"):
        app.set_theme(theme)
        app.update_idletasks()
        app.update()
        assert app.theme == theme
