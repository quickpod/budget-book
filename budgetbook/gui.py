#!/usr/bin/env python3
r"""BudgetBook -- a pure-stdlib tkinter GUI on top of the ``budgetbook`` API.

A single main window: a left sidebar (Transactions, Import, Budgets, Reports,
Rules) and a main panel that swaps to the selected section.  Every operation
calls the tested core library (never re-implements finance logic).  Slow work --
importing a statement, rendering a chart -- runs on a background thread and is
marshalled back to the UI with ``self.after``; failures are shown in a clear
inline bar as the ``BudgetBookError`` message, never a raw traceback.

Design goals baked in here:
  * pure standard-library tkinter/ttk -- NO third-party GUI deps.  Dark mode is
    a ttk-style + palette swap mirroring the QuickOpen palette.
  * Importing this module does nothing.  Only :func:`main` builds a root window,
    and it degrades gracefully (prints a message, returns 0) with no display.
  * Frozen-exe safe: bundled assets are resolved via ``sys._MEIPASS`` / the exe
    directory when ``sys.frozen`` is set -- never ``__file__``.
  * Charts come from :func:`budgetbook.reports.render_chart` (matplotlib Agg ->
    PNG) and are shown via ``tk.PhotoImage``.

100% AI-built, open source, published on QuickOpen (quickopen.ai).
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading

# NOTE: tkinter is imported lazily inside main()/build_app so that merely
# importing this module (e.g. during packaging or on a headless CI box) never
# fails and has no side effects.

APP_NAME = "BudgetBook"
APP_VERSION = "1.0.0"
WINDOW_TITLE = "BudgetBook — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"

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

# ---- colour palettes (mirror the QuickOpen palette) -------------------------
PALETTES = {
    "light": {
        "bg": "#f5f7fa", "surface": "#ffffff", "text": "#141820",
        "muted": "#5b6472", "primary": "#2f5fe0", "primary_hi": "#2450c8",
        "entry": "#ffffff", "border": "#d5dae2", "sel": "#2f5fe0",
        "sel_fg": "#ffffff", "trough": "#e2e7ef", "ok": "#1f7a3d",
        "err": "#c0392b",
    },
    "dark": {
        "bg": "#0f1115", "surface": "#1a1e24", "text": "#f1f3f7",
        "muted": "#9aa4b2", "primary": "#5b86f7", "primary_hi": "#7098ff",
        "entry": "#1a1e24", "border": "#2a2f38", "sel": "#5b86f7",
        "sel_fg": "#0f1115", "trough": "#2a2f38", "ok": "#5bd68a",
        "err": "#ff6b5e",
    },
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

    Kept inside a function so this module imports cleanly without a display.
    """
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog

    from . import guiconfig
    from .errors import BudgetBookError
    from .db import Database, default_db_path
    from . import importer, categorize, budget as budgetmod, reports

    FONT = "Segoe UI"

    class App(tk.Tk):
        def __init__(self, db_path=None):
            super().__init__()
            self.title(WINDOW_TITLE)
            self.geometry("1120x700")
            self.minsize(920, 580)

            self.theme = guiconfig.get_theme()
            self._busy = False
            self._panels = {}
            self._current = None
            self._tracked = []          # (tk_widget, role) for manual re-theming
            self._img_refs = []         # keep PhotoImage refs alive
            self._tmpdir = tempfile.mkdtemp(prefix="budgetbook_gui_")

            self.db = Database(db_path or default_db_path())

            self._set_icon()
            self._build_menu()
            self._build_layout()
            self._apply_theme()
            self.protocol("WM_DELETE_WINDOW", self._on_close)
            self.after(50, lambda: self._select_first())

        # ---- assets / icon
        def _set_icon(self):
            try:
                ico = asset_path("budget-book.ico")
                if ico:
                    self.iconbitmap(ico)
                    return
            except Exception:
                pass
            try:
                png = asset_path("budget-book.png")
                if png:
                    img = tk.PhotoImage(file=png)
                    self._img_refs.append(img)
                    self.iconphoto(True, img)
            except Exception:
                pass  # icon is cosmetic; never block launch

        # ---- theming
        def track(self, widget, role):
            self._tracked.append((widget, role))

        def _pal(self):
            return PALETTES[self.theme]

        def _apply_theme(self):
            p = self._pal()
            style = ttk.Style(self)
            try:
                style.theme_use("clam")
            except Exception:
                pass
            self.configure(bg=p["bg"])
            style.configure(".", background=p["bg"], foreground=p["text"],
                            fieldbackground=p["entry"], bordercolor=p["border"],
                            font=(FONT, 10))
            style.configure("TFrame", background=p["bg"])
            style.configure("Sidebar.TFrame", background=p["surface"])
            style.configure("Card.TFrame", background=p["surface"])
            style.configure("TLabel", background=p["bg"], foreground=p["text"])
            style.configure("Muted.TLabel", background=p["bg"], foreground=p["muted"])
            style.configure("Header.TLabel", background=p["bg"], foreground=p["text"],
                            font=(FONT, 15, "bold"))
            style.configure("Sub.TLabel", background=p["bg"], foreground=p["muted"],
                            font=(FONT, 10))
            style.configure("Brand.TLabel", background=p["surface"],
                            foreground=p["text"], font=(FONT, 12, "bold"))
            style.configure("Ok.TLabel", background=p["bg"], foreground=p["ok"])
            style.configure("Err.TLabel", background=p["bg"], foreground=p["err"])
            style.configure("Status.TLabel", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TButton", background=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], focuscolor=p["surface"],
                            padding=(10, 5))
            style.map("TButton",
                      background=[("active", p["trough"]), ("disabled", p["bg"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Accent.TButton", background=p["primary"],
                            foreground="#ffffff", padding=(12, 6))
            style.map("Accent.TButton",
                      background=[("active", p["primary_hi"]),
                                  ("disabled", p["border"])],
                      foreground=[("disabled", p["muted"])])
            style.configure("Toggle.TButton", background=p["surface"],
                            foreground=p["text"], padding=(8, 4))
            style.configure("Nav.TButton", background=p["surface"],
                            foreground=p["text"], anchor="w", padding=(14, 9),
                            borderwidth=0)
            style.map("Nav.TButton",
                      background=[("active", p["trough"])])
            style.configure("NavSel.TButton", background=p["primary"],
                            foreground="#ffffff", anchor="w", padding=(14, 9),
                            borderwidth=0)
            style.map("NavSel.TButton", background=[("active", p["primary_hi"])])
            for name in ("TEntry", "TSpinbox"):
                style.configure(name, fieldbackground=p["entry"], foreground=p["text"],
                                insertcolor=p["text"], bordercolor=p["border"])
            style.configure("TCombobox", fieldbackground=p["entry"],
                            foreground=p["text"], background=p["surface"],
                            arrowcolor=p["text"])
            style.map("TCombobox",
                      fieldbackground=[("readonly", p["entry"])],
                      foreground=[("readonly", p["text"])])
            style.configure("TCheckbutton", background=p["bg"], foreground=p["text"])
            style.map("TCheckbutton", background=[("active", p["bg"])])
            style.configure("TLabelframe", background=p["bg"], foreground=p["text"],
                            bordercolor=p["border"])
            style.configure("TLabelframe.Label", background=p["bg"],
                            foreground=p["muted"])
            style.configure("Treeview", background=p["surface"],
                            fieldbackground=p["surface"], foreground=p["text"],
                            bordercolor=p["border"], rowheight=24)
            style.map("Treeview", background=[("selected", p["primary"])],
                      foreground=[("selected", p["sel_fg"])])
            style.configure("Treeview.Heading", background=p["surface"],
                            foreground=p["muted"])
            style.configure("TScrollbar", background=p["surface"],
                            troughcolor=p["bg"], bordercolor=p["border"],
                            arrowcolor=p["text"])
            style.configure("TSeparator", background=p["border"])

            for widget, role in list(self._tracked):
                try:
                    if role == "text":
                        widget.configure(bg=p["surface"], fg=p["text"],
                                         insertbackground=p["text"],
                                         selectbackground=p["primary"],
                                         selectforeground=p["sel_fg"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"],
                                         borderwidth=0)
                    elif role == "canvas":
                        widget.configure(bg=p["surface"],
                                         highlightthickness=1,
                                         highlightbackground=p["border"])
                except Exception:
                    pass
            # refresh nav button styles
            for sid, btn in getattr(self, "_nav_btns", {}).items():
                btn.configure(style="NavSel.TButton" if sid == self._current
                              else "Nav.TButton")

        def toggle_theme(self):
            self.theme = "dark" if self.theme == "light" else "light"
            guiconfig.set_theme(self.theme)
            self._apply_theme()
            self._theme_btn.configure(
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            # re-render current section so chart colours etc. refresh
            if self._current:
                self._rebuild_current()

        # ---- menu
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Import statement…",
                              command=lambda: self._select("import"))
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)
            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About BudgetBook", command=self._about)
            bar.add_cascade(label="Help", menu=helpm)
            self.config(menu=bar)

        # ---- layout
        def _build_layout(self):
            top = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 8))
            top.pack(fill="x", side="top")
            ttk.Label(top, text="BudgetBook", style="Brand.TLabel").pack(side="left")
            ttk.Label(top, style="Status.TLabel",
                      text="  offline · open source · by QuickOpen").pack(side="left")
            self._theme_btn = ttk.Button(
                top, style="Toggle.TButton", command=self.toggle_theme,
                text="☀ Light mode" if self.theme == "dark" else "🌙 Dark mode")
            self._theme_btn.pack(side="right")

            body = ttk.Frame(self, style="TFrame")
            body.pack(fill="both", expand=True)

            side = ttk.Frame(body, style="Sidebar.TFrame", width=210)
            side.pack(side="left", fill="y")
            side.pack_propagate(False)
            self._nav_btns = {}
            for sid, label in SECTIONS:
                btn = ttk.Button(side, text=label, style="Nav.TButton",
                                 command=lambda s=sid: self._select(s))
                btn.pack(fill="x", padx=6, pady=2)
                self._nav_btns[sid] = btn

            main = ttk.Frame(body, style="TFrame", padding=(16, 12))
            main.pack(side="left", fill="both", expand=True)

            head = ttk.Frame(main, style="TFrame")
            head.pack(fill="x")
            self.title_lbl = ttk.Label(head, text="Welcome", style="Header.TLabel")
            self.title_lbl.pack(anchor="w")
            self.desc_lbl = ttk.Label(head, text="", style="Sub.TLabel",
                                      wraplength=760, justify="left")
            self.desc_lbl.pack(anchor="w", pady=(2, 8))
            ttk.Separator(main).pack(fill="x")

            self.container = ttk.Frame(main, style="TFrame")
            self.container.pack(fill="both", expand=True, pady=(10, 8))

            bar = ttk.Frame(self, style="Sidebar.TFrame", padding=(12, 6))
            bar.pack(fill="x", side="bottom")
            self.status_lbl = ttk.Label(bar, text="Ready", style="Status.TLabel",
                                        width=14, anchor="w")
            self.status_lbl.pack(side="left")
            self.result_lbl = ttk.Label(bar, text="", style="Status.TLabel",
                                        anchor="w", wraplength=820, justify="left")
            self.result_lbl.pack(side="left", fill="x", expand=True, padx=8)

        def _select_first(self):
            self._select(SECTIONS[0][0])

        def _select(self, sid):
            if self._current is not None and self._current in self._panels:
                self._panels[self._current].pack_forget()
            self._current = sid
            panel = self._panels.get(sid)
            if panel is None:
                panel = ttk.Frame(self.container, style="TFrame")
                self._panels[sid] = panel
                builder = getattr(self, "_panel_" + sid, None)
                if builder:
                    builder(panel)
                self._apply_theme()
            panel.pack(fill="both", expand=True)
            self.title_lbl.configure(text=dict(SECTIONS)[sid])
            self.desc_lbl.configure(text=SECTION_DESC.get(sid, ""))
            self._clear_result()
            for s, btn in self._nav_btns.items():
                btn.configure(style="NavSel.TButton" if s == sid else "Nav.TButton")
            refresh = getattr(self, "_refresh_" + sid, None)
            if refresh:
                refresh()

        def _rebuild_current(self):
            """Discard and rebuild the current panel (used after theme change)."""
            sid = self._current
            if not sid:
                return
            panel = self._panels.pop(sid, None)
            if panel is not None:
                panel.destroy()
            self._current = None
            self._select(sid)

        # ---- background runner
        def _bg(self, work, on_ok, button=None, busy="Working…"):
            if self._busy:
                self._show_error("Please wait — an operation is already running.")
                return
            self._busy = True
            if button is not None:
                try:
                    button.state(["disabled"])
                except Exception:
                    pass
            self._set_status(busy, kind="working")
            self._clear_result(keep_status=True)

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
                    self._set_status("error", kind="err")
                    self._show_error(err)
                    return
                self._set_status("done", kind="ok")
                try:
                    on_ok(res)
                except Exception as ex:
                    self._show_error(f"Post-processing error: {ex}")

            threading.Thread(target=run, daemon=True).start()

        # ---- status / result bar
        def _set_status(self, text, kind="idle"):
            p = self._pal()
            color = {"working": p["primary"], "ok": p["ok"], "err": p["err"]}.get(
                kind, p["muted"])
            self.status_lbl.configure(text=text, foreground=color)

        def _clear_result(self, keep_status=False):
            self.result_lbl.configure(text="")
            if not keep_status:
                self._set_status("Ready")

        def _show_error(self, message):
            self.result_lbl.configure(text="✕ " + message,
                                      foreground=self._pal()["err"])

        def _show_ok(self, message):
            self.result_lbl.configure(text="✓ " + message,
                                      foreground=self._pal()["ok"])
            self._set_status("done", kind="ok")

        # =================================================================
        # PANEL: Transactions
        # =================================================================
        def _panel_transactions(self, parent):
            filt = ttk.Frame(parent, style="TFrame")
            filt.pack(fill="x", pady=(0, 6))
            ttk.Label(filt, text="Month (YYYY-MM):").pack(side="left")
            self.tx_month = ttk.Entry(filt, width=10)
            self.tx_month.pack(side="left", padx=(4, 12))
            ttk.Label(filt, text="Category:").pack(side="left")
            self.tx_cat_filter = ttk.Combobox(filt, width=16, state="readonly")
            self.tx_cat_filter.pack(side="left", padx=4)
            ttk.Button(filt, text="Apply filter",
                       command=self._refresh_transactions).pack(side="left", padx=6)
            ttk.Button(filt, text="Clear",
                       command=self._clear_tx_filter).pack(side="left")

            cols = ("id", "date", "amount", "category", "account", "cleared", "payee")
            self.tx_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                        selectmode="browse", height=14)
            for c, w, anchor in (("id", 46, "e"), ("date", 92, "w"),
                                 ("amount", 96, "e"), ("category", 120, "w"),
                                 ("account", 110, "w"), ("cleared", 60, "center"),
                                 ("payee", 260, "w")):
                self.tx_tree.heading(c, text=c.title())
                self.tx_tree.column(c, width=w, anchor=anchor)
            self.tx_tree.pack(fill="both", expand=True)
            self.tx_tree.bind("<Double-1>", lambda e: self._edit_tx())

            btns = ttk.Frame(parent, style="TFrame")
            btns.pack(fill="x", pady=(8, 0))
            ttk.Button(btns, text="Add…", style="Accent.TButton",
                       command=self._add_tx).pack(side="left")
            ttk.Button(btns, text="Edit…", command=self._edit_tx).pack(side="left", padx=6)
            ttk.Button(btns, text="Delete", command=self._delete_tx).pack(side="left")
            ttk.Button(btns, text="Toggle cleared",
                       command=self._toggle_cleared).pack(side="left", padx=6)

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
                self._show_error(str(ex))
                return
            for t in txns:
                self.tx_tree.insert(
                    "", "end", iid=str(t["id"]),
                    values=(t["id"], t["date"], f"{t['amount']:.2f}",
                            t["category"] or "-", t["account"] or "-",
                            "✓" if t["cleared"] else "", t["payee"]))
            self._show_ok(f"{len(txns)} transaction(s).")

        def _selected_tx_id(self):
            sel = self.tx_tree.selection()
            return int(sel[0]) if sel else None

        def _add_tx(self):
            self._tx_dialog(None)

        def _edit_tx(self):
            tid = self._selected_tx_id()
            if tid is None:
                self._show_error("Select a transaction to edit.")
                return
            self._tx_dialog(self.db.get_transaction(tid))

        def _delete_tx(self):
            tid = self._selected_tx_id()
            if tid is None:
                self._show_error("Select a transaction to delete.")
                return
            if messagebox.askyesno("Delete", "Delete this transaction?"):
                self.db.delete_transaction(tid)
                self._refresh_transactions()

        def _toggle_cleared(self):
            tid = self._selected_tx_id()
            if tid is None:
                self._show_error("Select a transaction first.")
                return
            t = self.db.get_transaction(tid)
            self.db.set_cleared(tid, not t["cleared"])
            self._refresh_transactions()

        def _tx_dialog(self, txn):
            win = tk.Toplevel(self)
            win.title("Edit transaction" if txn else "Add transaction")
            win.configure(bg=self._pal()["bg"])
            win.transient(self)
            frm = ttk.Frame(win, style="TFrame", padding=14)
            frm.pack(fill="both", expand=True)
            fields = {}

            def field(label, value=""):
                r = ttk.Frame(frm, style="TFrame")
                r.pack(fill="x", pady=3)
                ttk.Label(r, text=label, width=12).pack(side="left")
                e = ttk.Entry(r, width=32)
                e.insert(0, value)
                e.pack(side="left", fill="x", expand=True)
                return e

            fields["date"] = field("Date", (txn or {}).get("date", ""))
            fields["amount"] = field("Amount", str((txn or {}).get("amount", "")))
            fields["payee"] = field("Payee", (txn or {}).get("payee", ""))
            r = ttk.Frame(frm, style="TFrame")
            r.pack(fill="x", pady=3)
            ttk.Label(r, text="Category", width=12).pack(side="left")
            cat_box = ttk.Combobox(r, width=30, values=self.db.category_names())
            cat_box.set((txn or {}).get("category", ""))
            cat_box.pack(side="left", fill="x", expand=True)
            fields["category"] = cat_box
            fields["account"] = field("Account", (txn or {}).get("account", ""))
            fields["notes"] = field("Notes", (txn or {}).get("notes", ""))

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
                    messagebox.showerror("BudgetBook", str(ex), parent=win)
                    return
                win.destroy()
                self._refresh_transactions()

            b = ttk.Frame(frm, style="TFrame")
            b.pack(fill="x", pady=(10, 0))
            ttk.Button(b, text="Save", style="Accent.TButton",
                       command=save).pack(side="right")
            ttk.Button(b, text="Cancel",
                       command=win.destroy).pack(side="right", padx=6)
            win.grab_set()

        # =================================================================
        # PANEL: Import
        # =================================================================
        def _panel_import(self, parent):
            row = ttk.Frame(parent, style="TFrame")
            row.pack(fill="x")
            ttk.Label(row, text="File:").pack(side="left")
            self.imp_path = ttk.Entry(row)
            self.imp_path.pack(side="left", fill="x", expand=True, padx=6)
            ttk.Button(row, text="Browse…",
                       command=self._pick_import).pack(side="left")

            opt = ttk.Frame(parent, style="TFrame")
            opt.pack(fill="x", pady=(8, 4))
            ttk.Label(opt, text="Account:").pack(side="left")
            self.imp_account = ttk.Entry(opt, width=18)
            self.imp_account.pack(side="left", padx=(4, 12))
            ttk.Button(opt, text="Detect columns / preview",
                       command=self._preview_import).pack(side="left")
            self.imp_btn = ttk.Button(opt, text="Import", style="Accent.TButton",
                                      command=self._do_import)
            self.imp_btn.pack(side="left", padx=6)

            self.imp_map_lbl = ttk.Label(parent, style="Sub.TLabel", text="")
            self.imp_map_lbl.pack(anchor="w", pady=(4, 2))

            self.imp_preview = tk.Text(parent, height=14, wrap="none")
            self.imp_preview.pack(fill="both", expand=True)
            self.track(self.imp_preview, "text")

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
                self._show_error("Choose a file first.")
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
                self._show_ok(f"{len(rows)} row(s) ready to import.")
            except BudgetBookError as ex:
                self.imp_map_lbl.configure(text="")
                self._show_error(str(ex))

        def _do_import(self):
            path = self.imp_path.get().strip()
            if not path:
                self._show_error("Choose a file first.")
                return
            account = self.imp_account.get().strip()
            is_ofx = self._is_ofx(path)

            def work():
                if is_ofx:
                    return importer.import_ofx(self.db, path, account=account)
                return importer.import_csv(self.db, path, account=account)

            def done(res):
                self._show_ok(
                    f"Imported {res['added']} new, skipped {res['duplicates']} "
                    f"duplicate(s) of {res['total']} row(s).")
                self.imp_preview.insert(
                    "end", f"\n>>> {res['added']} added, "
                           f"{res['duplicates']} duplicates skipped.\n")

            self._bg(work, done, button=self.imp_btn, busy="Importing…")

        # =================================================================
        # PANEL: Budgets
        # =================================================================
        def _panel_budgets(self, parent):
            row = ttk.Frame(parent, style="TFrame")
            row.pack(fill="x")
            ttk.Label(row, text="Category:").pack(side="left")
            self.bud_cat = ttk.Combobox(row, width=18)
            self.bud_cat.pack(side="left", padx=4)
            ttk.Label(row, text="Limit:").pack(side="left")
            self.bud_limit = ttk.Entry(row, width=10)
            self.bud_limit.pack(side="left", padx=4)
            ttk.Label(row, text="Month:").pack(side="left")
            self.bud_month = ttk.Entry(row, width=9)
            self.bud_month.pack(side="left", padx=4)
            ttk.Button(row, text="Set budget", style="Accent.TButton",
                       command=self._set_budget).pack(side="left", padx=6)
            ttk.Button(row, text="Refresh",
                       command=self._refresh_budgets).pack(side="left")

            cols = ("category", "actual", "limit", "remaining", "status")
            self.bud_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                         height=8)
            for c, w in (("category", 150), ("actual", 100), ("limit", 100),
                         ("remaining", 110), ("status", 80)):
                self.bud_tree.heading(c, text=c.title())
                self.bud_tree.column(c, width=w,
                                     anchor="w" if c == "category" else "e")
            self.bud_tree.pack(fill="x", pady=(8, 6))

            self.bud_canvas = tk.Label(parent, background=self._pal()["surface"])
            self.bud_canvas.pack(fill="both", expand=True)

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
                self._show_error(str(ex))
                return
            self._show_ok(f"Budget set: {cat} <= {limit:.2f}")
            self._refresh_budgets()

        def _refresh_budgets(self):
            if not hasattr(self, "bud_tree"):
                return
            self.bud_cat.configure(values=self.db.category_names())
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
        # PANEL: Reports
        # =================================================================
        def _panel_reports(self, parent):
            row = ttk.Frame(parent, style="TFrame")
            row.pack(fill="x")
            ttk.Button(row, text="Spending (pie)",
                       command=lambda: self._report("spending")).pack(side="left")
            ttk.Button(row, text="Cash flow (bars)",
                       command=lambda: self._report("cashflow")).pack(side="left", padx=6)
            ttk.Button(row, text="Net worth (line)",
                       command=lambda: self._report("networth")).pack(side="left")
            ttk.Label(row, text="   Month:").pack(side="left")
            self.rep_month = ttk.Entry(row, width=9)
            self.rep_month.pack(side="left", padx=4)
            ttk.Button(row, text="Export PNG…",
                       command=self._export_report).pack(side="right")

            self.rep_canvas = tk.Label(parent, background=self._pal()["surface"])
            self.rep_canvas.pack(fill="both", expand=True, pady=(8, 0))
            self._last_report = None  # (kind, data)

        def _report(self, kind):
            month = self.rep_month.get().strip() or None

            def work():
                if kind == "spending":
                    data = reports.spending_by_category(self.db, month=month)
                elif kind == "cashflow":
                    data = reports.income_vs_expense(self.db)
                else:
                    data = reports.net_worth(self.db)
                out = os.path.join(self._tmpdir, f"{kind}.png")
                reports.render_chart(kind, data, out)
                return kind, data, out

            def done(payload):
                k, data, png = payload
                self._last_report = (k, data)
                self._show_chart(self.rep_canvas, png)
                self._show_ok(f"{k} chart rendered.")

            self._bg(work, done, busy="Rendering…")

        def _export_report(self):
            if not self._last_report:
                self._show_error("Render a report first.")
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
                self._show_ok(f"Saved {p}")
                open_in_file_manager(p)

            self._bg(work, done, busy="Saving…")

        # =================================================================
        # PANEL: Rules
        # =================================================================
        def _panel_rules(self, parent):
            row = ttk.Frame(parent, style="TFrame")
            row.pack(fill="x")
            ttk.Label(row, text="Payee contains:").pack(side="left")
            self.rule_pat = ttk.Entry(row, width=20)
            self.rule_pat.pack(side="left", padx=4)
            ttk.Label(row, text="Category:").pack(side="left")
            self.rule_cat = ttk.Combobox(row, width=16)
            self.rule_cat.pack(side="left", padx=4)
            self.rule_regex = tk.IntVar(value=0)
            ttk.Checkbutton(row, text="regex", variable=self.rule_regex).pack(side="left")
            ttk.Button(row, text="Add rule", style="Accent.TButton",
                       command=self._add_rule).pack(side="left", padx=6)

            act = ttk.Frame(parent, style="TFrame")
            act.pack(fill="x", pady=(6, 0))
            ttk.Button(act, text="Apply to uncategorized",
                       command=lambda: self._apply_rules(False)).pack(side="left")
            ttk.Button(act, text="Apply to all",
                       command=lambda: self._apply_rules(True)).pack(side="left", padx=6)
            ttk.Button(act, text="Delete selected",
                       command=self._delete_rule).pack(side="left")

            cols = ("id", "pattern", "kind", "category")
            self.rule_tree = ttk.Treeview(parent, columns=cols, show="headings",
                                          height=12)
            for c, w in (("id", 50), ("pattern", 240), ("kind", 80),
                         ("category", 160)):
                self.rule_tree.heading(c, text=c.title())
                self.rule_tree.column(c, width=w,
                                      anchor="e" if c == "id" else "w")
            self.rule_tree.pack(fill="both", expand=True, pady=(8, 0))

        def _refresh_rules(self):
            if not hasattr(self, "rule_tree"):
                return
            self.rule_cat.configure(values=self.db.category_names())
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
                self._show_error(str(ex))
                return
            self.rule_pat.delete(0, "end")
            self._show_ok("Rule added.")
            self._refresh_rules()

        def _delete_rule(self):
            sel = self.rule_tree.selection()
            if not sel:
                self._show_error("Select a rule to delete.")
                return
            categorize.delete_rule(self.db, int(sel[0]))
            self._refresh_rules()

        def _apply_rules(self, all_txns):
            try:
                n = categorize.apply_rules(self.db, only_uncategorized=not all_txns)
            except BudgetBookError as ex:
                self._show_error(str(ex))
                return
            self._show_ok(f"Categorized {n} transaction(s).")

        # ---- chart display helper
        def _show_chart(self, label_widget, png_path):
            try:
                img = tk.PhotoImage(file=png_path)
                self._img_refs.append(img)
                # cap memory: keep only the last few images
                self._img_refs = self._img_refs[-6:]
                label_widget.configure(image=img, text="")
                label_widget.image = img
            except Exception as ex:
                label_widget.configure(image="", text=f"(could not display chart: {ex})")

        # ---- About
        def _about(self):
            win = tk.Toplevel(self)
            win.title("About BudgetBook")
            win.configure(bg=self._pal()["bg"])
            win.resizable(False, False)
            frm = ttk.Frame(win, style="TFrame", padding=18)
            frm.pack(fill="both", expand=True)
            ttk.Label(frm, text="BudgetBook", style="Header.TLabel").pack(anchor="w")
            ttk.Label(frm, text=f"Version {APP_VERSION}",
                      style="Sub.TLabel").pack(anchor="w", pady=(0, 8))
            ttk.Label(frm, style="TLabel", justify="left", wraplength=400,
                      text="A fast, fully-offline personal-finance manager — "
                           "import bank CSV/OFX, budget by category, auto-"
                           "categorize and chart spending, cash flow and net "
                           "worth.\n\n100% AI-built, open source, published on "
                           "QuickOpen. Nothing is ever uploaded anywhere."
                      ).pack(anchor="w")
            ttk.Label(frm, style="Sub.TLabel", justify="left", wraplength=400,
                      text="Licensed under Apache-2.0. Built on permissive "
                           "libraries: ofxparse, matplotlib."
                      ).pack(anchor="w", pady=(8, 4))
            link = ttk.Label(frm, text="Project page: quickopen.ai",
                             style="Ok.TLabel", cursor="hand2")
            link.pack(anchor="w", pady=(4, 10))
            link.bind("<Button-1>", lambda e: open_with_default_app(PROJECT_URL))
            ttk.Button(frm, text="Close", command=win.destroy).pack(anchor="e")
            win.transient(self)
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
    With no display (e.g. a server or CI), it prints a friendly note and returns
    0 instead of raising -- so ``gui.main()`` is safe to call headless.
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
