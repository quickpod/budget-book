"""Command-line interface: ``python -m budgetbook <command> ...``.

Subcommands: ``import``, ``add``, ``list``, ``budget``, ``report``,
``categorize`` and ``rule``.  A global ``--db PATH`` selects the database
(defaults to the per-user location).  Every command exits cleanly (status 1,
message on stderr) on :class:`BudgetBookError`.
"""

from __future__ import annotations

import argparse
import sys

from . import (
    BudgetBookError,
    Database,
    add_rule,
    apply_rules,
    budget_vs_actual,
    cash_flow,
    default_db_path,
    detect_mapping,
    import_csv,
    import_ofx,
    list_rules,
    net_worth,
    render_chart,
    spending_by_category,
)


def _open_db(a):
    return Database(a.db)


def _parse_map(pairs):
    """Turn ``--map date=Date amount=Amount`` items into a mapping dict."""
    if not pairs:
        return None
    mapping = {}
    for item in pairs:
        if "=" not in item:
            raise BudgetBookError(
                f"bad --map entry {item!r}; expected field=column")
        field, col = item.split("=", 1)
        mapping[field.strip()] = col.strip()
    return mapping


# --- command handlers -------------------------------------------------------
def cmd_import(a):
    db = _open_db(a)
    is_ofx = a.ofx or (not a.csv and a.file.lower().endswith((".ofx", ".qfx")))
    if is_ofx:
        res = import_ofx(db, a.file, account=a.account or "")
    else:
        mapping = _parse_map(a.map)
        res = import_csv(db, a.file, mapping=mapping, account=a.account or "",
                         default_category=a.category or "")
    print(f"Imported {res['added']} new, skipped {res['duplicates']} duplicate(s) "
          f"of {res['total']} row(s).")


def cmd_add(a):
    db = _open_db(a)
    from datetime import date as _date
    when = a.date or _date.today().strftime("%Y-%m-%d")
    from .importer import parse_date
    when = parse_date(when)
    txn_id = db.add_transaction(when, a.amount, payee=a.payee or "",
                                category=a.category or "", account=a.account or "",
                                notes=a.notes or "")
    print(f"Added transaction #{txn_id}: {when} {a.amount:+.2f} "
          f"{a.payee or ''} [{a.category or 'uncategorized'}]")


def cmd_list(a):
    db = _open_db(a)
    txns = db.list_transactions(month=a.month, category=a.category)
    if not txns:
        print("(no transactions)")
        return
    total = 0.0
    for t in txns:
        total += float(t["amount"])
        mark = "C" if t["cleared"] else " "
        print(f"#{t['id']:<5} {t['date']}  {t['amount']:>10.2f} [{mark}] "
              f"{(t['category'] or '-'):<14} {t['payee']}")
    print(f"{'':<7}{'':<11} {total:>10.2f}  ({len(txns)} transactions)")


def cmd_budget(a):
    db = _open_db(a)
    if a.budget_cmd == "set":
        db.set_budget(a.category, a.limit, period=a.period)
        print(f"Budget set: {a.category} <= {a.limit:.2f} ({a.period})")
    elif a.budget_cmd == "list":
        rows = budget_vs_actual(db, month=a.month, period=a.period)
        if not rows:
            print("(no budgets)")
            return
        for r in rows:
            if r["limit"] is None:
                print(f"{r['category']:<16} actual {r['actual']:>10.2f}   (no limit)")
            else:
                flag = "OVER" if r["over"] else "ok"
                print(f"{r['category']:<16} {r['actual']:>10.2f} / "
                      f"{r['limit']:>10.2f}  remaining {r['remaining']:>10.2f}  "
                      f"[{flag}]")


def cmd_report(a):
    db = _open_db(a)
    if a.report_kind == "spending":
        data = spending_by_category(db, month=a.month)
        kind = "spending"
        for r in data:
            print(f"{r['category']:<18} {r['amount']:>12.2f}")
        if not data:
            print("(no spending)")
    elif a.report_kind == "cashflow":
        from .reports import income_vs_expense
        data = income_vs_expense(db)
        kind = "cashflow"
        cf = cash_flow(db, month=a.month)
        print(f"Inflow {cf['inflow']:.2f}  Outflow {cf['outflow']:.2f}  "
              f"Net {cf['net']:+.2f}")
    elif a.report_kind == "networth":
        data = net_worth(db)
        kind = "networth"
        for r in data:
            print(f"{r['month'] or '(start)':<10} {r['net_worth']:>14.2f}")
        if not data:
            print("(no data)")
    else:  # pragma: no cover - argparse guards this
        raise BudgetBookError(f"unknown report: {a.report_kind}")

    if a.out:
        render_chart(kind, data, a.out)
        print(f"Chart written to {a.out}")


def cmd_categorize(a):
    db = _open_db(a)
    if a.auto:
        n = apply_rules(db, only_uncategorized=not a.all)
        print(f"Categorized {n} transaction(s).")
    else:
        print("Nothing to do (pass --auto to apply rules).")


def cmd_rule(a):
    db = _open_db(a)
    if a.rule_cmd == "add":
        rid = add_rule(db, a.pattern, a.category, is_regex=a.regex)
        print(f"Rule #{rid}: {a.pattern!r} -> {a.category}"
              f"{' (regex)' if a.regex else ''}")
    elif a.rule_cmd == "list":
        rules = list_rules(db)
        if not rules:
            print("(no rules)")
            return
        for r in rules:
            kind = "regex" if r["is_regex"] else "text"
            print(f"#{r['id']:<4} [{kind}] {r['pattern']!r} -> {r['category']}")


# --- parser -----------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="budgetbook",
        description="Offline personal-finance CLI: import statements, budget, "
        "categorize and report. Amounts are signed (expenses negative).",
    )
    p.add_argument("--db", default=default_db_path(),
                   help="database file (default: per-user BudgetBook DB)")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name, help, handler):
        sp = sub.add_parser(name, help=help)
        sp.set_defaults(func=handler)
        return sp

    s = add("import", "Import a CSV or OFX/QFX statement", cmd_import)
    s.add_argument("file")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--csv", action="store_true", help="force CSV parsing")
    g.add_argument("--ofx", action="store_true", help="force OFX/QFX parsing")
    s.add_argument("--map", nargs="+", metavar="FIELD=COLUMN",
                   help="explicit CSV column mapping, e.g. date=Date amount=Amount")
    s.add_argument("--account", help="assign imported rows to this account")
    s.add_argument("--category", help="default category for imported rows")

    s = add("add", "Add a single transaction", cmd_add)
    s.add_argument("amount", type=float, help="signed amount (expense negative)")
    s.add_argument("--payee", help="payee / description")
    s.add_argument("--category", help="category")
    s.add_argument("--account", help="account")
    s.add_argument("--date", help="date (many formats; default today)")
    s.add_argument("--notes", help="free-text notes")

    s = add("list", "List transactions", cmd_list)
    s.add_argument("--month", help="filter by YYYY-MM")
    s.add_argument("--category", help="filter by category")

    s = add("budget", "Set or list budgets", cmd_budget)
    bsub = s.add_subparsers(dest="budget_cmd", required=True)
    bset = bsub.add_parser("set", help="set a category limit")
    bset.add_argument("category")
    bset.add_argument("limit", type=float)
    bset.add_argument("--period", default="monthly")
    blist = bsub.add_parser("list", help="budget vs actual")
    blist.add_argument("--month", help="actuals for this YYYY-MM")
    blist.add_argument("--period", default="monthly")

    s = add("report", "Spending / cash-flow / net-worth reports", cmd_report)
    s.add_argument("report_kind", choices=["spending", "cashflow", "networth"])
    s.add_argument("--month", help="filter by YYYY-MM (spending/cashflow)")
    s.add_argument("--out", help="also render a PNG chart to this path")

    s = add("categorize", "Apply auto-categorization rules", cmd_categorize)
    s.add_argument("--auto", action="store_true", help="apply stored rules")
    s.add_argument("--all", action="store_true",
                   help="re-categorize even already-categorized transactions")

    s = add("rule", "Manage auto-categorization rules", cmd_rule)
    rsub = s.add_subparsers(dest="rule_cmd", required=True)
    radd = rsub.add_parser("add", help="add a payee->category rule")
    radd.add_argument("pattern")
    radd.add_argument("category")
    radd.add_argument("--regex", action="store_true", help="treat pattern as regex")
    rsub.add_parser("list", help="list rules")

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except BudgetBookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
