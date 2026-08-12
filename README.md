# BudgetBook

A fast, **offline**, **100% open-source** personal finance & budgeting tool for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/budget-book).

> **100% AI-built and open source.** Apache-2.0.

## What it does

Track income and expenses, set category budgets, and import transactions from bank CSV or OFX/QFX files with auto-categorization rules. Visualize spending trends, cash flow and net worth with charts, and see budget-vs-actual at a glance. All data is stored locally — a private money manager.

## Install

Download **`BudgetBook-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/budget-book) or the [GitHub release](https://github.com/quickpod/budget-book/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
pip install -r requirements.txt
python budget_book_app.py          # GUI
python -m budgetbook --help    # CLI
```


## Features

- **Local SQLite store** — accounts, transactions (signed amounts, categories, cleared flag, notes), categories and budgets, at `%LOCALAPPDATA%\BudgetBook\budgetbook.db` (path overridable).
- **Import CSV & OFX/QFX** — flexible column mapping with auto-detection, debit/credit columns, and automatic date-format detection. Duplicate transactions are detected by a `(date, amount, payee)` fingerprint and skipped, with an added/duplicate report.
- **Auto-categorization rules** — map a payee substring (or regex) to a category, apply rules in bulk, or learn a rule from a manual assignment.
- **Budgets** — set a spending limit per category and see budget-vs-actual with over/under and remaining, per month or overall.
- **Reports & charts** — spending by category, income vs expense over time, cash flow and net worth. Charts render via matplotlib's headless Agg backend to PNG for both in-app display and export.
- **Desktop GUI** — a pure-stdlib tkinter app: Transactions, Import (map → preview → import), Budgets, Reports and Rules, with light/dark themes and threaded imports/charting so the UI stays responsive. Fully offline.

## CLI examples

```sh
# Import a bank CSV (columns auto-detected) or an OFX/QFX file
python -m budgetbook import statement.csv
python -m budgetbook import statement.csv --map date=Date amount=Amount payee=Description
python -m budgetbook import export.qfx --ofx --account Checking

# Add a single transaction (expenses are negative)
python -m budgetbook add -42.50 --payee "Grocery Mart" --category Groceries --date 2026-01-07

# List transactions, optionally filtered
python -m budgetbook list --month 2026-01 --category Groceries

# Budgets
python -m budgetbook budget set Groceries 400
python -m budgetbook budget list --month 2026-01

# Auto-categorization rules
python -m budgetbook rule add "starbucks" Coffee
python -m budgetbook categorize --auto

# Reports (optionally export a PNG chart)
python -m budgetbook report spending --month 2026-01 --out spending.png
python -m budgetbook report cashflow
python -m budgetbook report networth --out networth.png

# Use an explicit database file
python -m budgetbook --db mybudget.db list
```

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
