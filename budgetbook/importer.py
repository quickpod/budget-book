"""Import transactions into a :class:`~budgetbook.db.Database`.

Two entry points:

* :func:`import_csv` -- flexible column mapping (date / amount / payee /
  category / notes, or a debit + credit pair) with automatic date-format
  detection.
* :func:`import_ofx` -- parse an OFX/QFX file via :mod:`ofxparse`.

Both dedupe against what is already stored (and within the same batch) using the
``(date, amount, payee)`` fingerprint from :mod:`budgetbook.db`, and both return
a small summary dict ``{"added": int, "duplicates": int, "total": int}``.
"""

from __future__ import annotations

import csv
import datetime as _dt
import os

from .db import transaction_fingerprint
from .errors import BudgetBookError

# Logical field -> the header aliases we will auto-detect when no mapping given.
_ALIASES = {
    "date": ("date", "transaction date", "posted", "posting date", "trans date"),
    "amount": ("amount", "amt", "value"),
    "payee": ("payee", "description", "name", "merchant", "memo", "details"),
    "category": ("category", "cat"),
    "notes": ("notes", "note", "memo"),
    "debit": ("debit", "withdrawal", "withdrawals", "money out", "paid out"),
    "credit": ("credit", "deposit", "deposits", "money in", "paid in"),
    "account": ("account", "account name"),
}

_DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%d-%m-%Y",
    "%m/%d/%y", "%d/%m/%y", "%b %d, %Y", "%d %b %Y", "%Y%m%d", "%d.%m.%Y",
)


def parse_date(value):
    """Parse a date string into ISO ``YYYY-MM-DD``; raise on failure."""
    if value is None:
        raise BudgetBookError("missing date value")
    s = str(value).strip()
    if not s:
        raise BudgetBookError("empty date value")
    # OFX-style compact timestamps (YYYYMMDDHHMMSS...) -> take the date part.
    if s.isdigit() and len(s) >= 8:
        s = s[:8]
    for fmt in _DATE_FORMATS:
        try:
            return _dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # last resort: ISO-ish parse
    try:
        return _dt.date.fromisoformat(s[:10]).strftime("%Y-%m-%d")
    except ValueError as exc:
        raise BudgetBookError(f"unrecognised date format: {value!r}") from exc


def parse_amount(value):
    """Parse a money string into a float; tolerant of $, commas and ()."""
    if value is None or str(value).strip() == "":
        return None
    s = str(value).strip()
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    s = s.replace("$", "").replace(",", "").replace("£", "").replace(
        "€", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        amt = float(s)
    except ValueError as exc:
        raise BudgetBookError(f"invalid amount: {value!r}") from exc
    return -amt if negative else amt


def _norm(header):
    return (header or "").strip().lower()


def detect_mapping(headers):
    """Guess a logical->header mapping from a CSV's header row."""
    lookup = {_norm(h): h for h in headers}
    mapping = {}
    for field, aliases in _ALIASES.items():
        for alias in aliases:
            if alias in lookup:
                mapping[field] = lookup[alias]
                break
    if "date" not in mapping:
        raise BudgetBookError(
            "could not find a date column; pass an explicit mapping "
            f"(headers were: {', '.join(headers)})")
    if "amount" not in mapping and not ("debit" in mapping or "credit" in mapping):
        raise BudgetBookError(
            "could not find an amount (or debit/credit) column; pass a mapping")
    return mapping


def _row_amount(row, mapping):
    """Compute the signed amount for a CSV row given the mapping."""
    if mapping.get("amount"):
        amt = parse_amount(row.get(mapping["amount"]))
        if amt is None:
            return None
        return amt
    debit = parse_amount(row.get(mapping.get("debit", ""))) if mapping.get("debit") else None
    credit = parse_amount(row.get(mapping.get("credit", ""))) if mapping.get("credit") else None
    if debit is None and credit is None:
        return None
    # credit is money in (positive), debit is money out (negative)
    return (credit or 0.0) - abs(debit or 0.0)


def read_csv_rows(path):
    """Return ``(headers, rows)`` for *path*; rows are dicts keyed by header."""
    if not os.path.exists(path):
        raise BudgetBookError(f"file not found: {path}")
    try:
        with open(path, "r", newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            rows = [dict(r) for r in reader]
    except OSError as exc:
        raise BudgetBookError(f"could not read {path!r}: {exc}") from exc
    if not headers:
        raise BudgetBookError(f"no header row found in {path}")
    return headers, rows


def import_csv(db, path, mapping=None, account="", default_category=""):
    """Import a delimited file into *db*.

    *mapping* maps logical fields (``date``, ``amount`` or ``debit``/``credit``,
    ``payee``, ``category``, ``notes``, ``account``) to column names; when it is
    ``None`` the columns are auto-detected from the header row.  Rows already in
    the DB (by fingerprint) are counted as duplicates and skipped.
    """
    headers, rows = read_csv_rows(path)
    if mapping is None:
        mapping = detect_mapping(headers)
    if "date" not in mapping:
        raise BudgetBookError("mapping must include a 'date' column")

    added = duplicates = 0
    seen = set()
    for row in rows:
        raw_date = row.get(mapping["date"])
        if raw_date is None or str(raw_date).strip() == "":
            continue  # skip blank lines
        date = parse_date(raw_date)
        amount = _row_amount(row, mapping)
        if amount is None:
            continue  # a row with no monetary value is not a transaction
        payee = (row.get(mapping.get("payee", "")) or "").strip() if mapping.get("payee") else ""
        category = default_category
        if mapping.get("category"):
            category = (row.get(mapping["category"]) or "").strip() or default_category
        notes = (row.get(mapping.get("notes", "")) or "").strip() if mapping.get("notes") else ""
        acct = account
        if mapping.get("account"):
            acct = (row.get(mapping["account"]) or "").strip() or account

        fp = transaction_fingerprint(date, amount, payee)
        if fp in seen or db.fingerprint_exists(fp):
            duplicates += 1
            seen.add(fp)
            continue
        db.add_transaction(date, amount, payee=payee, category=category,
                           account=acct, notes=notes, fingerprint=fp)
        seen.add(fp)
        added += 1
    return {"added": added, "duplicates": duplicates, "total": added + duplicates}


def import_ofx(db, path, account=""):
    """Import an OFX/QFX file into *db* using :mod:`ofxparse`."""
    if not os.path.exists(path):
        raise BudgetBookError(f"file not found: {path}")
    try:
        from ofxparse import OfxParser
    except Exception as exc:  # pragma: no cover - dependency missing
        raise BudgetBookError(f"ofxparse is required to import OFX files: {exc}") from exc
    try:
        with open(path, "rb") as fh:
            ofx = OfxParser.parse(fh)
    except Exception as exc:
        raise BudgetBookError(f"could not parse OFX file {path!r}: {exc}") from exc

    added = duplicates = 0
    seen = set()
    accounts = getattr(ofx, "accounts", None) or (
        [ofx.account] if getattr(ofx, "account", None) else [])
    for acc in accounts:
        acct_name = account or getattr(acc, "account_id", "") or ""
        statement = getattr(acc, "statement", None)
        if statement is None:
            continue
        for txn in getattr(statement, "transactions", []) or []:
            date = parse_date(getattr(txn, "date", None))
            try:
                amount = float(txn.amount)
            except (TypeError, ValueError) as exc:
                raise BudgetBookError(
                    f"invalid amount in OFX transaction: {txn.amount!r}") from exc
            payee = (getattr(txn, "payee", "") or getattr(txn, "memo", "") or "").strip()
            notes = (getattr(txn, "memo", "") or "").strip()
            fp = transaction_fingerprint(date, amount, payee)
            if fp in seen or db.fingerprint_exists(fp):
                duplicates += 1
                seen.add(fp)
                continue
            db.add_transaction(date, amount, payee=payee, account=acct_name,
                               notes=notes, fingerprint=fp)
            seen.add(fp)
            added += 1
    return {"added": added, "duplicates": duplicates, "total": added + duplicates}


__all__ = [
    "import_csv",
    "import_ofx",
    "detect_mapping",
    "read_csv_rows",
    "parse_date",
    "parse_amount",
]
