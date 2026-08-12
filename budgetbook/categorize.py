"""Rule-based auto-categorization.

A *rule* maps a payee pattern to a category.  Rules are either a plain
substring match (case-insensitive) or a regular expression (``is_regex=True``).
Rules live in a small ``rules`` table created on demand in the same database.

Typical flow::

    add_rule(db, "starbucks", "Coffee")
    apply_rules(db)                 # fill in blank categories
    learn_rule(db, txn_id, "Coffee")  # turn a manual assignment into a rule
"""

from __future__ import annotations

import re

from .errors import BudgetBookError


def _ensure_table(db):
    db.conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rules (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern  TEXT NOT NULL,
            category TEXT NOT NULL,
            is_regex INTEGER DEFAULT 0,
            UNIQUE(pattern, is_regex)
        )
        """
    )
    db.conn.commit()


def add_rule(db, pattern, category, is_regex=False):
    """Create (or update) a payee->category rule; returns the rule id."""
    pattern = (pattern or "").strip()
    category = (category or "").strip()
    if not pattern:
        raise BudgetBookError("rule pattern is required")
    if not category:
        raise BudgetBookError("rule category is required")
    if is_regex:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise BudgetBookError(f"invalid regex {pattern!r}: {exc}") from exc
    _ensure_table(db)
    db.conn.execute(
        "INSERT INTO rules(pattern, category, is_regex) VALUES (?,?,?)"
        " ON CONFLICT(pattern, is_regex) DO UPDATE SET category=excluded.category",
        (pattern, category, 1 if is_regex else 0),
    )
    db.conn.commit()
    row = db.conn.execute(
        "SELECT id FROM rules WHERE pattern=? AND is_regex=?",
        (pattern, 1 if is_regex else 0)).fetchone()
    return row["id"] if row else None


def list_rules(db):
    _ensure_table(db)
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM rules ORDER BY id")]


def delete_rule(db, rule_id):
    _ensure_table(db)
    db.conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    db.conn.commit()


def match_category(payee, rules):
    """Return the category for *payee* from *rules*, or ``None``.

    *rules* is a list of dicts (as from :func:`list_rules`).  Substring rules
    match case-insensitively; the first matching rule wins.
    """
    text = (payee or "").lower()
    for rule in rules:
        pat = rule["pattern"]
        if rule.get("is_regex"):
            try:
                if re.search(pat, payee or "", re.IGNORECASE):
                    return rule["category"]
            except re.error:
                continue
        elif pat.lower() in text:
            return rule["category"]
    return None


def apply_rules(db, only_uncategorized=True):
    """Apply every stored rule to matching transactions.

    By default only transactions with a blank category are touched.  Returns the
    number of transactions updated.
    """
    rules = list_rules(db)
    if not rules:
        return 0
    txns = db.list_transactions()
    updated = 0
    for txn in txns:
        if only_uncategorized and (txn.get("category") or "").strip():
            continue
        cat = match_category(txn.get("payee", ""), rules)
        if cat and cat != txn.get("category"):
            db.update_transaction(txn["id"], category=cat)
            updated += 1
    return updated


def learn_rule(db, txn_id, category=None):
    """Create a rule from a transaction's payee -> its (or given) category.

    Useful right after a manual category assignment in the GUI: the payee
    becomes a substring rule so similar future transactions auto-categorize.
    """
    txn = db.get_transaction(txn_id)
    if txn is None:
        raise BudgetBookError(f"no transaction with id {txn_id}")
    payee = (txn.get("payee") or "").strip()
    if not payee:
        raise BudgetBookError("cannot learn a rule from a transaction with no payee")
    category = (category or txn.get("category") or "").strip()
    if not category:
        raise BudgetBookError("no category to learn (assign one first)")
    return add_rule(db, payee, category, is_regex=False)


__all__ = [
    "add_rule",
    "list_rules",
    "delete_rule",
    "match_category",
    "apply_rules",
    "learn_rule",
]
