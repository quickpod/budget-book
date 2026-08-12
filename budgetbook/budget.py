"""Budget-vs-actual analysis.

A budget is a spending *limit* per category (see :meth:`Database.set_budget`).
Actual spending for a category in a period is the absolute value of the sum of
that category's negative (expense) transactions.  Income transactions do not
count against an expense budget.
"""

from __future__ import annotations

from .errors import BudgetBookError


def actual_spending(db, category, month=None):
    """Total *spending* (positive number) for *category* in *month* (or all)."""
    txns = db.list_transactions(month=month, category=category)
    spent = 0.0
    for t in txns:
        amt = float(t["amount"])
        if amt < 0:
            spent += -amt
    return round(spent, 2)


def budget_vs_actual(db, month=None, period="monthly"):
    """Return a per-category budget-vs-actual report.

    Each entry: ``{category, limit, actual, remaining, over, pct}`` where
    ``over`` is True when actual exceeds the limit, ``remaining`` can be
    negative, and ``pct`` is actual/limit as a percentage (``None`` if no
    limit).  Categories that have a budget but no spending still appear.
    """
    budgets = {b["category"]: b["limit_amount"] for b in db.list_budgets(period=period)}
    # include categories that have spending even without a budget
    categories = set(budgets)
    for t in db.list_transactions(month=month):
        if float(t["amount"]) < 0 and (t.get("category") or "").strip():
            categories.add(t["category"])

    report = []
    for cat in sorted(categories):
        limit = budgets.get(cat)
        actual = actual_spending(db, cat, month=month)
        entry = {
            "category": cat,
            "limit": round(float(limit), 2) if limit is not None else None,
            "actual": actual,
            "remaining": round(float(limit) - actual, 2) if limit is not None else None,
            "over": (limit is not None and actual > float(limit)),
            "pct": round(actual / float(limit) * 100.0, 1) if limit else None,
        }
        report.append(entry)
    return report


def totals(db, month=None, period="monthly"):
    """Roll up the budget-vs-actual report into overall figures."""
    rows = budget_vs_actual(db, month=month, period=period)
    total_limit = sum(r["limit"] for r in rows if r["limit"] is not None)
    total_actual = sum(r["actual"] for r in rows)
    over = [r["category"] for r in rows if r["over"]]
    return {
        "total_limit": round(total_limit, 2),
        "total_actual": round(total_actual, 2),
        "total_remaining": round(total_limit - total_actual, 2),
        "over_categories": over,
    }


__all__ = ["actual_spending", "budget_vs_actual", "totals", "BudgetBookError"]
