"""Error types for budgetbook."""


class BudgetBookError(Exception):
    """Raised for any recoverable failure in a budgetbook operation.

    All public functions raise this (and only this) on failure so callers
    -- including the CLI and the tkinter GUI -- have a single exception to
    catch and can show a clean message instead of a traceback.
    """
