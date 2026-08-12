#!/usr/bin/env python3
r"""BudgetBook -- an Aura (QuickOpen design system) GUI on top of the
``budgetbook`` API.

A single Aura window: the sidebar navigates five sections (Transactions,
Import, Budgets, Reports, Rules) and the content area swaps to the selected
one.  Every operation calls the tested core library (never re-implements
finance logic).  Slow work -- importing a statement, rendering a chart -- runs
on a background thread and is marshalled back to the UI with ``self.after``;
failures are shown in the Aura status bar as the ``BudgetBookError`` message,
never a raw traceback.

Design goals baked in here (mirrors the QuickOpen house style):
  * built on the vendored ``budgetbook/aura.py`` design system, which layers
    the quickopen.ai look (deep space + light) over CustomTkinter.  Runtime
    deps: ``customtkinter`` (+ ``darkdetect``) -- declared in
    requirements.txt; the PyInstaller build adds
    ``--collect-all customtkinter``.
  * Importing this module does nothing.  Only :func:`main` builds a root
    window, and it degrades gracefully (prints a message, returns 0) with no
    display or with customtkinter missing.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the
    exe directory when ``sys.frozen`` is set -- never ``__file__``.
  * Charts come from :func:`budgetbook.reports.render_chart` (matplotlib Agg
    -> PNG) and are shown via ``tk.PhotoImage``.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

# NOTE: tkinter/customtkinter are imported lazily inside main()/build_app so
# that merely importing this module (e.g. during packaging or on a headless
# CI box) never fails and has no side effects.

APP_NAME = "BudgetBook"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "BudgetBook — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#17914b"      # UI-accent registry (aurakit README): budget-book

CSV_TYPES = [("CSV / OFX / QFX", "*.csv *.ofx *.qfx"), ("All files", "*.*")]
PNG_TYPES = [("PNG image", "*.png"), ("All files", "*.*")]

SECTIONS = [
    ("transactions", "Transactions"),
    ("import", "Import"),
    ("budgets", "Budgets"),
    ("reports", "Reports"),
    ("rules", "Rules"),
]

SECTION_DESC = {
    "transactions": "Add, edit, filter and clear transactions. Amounts are "
                    "signed — expenses negative, income positive.",
    "import": "Import a bank CSV or OFX/QFX file. Map columns, preview, then "
              "import with automatic duplicate detection.",
    "budgets": "Set a spending limit per category and see budget-vs-actual at a "
               "glance.",
    "reports": "Charts for spending, cash flow and net worth — export any as a "
               "PNG.",
    "rules": "Auto-categorization rules: a payee substring (or regex) maps to a "
             "category.",
}


# ---------------------------------------------------------------------------
# Asset / frozen handling
# ---------------------------------------------------------------------------
def asset_path(name):
    """Locate a bundled asset from source OR a PyInstaller one-file build.

    For a frozen exe we look only at ``sys._MEIPASS`` and the executable's own
    directory (never ``__file__``).  From source we also consult the package
    dir, the repo root and the CWD.  Returns an absolute path or ``None``.
    """
    roots = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(meipass)
        roots.append(os.path.dirname(os.path.abspath(sys.executable)))
    else:
        here = os.path.dirname(os.path.abspath(__file__))
        roots += [here, os.path.dirname(here), os.getcwd()]
    for root in roots:
        candidate = os.path.join(root, name)
        if os.path.exists(candidate):
            return candidate
    return None


def open_in_file_manager(path):
    """Best-effort 'reveal in file manager', guarded on every platform."""
    try:
        folder = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        if hasattr(os, "startfile"):          # Windows
            os.startfile(folder)              # noqa: S606 - intended
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", folder])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", folder])
        return True
    except Exception:
        return False


def open_with_default_app(path):
    """Open a file/URL with the OS default application, guarded."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(path)                # noqa: S606
        elif sys.platform == "darwin":
            import subprocess
            subprocess.Popen(["open", path])
        else:
            import subprocess
            subprocess.Popen(["xdg-open", path])
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# The GUI proper -- everything that needs tkinter lives inside build_app().
# ---------------------------------------------------------------------------
def build_app():
    """Build and return the ``App`` class.

    Kept inside a function so this module imports cleanly without a display
    (and without customtkinter installed).
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import customtkinter as ctk

    from . import aura, guiconfig
    from .errors import BudgetBookError
    from .db import Database, default_db_path
    from . import importer, categorize, budget as budgetmod, reports

    class App(aura.AuraApp):
        def __init__(self, db_path=None):
            super().__init__(
                title=WINDOW_TITLE, app_name=APP_NAME, accent=ACCENT,
                theme=guiconfig.get_theme(),
                icon_png=asset_path("budget-book.png"), version=APP_VERSION,
                tagline="offline finance",
                on_theme_change=guiconfig.set_theme,
                size=(1120, 700), min_size=(920, 580))

            self._busy = False
            self._img_refs_gui = []     # keep PhotoImage refs alive
            self._tmpdir = tempfile.mkdtemp(prefix="budgetbook_gui_")
            self._last_report = None    # (kind, data)

            self.db = Database(db_path or default_db_path())

            self._set_icon()
            self._build_menu()
            self.add_section("transactions", "Transactions", "⇄",
                             self._panel_transactions)
            self.add_section("import", "Import", "↧", self._panel_import)
            self.add_section("budgets", "Budgets", "◈", self._panel_budgets)
            self.add_section("reports", "Reports", "▤", self._panel_reports)
            self.add_section("rules", "Rules", "✎", self._panel_rules)
            self.show("transactions")
            self.set_status("Ready")
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("budget-book.ico")
                if ico and os.name == "nt":
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("budget-book.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs_gui.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- section switching: refresh data every time a section shows
        def show(self, sid):
            super().show(sid)
            refresh = getattr(self, "_refresh_" + sid, None)
            if refresh:
                refresh()

        # ---- menu (native menus stay; theme lives in the sidebar toggle too)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Import statement…",
                              command=lambda: self.show("import"))
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About BudgetBook", command=self._about)
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

        # ---- shared section header (caption under the Aura page title)
        def _desc(self, parent, sid):
            aura.Caption(parent, SECTION_DESC.get(sid, ""),
                         wraplength=760, justify="left",
                         anchor="w").pack(anchor="w", pady=(0, 10))

        # ---- background runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            if self._busy:
                self.set_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self.set_status(busy, kind="working")

            def run():
                try:
                    res, err = work(), None
                except BudgetBookError as ex:
                    res, err = None, str(ex)
                except Exception as ex:  # never leak a traceback to the user
                    res, err = None, f"Unexpected error: {ex}"
                self.after(0, lambda: finish(res, err))

            def finish(res, err):
                self._busy = False
                if button is not None:
                    try:
                        button.state(["!disabled"])
                    except Exception:
                        pass
                if err is not None:
                    self.set_error(err)
                    return
                self.set_status("Done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self.set_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # =================================================================
        # SECTION: Transactions
        # =================================================================
        def _panel_transactions(self, parent):
            self._desc(parent, "transactions")
            filt = ctk.CTkFrame(parent, fg_color="transparent")
            filt.pack(fill="x", pady=(0, 10))
            self.tx_month = aura.AuraEntry(filt, placeholder="Month YYYY-MM",
                                           width=130)
            self.tx_month.pack(side="left", padx=(0, 8))
            self.tx_cat_filter = aura.AuraCombo(filt, width=170,
                                                state="readonly", values=[""])
            self.tx_cat_filter.set("")
            self.tx_cat_filter.pack(side="left", padx=(0, 8))
            aura.AuraButton(filt, "Apply filter", kind="secondary",
                            command=self._refresh_transactions).pack(
                side="left", padx=(0, 8))
            aura.AuraButton(filt, "Clear", kind="ghost",
                            command=self._clear_tx_filter).pack(side="left")

            body = ctk.CTkFrame(parent, fg_color="transparent")
            body.pack(fill="both", expand=True)
            cols = ("id", "date", "amount", "category", "account", "cleared",
                    "payee")
            self.tx_tree = ttk.Treeview(body, columns=cols, show="headings",
                                        selectmode="browse", height=8)
            for c, w, anchor in (("id", 46, "e"), ("date", 92, "w"),
                                 ("amount", 96, "e"), ("category", 120, "w"),
                                 ("account", 110, "w"), ("cleared", 82, "center"),
                                 ("payee", 240, "w")):
                self.tx_tree.heading(c, text=aura.spaced(c.title()), anchor="w")
                self.tx_tree.column(c, width=w, anchor=anchor)
            sb = ttk.Scrollbar(body, orient="vertical",
                               command=self.tx_tree.yview)
            self.tx_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.tx_tree.pack(side="left", fill="both", expand=True)
            self.tx_tree.bind("<Double-1>", lambda e: self._edit_tx())

            btns = ctk.CTkFrame(parent, fg_color="transparent")
            btns.pack(fill="x", pady=(10, 0))
            aura.AuraButton(btns, "Add…", kind="primary",
                            command=self._add_tx).pack(side="left")
            aura.AuraButton(btns, "Edit…", kind="secondary",
                            command=self._edit_tx).pack(side="left", padx=8)
            aura.AuraButton(btns, "Toggle cleared", kind="secondary",
                            command=self._toggle_cleared).pack(side="left")
            aura.AuraButton(btns, "Delete", kind="danger",
                            command=self._delete_tx).pack(side="left", padx=8)

        def _clear_tx_filter(self):
            self.tx_month.delete(0, "end")
            self.tx_cat_filter.set("")
            self._refresh_transactions()

        def _refresh_transactions(self):
            if not hasattr(self, "tx_tree"):
                return
            cats = [""] + self.db.category_names()
            self.tx_cat_filter.configure(values=cats)
            month = self.tx_month.get().strip() or None
            cat = self.tx_cat_filter.get().strip() or None
            for row in self.tx_tree.get_children():
                self.tx_tree.delete(row)
            try:
                txns = self.db.list_transactions(month=month, category=cat)
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return
            for t in txns:
                self.tx_tree.insert(
                    "", "end", iid=str(t["id"]),
                    values=(t["id"], t["date"], f"{t['amount']:.2f}",
                            t["category"] or "-", t["account"] or "-",
                            "✓" if t["cleared"] else "", t["payee"]))
            self.set_success(f"{len(txns)} transaction(s).")

        def _selected_tx_id(self):
            sel = self.tx_tree.selection()
            return int(sel[0]) if sel else None

        def _add_tx(self):
            self._tx_dialog(None)

        def _edit_tx(self):
            tid = self._selected_tx_id()
            if tid is None:
                self.set_error("Select a transaction to edit.")
                return
            self._tx_dialog(self.db.get_transaction(tid))

        def _delete_tx(self):
            tid = self._selected_tx_id()
            if tid is None:
                self.set_error("Select a transaction to delete.")
                return
            if messagebox.askyesno("Delete", "Delete this transaction?"):
                self.db.delete_transaction(tid)
                self._refresh_transactions()

        def _toggle_cleared(self):
            tid = self._selected_tx_id()
            if tid is None:
                self.set_error("Select a transaction first.")
                return
            t = self.db.get_transaction(tid)
            self.db.set_cleared(tid, not t["cleared"])
            self._refresh_transactions()

        def _tx_dialog(self, txn):
            win = ctk.CTkToplevel(self)
            win.title("Edit transaction" if txn else "Add transaction")
            win.transient(self)
            frm = ctk.CTkFrame(win, fg_color="transparent")
            frm.pack(fill="both", expand=True, padx=18, pady=16)
            fields = {}

            def field(label, value="", placeholder=""):
                r = ctk.CTkFrame(frm, fg_color="transparent")
                r.pack(fill="x", pady=4)
                ctk.CTkLabel(r, text=label, width=80, anchor="w",
                             font=aura.font()).pack(side="left")
                e = aura.AuraEntry(r, placeholder=placeholder, width=280)
                if value:
                    e.insert(0, value)
                e.pack(side="left", fill="x", expand=True)
                return e

            fields["date"] = field("Date", (txn or {}).get("date", ""),
                                   "YYYY-MM-DD")
            fields["amount"] = field("Amount", str((txn or {}).get("amount", "")),
                                     "-42.50")
            fields["payee"] = field("Payee", (txn or {}).get("payee", ""))
            r = ctk.CTkFrame(frm, fg_color="transparent")
            r.pack(fill="x", pady=4)
            ctk.CTkLabel(r, text="Category", width=80, anchor="w",
                         font=aura.font()).pack(side="left")
            cat_box = aura.AuraCombo(r, width=280,
                                     values=self.db.category_names() or [""])
            cat_box.set((txn or {}).get("category", "") or "")
            cat_box.pack(side="left", fill="x", expand=True)
            fields["category"] = cat_box
            fields["account"] = field("Account", (txn or {}).get("account", ""))
            fields["notes"] = field("Notes", (txn or {}).get("notes", ""))

            err_lbl = aura.Caption(frm, "", wraplength=380, justify="left")
            err_lbl.pack(anchor="w", pady=(6, 0))

            def save():
                try:
                    date = importer.parse_date(fields["date"].get())
                    amount = importer.parse_amount(fields["amount"].get())
                    if amount is None:
                        raise BudgetBookError("amount is required")
                    data = dict(date=date, amount=amount,
                                payee=fields["payee"].get().strip(),
                                category=cat_box.get().strip(),
                                account=fields["account"].get().strip(),
                                notes=fields["notes"].get().strip())
                    if txn:
                        self.db.update_transaction(txn["id"], **data)
                    else:
                        self.db.add_transaction(**data)
                        if data["category"]:
                            self.db.add_category(data["category"])
                except BudgetBookError as ex:
                    err_lbl.configure(text="✕ " + str(ex),
                                      text_color=aura.P("danger"))
                    return
                win.destroy()
                self._refresh_transactions()

            b = ctk.CTkFrame(frm, fg_color="transparent")
            b.pack(fill="x", pady=(12, 0))
            aura.AuraButton(b, "Save", kind="primary",
                            command=save).pack(side="right")
            aura.AuraButton(b, "Cancel", kind="ghost",
                            command=win.destroy).pack(side="right", padx=8)
            win.grab_set()

        # =================================================================
        # SECTION: Import
        # =================================================================
        def _panel_import(self, parent):
            self._desc(parent, "import")
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x")
            self.imp_path = aura.AuraEntry(
                row, placeholder="Statement file (CSV / OFX / QFX)…")
            self.imp_path.pack(side="left", fill="x", expand=True, padx=(0, 8))
            aura.AuraButton(row, "Browse…", kind="secondary",
                            command=self._pick_import).pack(side="left")

            opt = ctk.CTkFrame(parent, fg_color="transparent")
            opt.pack(fill="x", pady=(10, 6))
            self.imp_account = aura.AuraEntry(opt, placeholder="Account name",
                                              width=180)
            self.imp_account.pack(side="left", padx=(0, 10))
            aura.AuraButton(opt, "Detect columns / preview", kind="secondary",
                            command=self._preview_import).pack(side="left")
            self.imp_btn = aura.AuraButton(opt, "Import", kind="primary",
                                           command=self._do_import)
            self.imp_btn.pack(side="left", padx=8)

            self.imp_map_lbl = aura.Caption(parent, "", wraplength=760,
                                            justify="left")
            self.imp_map_lbl.pack(anchor="w", pady=(6, 4))

            self.imp_preview = tk.Text(parent, height=14, wrap="none")
            self.imp_preview.pack(fill="both", expand=True)
            aura.track(self.imp_preview, "text")

        def _pick_import(self):
            path = filedialog.askopenfilename(title="Choose a statement",
                                              filetypes=CSV_TYPES)
            if path:
                self.imp_path.delete(0, "end")
                self.imp_path.insert(0, path)
                guiconfig.add_recent(path)
                self._preview_import()

        def _is_ofx(self, path):
            return path.lower().endswith((".ofx", ".qfx"))

        def _preview_import(self):
            path = self.imp_path.get().strip()
            if not path:
                self.set_error("Choose a file first.")
                return
            self.imp_preview.delete("1.0", "end")
            try:
                if self._is_ofx(path):
                    self.imp_map_lbl.configure(text="OFX/QFX — parsed via ofxparse.")
                    self.imp_preview.insert("end",
                        "OFX/QFX file — transactions will be read directly.\n"
                        "Click Import to load them (duplicates are skipped).\n")
                    return
                headers, rows = importer.read_csv_rows(path)
                mapping = importer.detect_mapping(headers)
                self.imp_map_lbl.configure(
                    text="Detected mapping:  " +
                    ", ".join(f"{k}={v}" for k, v in mapping.items()))
                self.imp_preview.insert("end", " | ".join(headers) + "\n")
                self.imp_preview.insert("end", "-" * 60 + "\n")
                for r in rows[:12]:
                    self.imp_preview.insert(
                        "end", " | ".join(str(r.get(h, "")) for h in headers) + "\n")
                self.set_success(f"{len(rows)} row(s) ready to import.")
            except BudgetBookError as ex:
                self.imp_map_lbl.configure(text="")
                self.set_error(str(ex))

        def _do_import(self):
            path = self.imp_path.get().strip()
            if not path:
                self.set_error("Choose a file first.")
                return
            account = self.imp_account.get().strip()
            is_ofx = self._is_ofx(path)
            db_path = self.db.path

            def work():
                # sqlite connections are single-thread: open a worker-thread
                # connection to the same file (self.db sees the commits).
                wdb = Database(db_path)
                try:
                    if is_ofx:
                        return importer.import_ofx(wdb, path, account=account)
                    return importer.import_csv(wdb, path, account=account)
                finally:
                    wdb.close()

            def done(res):
                self.set_success(
                    f"Imported {res['added']} new, skipped {res['duplicates']} "
                    f"duplicate(s) of {res['total']} row(s).")
                self.imp_preview.insert(
                    "end", f"\n>>> {res['added']} added, "
                           f"{res['duplicates']} duplicates skipped.\n")

            self._bg(work, done, button=self.imp_btn, busy="Importing…")

        # =================================================================
        # SECTION: Budgets
        # =================================================================
        def _panel_budgets(self, parent):
            self._desc(parent, "budgets")
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x")
            self.bud_cat = aura.AuraCombo(row, width=180, values=[""])
            self.bud_cat.set("")
            self.bud_cat.pack(side="left", padx=(0, 8))
            self.bud_limit = aura.AuraEntry(row, placeholder="Limit", width=100)
            self.bud_limit.pack(side="left", padx=(0, 8))
            self.bud_month = aura.AuraEntry(row, placeholder="YYYY-MM",
                                            width=100)
            self.bud_month.pack(side="left", padx=(0, 8))
            aura.AuraButton(row, "Set budget", kind="primary",
                            command=self._set_budget).pack(side="left",
                                                           padx=(0, 8))
            aura.AuraButton(row, "Refresh", kind="secondary",
                            command=self._refresh_budgets).pack(side="left")

            cols = ("category", "actual", "limit", "remaining", "status")
            self.bud_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                         height=5)
            for c, w in (("category", 150), ("actual", 100), ("limit", 100),
                         ("remaining", 110), ("status", 80)):
                self.bud_tree.heading(c, text=aura.spaced(c.title()),
                                      anchor="w")
                self.bud_tree.column(c, width=w,
                                     anchor="w" if c == "category" else "e")
            self.bud_tree.pack(fill="x", pady=(10, 8))

            self.bud_canvas = tk.Label(parent, bd=0, text="")
            self.bud_canvas.pack(fill="both", expand=True)
            aura.track(self.bud_canvas, "canvas")

        def _set_budget(self):
            cat = self.bud_cat.get().strip()
            limit = importer.parse_amount(self.bud_limit.get())
            try:
                if not cat:
                    raise BudgetBookError("choose a category")
                if limit is None:
                    raise BudgetBookError("enter a limit")
                self.db.set_budget(cat, limit)
                self.db.add_category(cat)
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return
            self.set_success(f"Budget set: {cat} <= {limit:.2f}")
            self._refresh_budgets()

        def _refresh_budgets(self):
            if not hasattr(self, "bud_tree"):
                return
            cats = self.db.category_names()
            self.bud_cat.configure(values=cats or [""])
            month = self.bud_month.get().strip() or None
            for row in self.bud_tree.get_children():
                self.bud_tree.delete(row)
            rows = budgetmod.budget_vs_actual(self.db, month=month)
            for r in rows:
                lim = "-" if r["limit"] is None else f"{r['limit']:.2f}"
                rem = "-" if r["remaining"] is None else f"{r['remaining']:.2f}"
                status = "OVER" if r["over"] else ("ok" if r["limit"] else "")
                self.bud_tree.insert("", "end",
                    values=(r["category"], f"{r['actual']:.2f}", lim, rem, status))

            def work():
                out = os.path.join(self._tmpdir, "budget.png")
                return reports.render_chart("budget", rows, out)

            def done(png):
                self._show_chart(self.bud_canvas, png)

            if rows:
                self._bg(work, done, busy="Charting…")
            else:
                self.bud_canvas.configure(image="", text="No budgets set yet.")

        # =================================================================
        # SECTION: Reports
        # =================================================================
        def _panel_reports(self, parent):
            self._desc(parent, "reports")
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x")
            aura.AuraButton(row, "Spending (pie)", kind="secondary",
                            command=lambda: self._report("spending")).pack(
                side="left")
            aura.AuraButton(row, "Cash flow (bars)", kind="secondary",
                            command=lambda: self._report("cashflow")).pack(
                side="left", padx=8)
            aura.AuraButton(row, "Net worth (line)", kind="secondary",
                            command=lambda: self._report("networth")).pack(
                side="left")
            self.rep_month = aura.AuraEntry(row, placeholder="YYYY-MM",
                                            width=100)
            self.rep_month.pack(side="left", padx=(12, 0))
            aura.AuraButton(row, "Export PNG…", kind="ghost",
                            command=self._export_report).pack(side="right")

            self.rep_canvas = tk.Label(parent, bd=0, text="")
            self.rep_canvas.pack(fill="both", expand=True, pady=(10, 0))
            aura.track(self.rep_canvas, "canvas")

        def _report(self, kind):
            month = self.rep_month.get().strip() or None
            # query on the UI thread (sqlite connections are single-thread);
            # only the slow matplotlib render goes to the worker.
            try:
                if kind == "spending":
                    data = reports.spending_by_category(self.db, month=month)
                elif kind == "cashflow":
                    data = reports.income_vs_expense(self.db)
                else:
                    data = reports.net_worth(self.db)
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return

            def work():
                out = os.path.join(self._tmpdir, f"{kind}.png")
                reports.render_chart(kind, data, out)
                return kind, data, out

            def done(payload):
                k, data, png = payload
                self._last_report = (k, data)
                self._show_chart(self.rep_canvas, png)
                self.set_success(f"{k} chart rendered.")

            self._bg(work, done, busy="Rendering…")

        def _export_report(self):
            if not self._last_report:
                self.set_error("Render a report first.")
                return
            kind, data = self._last_report
            path = filedialog.asksaveasfilename(defaultextension=".png",
                                                filetypes=PNG_TYPES,
                                                initialfile=f"{kind}.png")
            if not path:
                return

            def work():
                return reports.render_chart(kind, data, path)

            def done(p):
                self.set_success(f"Saved {p}")
                open_in_file_manager(p)

            self._bg(work, done, busy="Saving…")

        # =================================================================
        # SECTION: Rules
        # =================================================================
        def _panel_rules(self, parent):
            self._desc(parent, "rules")
            row = ctk.CTkFrame(parent, fg_color="transparent")
            row.pack(fill="x")
            self.rule_pat = aura.AuraEntry(row, placeholder="Payee contains…",
                                           width=200)
            self.rule_pat.pack(side="left", padx=(0, 8))
            self.rule_cat = aura.AuraCombo(row, width=170, values=[""])
            self.rule_cat.set("")
            self.rule_cat.pack(side="left", padx=(0, 8))
            self.rule_regex = tk.BooleanVar(value=False)
            ctk.CTkCheckBox(row, text="regex", variable=self.rule_regex,
                            font=aura.font()).pack(side="left")
            aura.AuraButton(row, "Add rule", kind="primary",
                            command=self._add_rule).pack(side="left", padx=8)

            act = ctk.CTkFrame(parent, fg_color="transparent")
            act.pack(fill="x", pady=(8, 0))
            aura.AuraButton(act, "Apply to uncategorized", kind="secondary",
                            command=lambda: self._apply_rules(False)).pack(
                side="left")
            aura.AuraButton(act, "Apply to all", kind="secondary",
                            command=lambda: self._apply_rules(True)).pack(
                side="left", padx=8)
            aura.AuraButton(act, "Delete selected", kind="danger",
                            command=self._delete_rule).pack(side="left")

            cols = ("id", "pattern", "kind", "category")
            self.rule_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                          height=8)
            for c, w in (("id", 50), ("pattern", 240), ("kind", 80),
                         ("category", 160)):
                self.rule_tree.heading(c, text=aura.spaced(c.title()),
                                      anchor="w")
                self.rule_tree.column(c, width=w,
                                      anchor="e" if c == "id" else "w")
            self.rule_tree.pack(fill="both", expand=True, pady=(10, 0))

        def _refresh_rules(self):
            if not hasattr(self, "rule_tree"):
                return
            self.rule_cat.configure(values=self.db.category_names() or [""])
            for row in self.rule_tree.get_children():
                self.rule_tree.delete(row)
            for r in categorize.list_rules(self.db):
                self.rule_tree.insert(
                    "", "end", iid=str(r["id"]),
                    values=(r["id"], r["pattern"],
                            "regex" if r["is_regex"] else "text", r["category"]))

        def _add_rule(self):
            try:
                categorize.add_rule(self.db, self.rule_pat.get(),
                                    self.rule_cat.get(),
                                    is_regex=bool(self.rule_regex.get()))
                if self.rule_cat.get().strip():
                    self.db.add_category(self.rule_cat.get().strip())
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return
            self.rule_pat.delete(0, "end")
            self.set_success("Rule added.")
            self._refresh_rules()

        def _delete_rule(self):
            sel = self.rule_tree.selection()
            if not sel:
                self.set_error("Select a rule to delete.")
                return
            categorize.delete_rule(self.db, int(sel[0]))
            self._refresh_rules()

        def _apply_rules(self, all_txns):
            try:
                n = categorize.apply_rules(self.db, only_uncategorized=not all_txns)
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return
            self.set_success(f"Categorized {n} transaction(s).")

        # ---- chart display helper (matplotlib Agg PNG -> tk.PhotoImage)
        def _show_chart(self, label_widget, png_path):
            try:
                img = tk.PhotoImage(file=png_path)
                # shrink (rational zoom/subsample -- still pure tk.PhotoImage)
                # so the chart is never clipped by the space the label has.
                try:
                    label_widget.update_idletasks()
                    avail_h = label_widget.winfo_height()
                    avail_w = label_widget.winfo_width()
                    if avail_h > 50 and avail_w > 50 and (
                            img.height() > avail_h or img.width() > avail_w):
                        target = min(avail_h / img.height(),
                                     avail_w / img.width())
                        for z, d in ((4, 5), (3, 4), (2, 3), (3, 5), (1, 2),
                                     (2, 5), (1, 3), (1, 4)):
                            if z / d <= target:
                                if z > 1:
                                    img = img.zoom(z, z)
                                img = img.subsample(d, d)
                                break
                        else:
                            img = img.subsample(4, 4)
                except Exception:
                    pass
                self._img_refs_gui.append(img)
                # cap memory: keep only the last few images
                self._img_refs_gui = self._img_refs_gui[-6:]
                label_widget.configure(image=img, text="")
                label_widget.image = img
            except Exception as ex:
                label_widget.configure(image="",
                                       text=f"(could not display chart: {ex})")

        # ---- About
        def _about(self):
            win = ctk.CTkToplevel(self)
            win.title("About BudgetBook")
            win.resizable(False, False)
            win.transient(self)
            frm = ctk.CTkFrame(win, fg_color="transparent")
            frm.pack(fill="both", expand=True, padx=20, pady=18)
            aura.Heading(frm, APP_NAME).pack(anchor="w")
            aura.Caption(frm, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                frm, font=aura.font(), justify="left", anchor="w",
                wraplength=400,
                text="A fast, fully-offline personal-finance manager — "
                     "import bank CSV/OFX, budget by category, auto-"
                     "categorize and chart spending, cash flow and net "
                     "worth.\n\n100% AI-built, open source, published on "
                     "QuickOpen. Nothing is ever uploaded anywhere."
                ).pack(anchor="w")
            aura.Caption(frm,
                         "Licensed under Apache-2.0. Built on permissive "
                         "libraries: ofxparse, matplotlib, CustomTkinter.",
                         wraplength=400, justify="left").pack(
                anchor="w", pady=(10, 4))
            aura.AuraButton(frm, "Project page: quickopen.ai", kind="ghost",
                            command=lambda: open_with_default_app(
                                PROJECT_URL)).pack(anchor="w", pady=(6, 0))
            aura.AuraButton(frm, "Close", kind="secondary",
                            command=win.destroy).pack(anchor="e", pady=(8, 0))
            win.grab_set()

        def _on_close(self):
            try:
                self.db.close()
            except Exception:
                pass
            try:
                import shutil
                shutil.rmtree(self._tmpdir, ignore_errors=True)
            except Exception:
                pass
            self.destroy()

    return App


def main(argv=None):
    """Entry point: build the root window and run.  Degrades on headless hosts.

    Importing this module does nothing; only this function creates a Tk root.
    With no display (e.g. a server or CI) or without customtkinter installed,
    it prints a friendly note and returns 0 instead of raising -- so
    ``gui.main()`` is safe to call headless.
    """
    try:
        import tkinter as tk
    except Exception as exc:  # tkinter missing entirely
        print(f"{APP_NAME}: a graphical environment with tkinter is required "
              f"to run the GUI ({exc}).")
        return 0

    # Headless guard: don't even build the class if there is no display.
    if os.name != "nt" and sys.platform != "darwin" and not os.environ.get("DISPLAY"):
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here. This app is intended for the Windows desktop.")
        return 0

    try:
        App = build_app()
        app = App()
    except ImportError as exc:
        print(f"{APP_NAME}: the GUI needs the 'customtkinter' package "
              f"({exc}). Install it with:  pip install customtkinter")
        return 0
    except tk.TclError as exc:
        print(f"{APP_NAME}: no graphical display available — cannot start the "
              f"GUI here ({exc}). This app is intended for the Windows desktop.")
        return 0
    except Exception as exc:
        print(f"{APP_NAME}: could not start the GUI ({exc}).")
        return 1

    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
