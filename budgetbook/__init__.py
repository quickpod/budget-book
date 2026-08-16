"""budgetbook -- a permissively-licensed personal-finance library.

Public API around a single SQLite store: import bank CSV/OFX files, auto-
categorize by payee rules, set category budgets, and produce spending / cash-
flow / net-worth reports and charts.  Every public function raises
:class:`BudgetBookError` (and only that) on failure, so the CLI and the tkinter
GUI have one exception to catch.

    from budgetbook import Database, import_csv, budget_vs_actual
    db = Database("budget.db")
    import_csv(db, "statement.csv")

The GUI in :mod:`budgetbook.gui` builds on this module; importing it does
nothing until :func:`budgetbook.gui.main` is called.
"""

from __future__ import annotations

from .errors import BudgetBookError
from .db import Database, default_db_path, transaction_fingerprint
from .importer import (
    import_csv,
    import_ofx,
    detect_mapping,
    read_csv_rows,
    parse_date,
    parse_amount,
)
from .categorize import (
    add_rule,
    list_rules,
    delete_rule,
    match_category,
    apply_rules,
    learn_rule,
)
from .budget import actual_spending, budget_vs_actual, totals
from .reports import (
    spending_by_category,
    income_vs_expense,
    cash_flow,
    net_worth,
    render_chart,
)

__version__ = "1.1.0"

__all__ = [
    "BudgetBookError",
    "Database",
    "default_db_path",
    "transaction_fingerprint",
    "import_csv",
    "import_ofx",
    "detect_mapping",
    "read_csv_rows",
    "parse_date",
    "parse_amount",
    "add_rule",
    "list_rules",
    "delete_rule",
    "match_category",
    "apply_rules",
    "learn_rule",
    "actual_spending",
    "budget_vs_actual",
    "totals",
    "spending_by_category",
    "income_vs_expense",
    "cash_flow",
    "net_worth",
    "render_chart",
]
