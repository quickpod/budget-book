"""Categorization rules, budget-vs-actual, reports and chart rendering."""

import os

from budgetbook import categorize, budget as budgetmod, reports


def _seed(db):
    db.add_transaction("2026-01-03", -4.50, payee="Starbucks Coffee")
    db.add_transaction("2026-01-08", -60.00, payee="Whole Foods Market")
    db.add_transaction("2026-01-10", -30.00, payee="Shell Gas Station")
    db.add_transaction("2026-01-12", 2500.00, payee="ACME Payroll")
    db.add_transaction("2026-02-05", -12.00, payee="Starbucks Coffee")


# ---- categorize ----------------------------------------------------------
def test_rules_assign_categories(db):
    _seed(db)
    categorize.add_rule(db, "starbucks", "Coffee")
    categorize.add_rule(db, "whole foods", "Groceries")
    n = categorize.apply_rules(db)
    assert n == 3  # two Starbucks + one Whole Foods
    cats = {t["payee"]: t["category"] for t in db.list_transactions()}
    assert cats["Starbucks Coffee"] == "Coffee"
    assert cats["Whole Foods Market"] == "Groceries"


def test_learn_rule_from_manual_assignment(db):
    tid = db.add_transaction("2026-01-20", -18.0, payee="Netflix",
                             category="Subscriptions")
    rid = categorize.learn_rule(db, tid)
    assert rid is not None
    # a new, uncategorized Netflix charge now auto-categorizes
    db.add_transaction("2026-02-20", -18.0, payee="Netflix Streaming")
    categorize.apply_rules(db)
    got = [t for t in db.list_transactions() if t["payee"] == "Netflix Streaming"][0]
    assert got["category"] == "Subscriptions"


# ---- budget --------------------------------------------------------------
def test_budget_vs_actual(db):
    _seed(db)
    categorize.add_rule(db, "starbucks", "Coffee")
    categorize.add_rule(db, "shell", "Auto")
    categorize.apply_rules(db)
    db.set_budget("Coffee", 10.00)     # Jan spend is 4.50 -> under
    db.set_budget("Auto", 20.00)       # Jan spend is 30.00 -> over

    rows = {r["category"]: r for r in budgetmod.budget_vs_actual(db, month="2026-01")}
    assert rows["Coffee"]["actual"] == 4.50
    assert rows["Coffee"]["over"] is False
    assert rows["Coffee"]["remaining"] == 5.50
    assert rows["Auto"]["actual"] == 30.00
    assert rows["Auto"]["over"] is True
    assert rows["Auto"]["remaining"] == -10.00

    tot = budgetmod.totals(db, month="2026-01")
    assert "Auto" in tot["over_categories"]


# ---- reports -------------------------------------------------------------
def test_spending_by_category(db):
    _seed(db)
    categorize.add_rule(db, "starbucks", "Coffee")
    categorize.apply_rules(db)
    rows = reports.spending_by_category(db, month="2026-01")
    amounts = {r["category"]: r["amount"] for r in rows}
    assert amounts["Coffee"] == 4.50
    # income is excluded from spending
    assert "ACME Payroll" not in amounts
    # sorted largest first
    assert rows[0]["amount"] >= rows[-1]["amount"]


def test_income_vs_expense_and_networth(db):
    _seed(db)
    db.add_account("Checking", opening=1000.0)
    ive = reports.income_vs_expense(db)
    jan = [r for r in ive if r["month"] == "2026-01"][0]
    assert jan["income"] == 2500.00
    assert jan["expense"] == 94.50
    nw = reports.net_worth(db)
    assert nw[-1]["net_worth"] == round(1000.0 + (2500.0 - 94.50) + (-12.0), 2)


def test_render_chart_writes_png(db, tmp_path):
    _seed(db)
    data = reports.spending_by_category(db)
    out = os.path.join(str(tmp_path), "spending.png")
    reports.render_chart("spending", data, out)
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0
    # PNG magic number
    with open(out, "rb") as fh:
        assert fh.read(4) == b"\x89PNG"


def test_render_all_chart_kinds(db, tmp_path):
    _seed(db)
    reports.render_chart("cashflow", reports.income_vs_expense(db),
                         str(tmp_path / "cf.png"))
    reports.render_chart("networth", reports.net_worth(db),
                         str(tmp_path / "nw.png"))
    db.set_budget("Coffee", 10.0)
    reports.render_chart("budget", budgetmod.budget_vs_actual(db),
                         str(tmp_path / "bud.png"))
    for name in ("cf.png", "nw.png", "bud.png"):
        assert os.path.getsize(tmp_path / name) > 0
