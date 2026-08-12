"""SQLite-backed store for BudgetBook.

A single :class:`Database` wraps one SQLite file and exposes plain CRUD for the
four tables the app needs:

* ``accounts``      -- name, type, opening balance
* ``transactions``  -- date, payee, signed amount, category, account, notes,
  cleared flag, and a dedupe ``fingerprint``
* ``categories``    -- name + kind (income/expense)
* ``budgets``       -- category, period (``YYYY-MM`` or ``monthly``) and a limit

Amounts are always **signed**: expenses negative, income positive.  Dates are
stored as ISO ``YYYY-MM-DD`` strings so lexical ordering is chronological.

The default location is ``%LOCALAPPDATA%\\BudgetBook\\budgetbook.db`` on Windows
(``~/.budgetbook/budgetbook.db`` elsewhere), but any path -- including
``":memory:"`` -- may be passed in, which is what the tests use.
"""

from __future__ import annotations

import os
import sqlite3

from .errors import BudgetBookError
from .guiconfig import config_dir

DEFAULT_DB_NAME = "budgetbook.db"


def default_db_path():
    r"""Return the on-disk DB path (``%LOCALAPPDATA%\BudgetBook\budgetbook.db``)."""
    return os.path.join(config_dir(), DEFAULT_DB_NAME)


def transaction_fingerprint(date, amount, payee):
    """Stable dedupe key for a transaction: ``date|amount(2dp)|payee(lower)``."""
    try:
        amt = f"{float(amount):.2f}"
    except (TypeError, ValueError):
        amt = str(amount)
    return f"{str(date).strip()}|{amt}|{str(payee or '').strip().lower()}"


class Database:
    """A thin, well-typed wrapper around a single SQLite connection."""

    def __init__(self, path=None):
        self.path = path or default_db_path()
        if self.path != ":memory:":
            parent = os.path.dirname(os.path.abspath(self.path))
            if parent and not os.path.isdir(parent):
                try:
                    os.makedirs(parent, exist_ok=True)
                except OSError as exc:  # pragma: no cover - fs failure
                    raise BudgetBookError(
                        f"could not create data directory {parent!r}: {exc}"
                    ) from exc
        try:
            self.conn = sqlite3.connect(self.path)
        except sqlite3.Error as exc:
            raise BudgetBookError(f"could not open database {self.path!r}: {exc}") from exc
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    # -- lifecycle ---------------------------------------------------------
    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _create_schema(self):
        cur = self.conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                name    TEXT NOT NULL UNIQUE,
                type    TEXT DEFAULT 'checking',
                opening REAL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                kind TEXT DEFAULT 'expense'
            );
            CREATE TABLE IF NOT EXISTS transactions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                payee       TEXT DEFAULT '',
                amount      REAL NOT NULL,
                category    TEXT DEFAULT '',
                account     TEXT DEFAULT '',
                notes       TEXT DEFAULT '',
                cleared     INTEGER DEFAULT 0,
                fingerprint TEXT
            );
            CREATE TABLE IF NOT EXISTS budgets (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                period   TEXT NOT NULL DEFAULT 'monthly',
                limit_amount REAL NOT NULL DEFAULT 0.0,
                UNIQUE(category, period)
            );
            CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(date);
            CREATE INDEX IF NOT EXISTS ix_txn_fp   ON transactions(fingerprint);
            """
        )
        self.conn.commit()

    # =====================================================================
    # Accounts
    # =====================================================================
    def add_account(self, name, type="checking", opening=0.0):
        name = (name or "").strip()
        if not name:
            raise BudgetBookError("account name is required")
        try:
            cur = self.conn.execute(
                "INSERT INTO accounts(name, type, opening) VALUES (?,?,?)",
                (name, type, float(opening or 0.0)),
            )
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError as exc:
            raise BudgetBookError(f"account {name!r} already exists") from exc

    def list_accounts(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM accounts ORDER BY name")]

    def get_account(self, name):
        row = self.conn.execute(
            "SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def delete_account(self, name):
        self.conn.execute("DELETE FROM accounts WHERE name=?", (name,))
        self.conn.commit()

    def account_balance(self, name):
        """Opening balance + sum of the account's transactions."""
        acct = self.get_account(name)
        opening = acct["opening"] if acct else 0.0
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM transactions WHERE account=?",
            (name,)).fetchone()
        return float(opening) + float(row["s"])

    # =====================================================================
    # Categories
    # =====================================================================
    def add_category(self, name, kind="expense"):
        name = (name or "").strip()
        if not name:
            raise BudgetBookError("category name is required")
        if kind not in ("expense", "income"):
            raise BudgetBookError("category kind must be 'expense' or 'income'")
        try:
            cur = self.conn.execute(
                "INSERT INTO categories(name, kind) VALUES (?,?)", (name, kind))
            self.conn.commit()
            return cur.lastrowid
        except sqlite3.IntegrityError:
            # already present -- treat as idempotent, return existing id
            row = self.conn.execute(
                "SELECT id FROM categories WHERE name=?", (name,)).fetchone()
            return row["id"] if row else None

    def list_categories(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM categories ORDER BY name")]

    def category_names(self):
        return [r["name"] for r in self.list_categories()]

    def delete_category(self, name):
        self.conn.execute("DELETE FROM categories WHERE name=?", (name,))
        self.conn.commit()

    # =====================================================================
    # Transactions
    # =====================================================================
    def add_transaction(self, date, amount, payee="", category="",
                         account="", notes="", cleared=0, fingerprint=None):
        date = (date or "").strip()
        if not date:
            raise BudgetBookError("transaction date is required")
        try:
            amount = float(amount)
        except (TypeError, ValueError) as exc:
            raise BudgetBookError(f"invalid amount: {amount!r}") from exc
        fp = fingerprint or transaction_fingerprint(date, amount, payee)
        cur = self.conn.execute(
            "INSERT INTO transactions(date, payee, amount, category, account,"
            " notes, cleared, fingerprint) VALUES (?,?,?,?,?,?,?,?)",
            (date, payee or "", amount, category or "", account or "",
             notes or "", 1 if cleared else 0, fp),
        )
        self.conn.commit()
        return cur.lastrowid

    def fingerprint_exists(self, fingerprint):
        row = self.conn.execute(
            "SELECT 1 FROM transactions WHERE fingerprint=? LIMIT 1",
            (fingerprint,)).fetchone()
        return row is not None

    def get_transaction(self, txn_id):
        row = self.conn.execute(
            "SELECT * FROM transactions WHERE id=?", (txn_id,)).fetchone()
        return dict(row) if row else None

    def update_transaction(self, txn_id, **fields):
        allowed = {"date", "payee", "amount", "category", "account",
                   "notes", "cleared"}
        sets, vals = [], []
        for key, val in fields.items():
            if key not in allowed:
                raise BudgetBookError(f"unknown transaction field: {key!r}")
            if key == "amount":
                val = float(val)
            if key == "cleared":
                val = 1 if val else 0
            sets.append(f"{key}=?")
            vals.append(val)
        if not sets:
            return
        # keep the dedupe fingerprint in sync when date/amount/payee change
        cur = self.get_transaction(txn_id)
        if cur is None:
            raise BudgetBookError(f"no transaction with id {txn_id}")
        merged = {**cur, **fields}
        sets.append("fingerprint=?")
        vals.append(transaction_fingerprint(
            merged["date"], merged["amount"], merged["payee"]))
        vals.append(txn_id)
        self.conn.execute(
            f"UPDATE transactions SET {', '.join(sets)} WHERE id=?", vals)
        self.conn.commit()

    def set_cleared(self, txn_id, cleared=True):
        self.conn.execute("UPDATE transactions SET cleared=? WHERE id=?",
                          (1 if cleared else 0, txn_id))
        self.conn.commit()

    def delete_transaction(self, txn_id):
        self.conn.execute("DELETE FROM transactions WHERE id=?", (txn_id,))
        self.conn.commit()

    def list_transactions(self, month=None, category=None, account=None,
                          order="date"):
        """Return transactions, newest fields first, filtered as requested.

        *month* is a ``YYYY-MM`` prefix match on the date; *category* and
        *account* are exact matches.
        """
        clauses, params = [], []
        if month:
            clauses.append("date LIKE ?")
            params.append(f"{month}%")
        if category is not None:
            clauses.append("category=?")
            params.append(category)
        if account is not None:
            clauses.append("account=?")
            params.append(account)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order_col = "date" if order == "date" else "id"
        rows = self.conn.execute(
            f"SELECT * FROM transactions{where} ORDER BY {order_col}, id",
            params).fetchall()
        return [dict(r) for r in rows]

    # =====================================================================
    # Budgets
    # =====================================================================
    def set_budget(self, category, limit_amount, period="monthly"):
        category = (category or "").strip()
        if not category:
            raise BudgetBookError("budget category is required")
        try:
            limit_amount = float(limit_amount)
        except (TypeError, ValueError) as exc:
            raise BudgetBookError(f"invalid budget limit: {limit_amount!r}") from exc
        self.conn.execute(
            "INSERT INTO budgets(category, period, limit_amount) VALUES (?,?,?)"
            " ON CONFLICT(category, period) DO UPDATE SET limit_amount=excluded.limit_amount",
            (category, period, limit_amount),
        )
        self.conn.commit()

    def list_budgets(self, period=None):
        if period:
            rows = self.conn.execute(
                "SELECT * FROM budgets WHERE period=? ORDER BY category",
                (period,))
        else:
            rows = self.conn.execute(
                "SELECT * FROM budgets ORDER BY category, period")
        return [dict(r) for r in rows]

    def get_budget(self, category, period="monthly"):
        row = self.conn.execute(
            "SELECT * FROM budgets WHERE category=? AND period=?",
            (category, period)).fetchone()
        return dict(row) if row else None

    def delete_budget(self, category, period="monthly"):
        self.conn.execute("DELETE FROM budgets WHERE category=? AND period=?",
                          (category, period))
        self.conn.commit()


__all__ = [
    "Database",
    "default_db_path",
    "transaction_fingerprint",
    "BudgetBookError",
]
