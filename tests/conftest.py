"""Shared test fixtures. Forces the matplotlib Agg backend before pyplot."""

import matplotlib

matplotlib.use("Agg")  # headless: never needs a display

import pytest  # noqa: E402

from budgetbook.db import Database  # noqa: E402


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()
