"""CRUD coverage for the SQLite store."""

import pytest

from budgetbook.db import Database, transaction_fingerprint
from budgetbook.errors import BudgetBookError


def test_account_crud_and_balance(db):
    db.add_account("Checking", opening=100.0)
    assert db.get_account("Checking")["opening"] == 100.0
    db.add_transaction("2026-01-05", -25.0, payee="Store", account="Checking")
    db.add_transaction("2026-01-06", 50.0, payee="Refund", account="Checking")
    assert db.account_balance("Checking") == 125.0
    assert [a["name"] for a in db.list_accounts()] == ["Checking"]
    db.delete_account("Checking")
    assert db.get_account("Checking") is None


def test_duplicate_account_raises(db):
    db.add_account("Main")
    with pytest.raises(BudgetBookError):
        db.add_account("Main")


def test_category_idempotent(db):
    a = db.add_category("Groceries")
    b = db.add_category("Groceries")
    assert a == b
    assert "Groceries" in db.category_names()


def test_transaction_crud_and_filters(db):
    t1 = db.add_transaction("2026-01-10", -12.0, payee="Cafe", category="Coffee")
    db.add_transaction("2026-02-10", -8.0, payee="Cafe", category="Coffee")
    db.add_transaction("2026-01-15", -40.0, payee="Gas", category="Auto")

    jan = db.list_transactions(month="2026-01")
    assert len(jan) == 2
    coffee = db.list_transactions(category="Coffee")
    assert len(coffee) == 2

    db.update_transaction(t1, amount=-15.0, category="CoffeeShops")
    got = db.get_transaction(t1)
    assert got["amount"] == -15.0
    assert got["category"] == "CoffeeShops"
    # fingerprint kept in sync with new amount
    assert got["fingerprint"] == transaction_fingerprint("2026-01-10", -15.0, "Cafe")

    db.set_cleared(t1, True)
    assert db.get_transaction(t1)["cleared"] == 1

    db.delete_transaction(t1)
    assert db.get_transaction(t1) is None


def test_invalid_amount_raises(db):
    with pytest.raises(BudgetBookError):
        db.add_transaction("2026-01-01", "not-a-number")


def test_budget_upsert(db):
    db.set_budget("Groceries", 300.0)
    db.set_budget("Groceries", 350.0)  # upsert, not duplicate
    rows = db.list_budgets(period="monthly")
    assert len(rows) == 1
    assert rows[0]["limit_amount"] == 350.0
    assert db.get_budget("Groceries")["limit_amount"] == 350.0
    db.delete_budget("Groceries")
    assert db.get_budget("Groceries") is None
