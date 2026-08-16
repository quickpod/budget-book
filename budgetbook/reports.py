"""Reporting: summary data structures + matplotlib (Agg) chart rendering.

The ``*_data`` helpers return plain Python structures (lists/dicts) so they are
easy to test and to feed into the GUI.  :func:`render_chart` turns one of those
structures into a PNG using matplotlib's headless **Agg** backend -- it never
needs a display, which is why the backend is forced before ``pyplot`` is ever
imported.
"""

from __future__ import annotations

import os

import matplotlib

# Force the non-interactive backend BEFORE pyplot is imported anywhere, so
# rendering works on a headless server and in CI with no $DISPLAY.
matplotlib.use("Agg")

from .errors import BudgetBookError  # noqa: E402


def _month_of(date):
    return (date or "")[:7]


def spending_by_category(db, month=None):
    """Total spending per category (expenses only), largest first.

    Returns ``[{"category": str, "amount": float}, ...]`` where amount is a
    positive spending figure.
    """
    totals = {}
    for t in db.list_transactions(month=month):
        amt = float(t["amount"])
        if amt < 0:
            cat = (t.get("category") or "Uncategorized").strip() or "Uncategorized"
            totals[cat] = totals.get(cat, 0.0) + (-amt)
    rows = [{"category": c, "amount": round(v, 2)} for c, v in totals.items()]
    rows.sort(key=lambda r: r["amount"], reverse=True)
    return rows


def income_vs_expense(db):
    """Per-month income and expense totals, oldest month first.

    Returns ``[{"month": "YYYY-MM", "income": float, "expense": float,
    "net": float}, ...]``.
    """
    buckets = {}
    for t in db.list_transactions():
        m = _month_of(t["date"])
        if not m:
            continue
        b = buckets.setdefault(m, {"income": 0.0, "expense": 0.0})
        amt = float(t["amount"])
        if amt >= 0:
            b["income"] += amt
        else:
            b["expense"] += -amt
    out = []
    for m in sorted(buckets):
        inc = round(buckets[m]["income"], 2)
        exp = round(buckets[m]["expense"], 2)
        out.append({"month": m, "income": inc, "expense": exp,
                    "net": round(inc - exp, 2)})
    return out


def cash_flow(db, month=None):
    """Overall inflow/outflow/net for a month (or all time)."""
    inflow = outflow = 0.0
    for t in db.list_transactions(month=month):
        amt = float(t["amount"])
        if amt >= 0:
            inflow += amt
        else:
            outflow += -amt
    return {
        "inflow": round(inflow, 2),
        "outflow": round(outflow, 2),
        "net": round(inflow - outflow, 2),
    }


def net_worth(db):
    """Cumulative net worth at the end of each month.

    Opening balances of all accounts seed the running total, then each month's
    net cash flow is added, giving ``[{"month": "YYYY-MM", "net_worth": float},
    ...]`` oldest first.
    """
    opening = sum(float(a["opening"]) for a in db.list_accounts())
    monthly = income_vs_expense(db)
    running = opening
    out = []
    for row in monthly:
        running += row["net"]
        out.append({"month": row["month"], "net_worth": round(running, 2)})
    if not out and opening:
        # no transactions yet, but accounts have balances
        out.append({"month": "", "net_worth": round(opening, 2)})
    return out


# ---------------------------------------------------------------------------
# Chart rendering (Agg -> PNG)
# ---------------------------------------------------------------------------
def render_chart(kind, data, out_png, title=None, theme=None):
    """Render *data* as a PNG at *out_png* and return the path.

    *kind* is one of ``spending`` (pie), ``cashflow`` (grouped bars of income
    vs expense per month), ``networth`` (line) or ``budget`` (horizontal
    budget-vs-actual bars).  *data* is the matching structure from this module
    (or :mod:`budgetbook.budget` for ``budget``).  Always uses the Agg backend.

    ``theme`` may be ``"light"`` or ``"dark"`` to paint the figure in the Aura
    palette so the chart never sits as an unthemed white rectangle inside the
    GUI; ``None`` (the default) keeps the classic white look for exports and
    existing callers.
    """
    import matplotlib.pyplot as plt  # safe: backend already forced to Agg

    parent = os.path.dirname(os.path.abspath(out_png))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    if theme in ("light", "dark"):
        dark = theme == "dark"
        bg = "#14171c" if dark else "#ffffff"       # Aura surface tones
        fg = "#e8ecf5" if dark else "#232838"
        plt.rcParams.update({
            "figure.facecolor": bg, "axes.facecolor": bg,
            "savefig.facecolor": bg, "text.color": fg,
            "axes.labelcolor": fg, "axes.edgecolor": fg,
            "xtick.color": fg, "ytick.color": fg,
            "legend.facecolor": bg, "legend.edgecolor": fg,
        })
    else:
        plt.rcParams.update(plt.rcParamsDefault)
        matplotlib.use("Agg")                        # keep the forced backend

    fig, ax = plt.subplots(figsize=(7.5, 4.5), dpi=100)
    try:
        if kind == "spending":
            rows = [r for r in (data or []) if r.get("amount", 0) > 0]
            if not rows:
                _empty(ax, "No spending to chart")
            else:
                ax.pie([r["amount"] for r in rows],
                       labels=[r["category"] for r in rows],
                       autopct="%1.0f%%", startangle=90)
                ax.axis("equal")
            ax.set_title(title or "Spending by category")

        elif kind == "cashflow":
            rows = data or []
            if not rows:
                _empty(ax, "No cash-flow data")
            else:
                months = [r["month"] for r in rows]
                idx = range(len(months))
                width = 0.4
                ax.bar([i - width / 2 for i in idx],
                       [r["income"] for r in rows], width, label="Income",
                       color="#1f7a3d")
                ax.bar([i + width / 2 for i in idx],
                       [r["expense"] for r in rows], width, label="Expense",
                       color="#c0392b")
                ax.set_xticks(list(idx))
                ax.set_xticklabels(months, rotation=45, ha="right")
                ax.legend()
                ax.set_ylabel("Amount")
            ax.set_title(title or "Income vs expense")

        elif kind == "networth":
            rows = data or []
            if not rows:
                _empty(ax, "No net-worth data")
            else:
                ax.plot([r["month"] for r in rows],
                        [r["net_worth"] for r in rows],
                        marker="o", color="#2f5fe0")
                ax.set_ylabel("Net worth")
                for label in ax.get_xticklabels():
                    label.set_rotation(45)
                    label.set_ha("right")
                ax.grid(True, alpha=0.3)
            ax.set_title(title or "Net worth over time")

        elif kind == "budget":
            rows = [r for r in (data or []) if r.get("limit") is not None]
            if not rows:
                _empty(ax, "No budgets set")
            else:
                cats = [r["category"] for r in rows]
                idx = range(len(cats))
                ax.barh(list(idx), [r["limit"] for r in rows],
                        color="#d5dae2", label="Limit")
                ax.barh(list(idx), [r["actual"] for r in rows], height=0.5,
                        color=["#c0392b" if r["over"] else "#1f7a3d" for r in rows],
                        label="Actual")
                ax.set_yticks(list(idx))
                ax.set_yticklabels(cats)
                ax.invert_yaxis()
                ax.legend()
            ax.set_title(title or "Budget vs actual")

        else:
            raise BudgetBookError(f"unknown chart kind: {kind!r}")

        fig.tight_layout()
        try:
            fig.savefig(out_png)
        except Exception as exc:
            raise BudgetBookError(f"could not write chart {out_png!r}: {exc}") from exc
    finally:
        plt.close(fig)
    return out_png


def _empty(ax, text):
    ax.text(0.5, 0.5, text, ha="center", va="center", transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


__all__ = [
    "spending_by_category",
    "income_vs_expense",
    "cash_flow",
    "net_worth",
    "render_chart",
]
