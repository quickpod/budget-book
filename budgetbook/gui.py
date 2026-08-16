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
APP_VERSION = "1.1.0"
WINDOW_TITLE = "BudgetBook — by QuickOpen (quickopen.ai)"
PROJECT_URL = "https://quickopen.ai"
ACCENT = "#5b86f7"      # Aura brand accent (the old per-app green was a
                        # legacy scaffold accent)

CSV_TYPES = [("CSV / OFX / QFX", "*.csv *.ofx *.qfx"), ("All files", "*.*")]
PNG_TYPES = [("PNG image", "*.png"), ("All files", "*.*")]

SECTIONS = [
    ("transactions", "Transactions"),
    ("import", "Import"),
    ("budgets", "Budgets"),
    ("reports", "Reports"),
    ("rules", "Rules"),
    ("about", "About"),
]

REPORT_KINDS = [("Spending", "spending"), ("Cash flow", "cashflow"),
                ("Net worth", "networth")]

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
            self._account = None        # sidebar account filter (None = all)

            self.db = Database(db_path or default_db_path())

            self._set_icon()
            self._build_menu()
            self.add_section("transactions", "Transactions", "⇄",
                             self._panel_transactions)
            self.add_section("import", "Import", "↧", self._panel_import)
            self.add_section("budgets", "Budgets", "◈", self._panel_budgets)
            self.add_section("reports", "Reports", "▤", self._panel_reports)
            self.add_section("rules", "Rules", "✎", self._panel_rules)
            self.add_section("about", "About", "ℹ", self._panel_about)
            self._build_accounts_sidebar()
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

        # ---- menu + keyboard baseline (APP-LAYOUT-LANGUAGE.md §7/§9)
        def _build_menu(self):
            bar = tk.Menu(self)
            filem = tk.Menu(bar, tearoff=0)
            filem.add_command(label="Add transaction…", accelerator="Ctrl+N",
                              command=lambda: (self.show("transactions"),
                                               self._add_tx()))
            filem.add_command(label="Import statement…",
                              command=lambda: self.show("import"))
            filem.add_separator()
            filem.add_command(label="Settings…", accelerator="Ctrl+,",
                              command=self._open_settings)
            filem.add_separator()
            filem.add_command(label="Exit", command=self._on_close)
            bar.add_cascade(label="File", menu=filem)

            viewm = tk.Menu(bar, tearoff=0)
            viewm.add_command(label="Toggle sidebar", accelerator="Ctrl+\\",
                              command=self.toggle_sidebar)
            viewm.add_command(
                label="Toggle dark mode",
                command=lambda: self.set_theme(
                    "light" if self.theme == "dark" else "dark"))
            bar.add_cascade(label="View", menu=viewm)

            helpm = tk.Menu(bar, tearoff=0)
            helpm.add_command(label="About BudgetBook",
                              command=lambda: self.show("about"))
            bar.add_cascade(label="Help", menu=helpm)
            self.configure(menu=bar)

            self.bind_all("<Control-n>",
                          lambda e: (self.show("transactions"),
                                     self._add_tx(), "break")[2])
            self.bind_all("<Control-f>",
                          lambda e: (self._focus_search(), "break")[1])
            self.bind_all("<Control-comma>",
                          lambda e: (self._open_settings(), "break")[1])

        def _focus_search(self):
            try:
                self.show("transactions")
                self.tx_search.focus_set()
            except Exception:
                pass

        # =================================================================
        # Sidebar library: accounts with balances (the YNAB convention)
        # =================================================================
        def _build_accounts_sidebar(self):
            aura.SectionLabel(self.sidebar_body, "Accounts").pack(
                anchor="w", padx=6, pady=(0, 4))
            self._acct_scroll = ctk.CTkScrollableFrame(
                self.sidebar_body, fg_color="transparent")
            self._acct_scroll.pack(fill="both", expand=True)
            self._refresh_accounts_sidebar()

        def _acct_row(self, label, balance, account):
            active = (account == self._account)
            text = label if balance is None else \
                "%s   ·  %.2f" % (label, balance)
            btn = ctk.CTkButton(
                self._acct_scroll, text=text, anchor="w", height=30,
                corner_radius=aura.TOKENS["geometry"]["radius_button"],
                fg_color=aura._pair("accent_soft") if active else "transparent",
                hover_color=(aura._pal["light"]["surface2"],
                             aura._pal["dark"]["surface2"]),
                text_color=aura._pair("text") if active else aura._pair("muted"),
                font=aura.font(role="body"),
                command=lambda: self._set_account_filter(account))
            btn.pack(fill="x", pady=1)
            return btn

        def _refresh_accounts_sidebar(self):
            if not hasattr(self, "_acct_scroll"):
                return
            for w in list(self._acct_scroll.winfo_children()):
                try:
                    w.destroy()
                except Exception:
                    pass
            try:
                # accounts seen on transactions + explicitly created ones
                names = {a["name"] for a in self.db.list_accounts()}
                for t in self.db.list_transactions():
                    if t["account"]:
                        names.add(t["account"])
            except BudgetBookError:
                names = set()
            self._acct_row("All accounts", None, None)
            for name in sorted(names, key=str.lower):
                try:
                    txsum = sum(t["amount"]
                                for t in self.db.list_transactions(account=name))
                except BudgetBookError:
                    txsum = 0.0
                self._acct_row(name, txsum, name)
            if self._account is not None and self._account not in names:
                self._account = None

        def _set_account_filter(self, account):
            self._account = account
            self._refresh_accounts_sidebar()
            self.show("transactions")
            self._refresh_transactions()

        # ---- settings (Ctrl+,)
        def _open_settings(self):
            dlg = aura.Dialog(self, title="Settings", size=(520, 320))

            aura.SectionLabel(dlg.body, "Appearance").pack(anchor="w",
                                                           pady=(0, 2))
            trow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            trow.pack(anchor="w", pady=(4, 2))
            aura.Caption(trow, "Theme").pack(side="left", padx=(0, 10))
            cur = guiconfig.get_theme()
            th = aura.AuraOption(trow, values=["System", "Light", "Dark"],
                                 width=110, height=30,
                                 command=self._set_theme_pref)
            th.set(cur.capitalize() if cur in ("light", "dark") else "System")
            th.pack(side="left")
            aura.Caption(dlg.body,
                         "System follows the OS Aura Dark/Light live.").pack(
                anchor="w", pady=(0, 14))

            aura.SectionLabel(dlg.body, "Data").pack(anchor="w", pady=(0, 2))
            aura.Caption(dlg.body, str(self.db.path)).pack(anchor="w")
            drow = ctk.CTkFrame(dlg.body, fg_color="transparent")
            drow.pack(anchor="w", pady=(6, 0))
            aura.AuraButton(drow, "Open data folder", kind="ghost", height=30,
                            command=lambda: open_in_file_manager(
                                str(self.db.path))).pack(side="left")

            dlg.add_button("Close")

        def _set_theme_pref(self, choice):
            pref = str(choice).lower()
            if pref == "system":
                guiconfig.set_theme("system")
                self._follow_system = True
                if self._sys_listener is None:
                    self._start_system_listener()
                self.set_theme(aura._system_theme(), _system=True)
            elif pref in ("light", "dark"):
                self.set_theme(pref)     # persists via on_theme_change

        # ---- theme: keep sidebar rows + amount tags in sync
        def set_theme(self, theme, _system=False):
            super().set_theme(theme, _system=_system)
            try:
                self._refresh_accounts_sidebar()
                if self._last_report and not self._busy:
                    self._report(self._last_report[0])
            except Exception:
                pass

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
            parent.grid_columnconfigure(0, weight=1)
            parent.grid_rowconfigure(1, weight=1)

            tb = aura.Toolbar(parent)
            tb.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            tb.add_button("＋ Add", self._add_tx, kind="primary")
            tb.add_button("Edit…", self._edit_tx,
                          tooltip="Edit the selected transaction (F2)")
            self.tx_search = tb.add_search("Search payee…",
                                           on_change=lambda _t:
                                           self._refresh_transactions(),
                                           width=150)
            self.tx_month = aura.AuraEntry(tb, placeholder="YYYY-MM",
                                           width=82, height=32)
            self.tx_month.bind("<Return>",
                               lambda e: self._refresh_transactions())
            tb.add_right(self.tx_month)
            self.tx_cat_filter = aura.AuraOption(
                tb, values=["All categories"], width=130, height=32,
                command=lambda _v: self._refresh_transactions())
            self.tx_cat_filter.set("All categories")
            tb.add_right(self.tx_cat_filter)

            wrap = ctk.CTkFrame(parent, fg_color=aura._pair("surface"),
                                corner_radius=10, border_width=1,
                                border_color=aura._pair("border"))
            wrap.grid(row=1, column=0, sticky="nsew")
            self._tx_wrap = wrap
            cols = ("date", "payee", "category", "account", "amount",
                    "cleared")
            self.tx_tree = ttk.Treeview(wrap, columns=cols, show="headings",
                                        selectmode="browse")
            for c, label, w, anchor, stretch in (
                    ("date", "Date", 100, "w", False),
                    ("payee", "Payee", 240, "w", True),
                    ("category", "Category", 130, "w", False),
                    ("account", "Account", 110, "w", False),
                    ("amount", "Amount", 96, "e", False),
                    ("cleared", "✓", 40, "center", False)):
                self.tx_tree.heading(c, text=label, anchor="w")
                self.tx_tree.column(c, width=w, anchor=anchor,
                                    stretch=stretch)
            sb = aura.AuraScrollbar(wrap, command=self.tx_tree.yview)
            self.tx_tree.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y", padx=(0, 4), pady=6)
            self.tx_tree.pack(side="left", fill="both", expand=True,
                              padx=(6, 0), pady=6)
            self.tx_tree.bind("<Double-1>", lambda e: self._edit_tx())
            self.tx_tree.bind("<Delete>", lambda e: self._delete_tx())
            self.tx_tree.bind("<F2>", lambda e: self._edit_tx())
            self.tx_tree.bind("<Button-3>", self._show_tx_menu)
            self._tx_menu = tk.Menu(self, tearoff=0)
            aura.track(self._tx_menu, "menu")

            self.empty_tx = aura.EmptyState(
                parent, title="No transactions yet",
                caption="Import a bank statement (CSV or OFX/QFX) or add "
                        "your first transaction — expenses negative, income "
                        "positive.",
                action_text="↧ Import a statement",
                action=lambda: self.show("import"),
                image=(asset_path("assets/money-empty-light.png"),
                       asset_path("assets/money-empty-dark.png")))

        def _show_tx_menu(self, event):
            iid = self.tx_tree.identify_row(event.y)
            if not iid:
                return
            self.tx_tree.selection_set(iid)
            m = self._tx_menu
            m.delete(0, "end")
            m.add_command(label="Edit…  (F2)", command=self._edit_tx)
            m.add_command(label="Toggle cleared",
                          command=self._toggle_cleared)
            m.add_separator()
            m.add_command(label="Delete  (Del)", command=self._delete_tx)
            aura.style_menu(m)
            try:
                m.tk_popup(event.x_root, event.y_root)
            finally:
                try:
                    m.grab_release()
                except Exception:
                    pass

        def _refresh_transactions(self):
            if not hasattr(self, "tx_tree"):
                return
            cats = ["All categories"] + self.db.category_names()
            self.tx_cat_filter.configure(values=cats)
            month = self.tx_month.get().strip() or None
            cat = self.tx_cat_filter.get().strip()
            cat = None if cat in ("", "All categories") else cat
            query = self.tx_search.get().strip().lower() \
                if hasattr(self, "tx_search") else ""
            for row in self.tx_tree.get_children():
                self.tx_tree.delete(row)
            try:
                txns = self.db.list_transactions(month=month, category=cat,
                                                 account=self._account)
                has_any = bool(self.db.list_transactions())
            except BudgetBookError as ex:
                self.set_error(str(ex))
                return
            if query:
                txns = [t for t in txns if query in (t["payee"] or "").lower()
                        or query in (t["notes"] or "").lower()]
            for t in txns:
                self.tx_tree.insert(
                    "", "end", iid=str(t["id"]),
                    values=(t["date"], t["payee"], t["category"] or "-",
                            t["account"] or "-", f"{t['amount']:.2f}",
                            "✓" if t["cleared"] else ""))
            if has_any:
                self.empty_tx.place_forget()
                self._tx_wrap.grid()
            else:
                self._tx_wrap.grid_remove()
                self.empty_tx.place(relx=0, rely=0.08, relwidth=1,
                                    relheight=0.9)
                self.empty_tx.lift()
            total = sum(t["amount"] for t in txns)
            scope = self._account or "all accounts"
            self.set_status(f"{len(txns)} transaction(s) in {scope}"
                            f"   ·   net {total:+.2f}")
            self._refresh_accounts_sidebar()

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

            theme = self.theme

            def work():
                out = os.path.join(self._tmpdir, "budget.png")
                return reports.render_chart("budget", rows, out, theme=theme)

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
            parent.grid_columnconfigure(0, weight=1)
            parent.grid_rowconfigure(1, weight=1)
            tb = aura.Toolbar(parent)
            tb.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            self.rep_seg = aura.SegmentedControl(
                tb, values=[lbl for lbl, _k in REPORT_KINDS], width=300,
                command=lambda _v: self._report_from_seg())
            self.rep_seg.set(REPORT_KINDS[0][0])
            tb.add(self.rep_seg)
            self.rep_month = aura.AuraEntry(tb, placeholder="YYYY-MM",
                                            width=90, height=32)
            self.rep_month.bind("<Return>", lambda e: self._report_from_seg())
            tb.add(self.rep_month)
            tb.add_right(aura.AuraButton(tb, "Export PNG…", kind="ghost",
                                         height=32,
                                         command=self._export_report))

            self.rep_canvas = tk.Label(parent, bd=0, text="")
            self.rep_canvas.grid(row=1, column=0, sticky="nsew")
            aura.track(self.rep_canvas, "canvas")

        def _report_from_seg(self):
            label = self.rep_seg.get()
            for lbl, kind in REPORT_KINDS:
                if lbl == label:
                    self._report(kind)
                    return

        def _refresh_reports(self):
            # auto-render the selected chart when the section shows
            if not self._busy and not self._last_report:
                self._report_from_seg()

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

            theme = self.theme

            def work():
                out = os.path.join(self._tmpdir, f"{kind}.png")
                reports.render_chart(kind, data, out, theme=theme)
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

        # ---- About section
        def _panel_about(self, frame):
            card = aura.Card(frame, title="About BudgetBook")
            card.pack(fill="x")
            aura.Heading(card.body, APP_NAME).pack(anchor="w")
            aura.Caption(card.body, f"Version {APP_VERSION}").pack(
                anchor="w", pady=(0, 10))
            ctk.CTkLabel(
                card.body, font=aura.font(), justify="left", anchor="w",
                wraplength=560,
                text="A fast, fully-offline personal-finance manager — "
                     "import bank CSV/OFX, budget by category, auto-"
                     "categorize and chart spending, cash flow and net "
                     "worth.\n\n100% AI-built, open source, published on "
                     "QuickOpen. Nothing is ever uploaded anywhere."
                ).pack(anchor="w")
            aura.Caption(card.body,
                         "Shortcuts: Ctrl+N add · Ctrl+F search · F2 edit · "
                         "Delete remove · Ctrl+, settings · Ctrl+\\ "
                         "sidebar").pack(anchor="w", pady=(10, 0))
            aura.Caption(card.body,
                         "Licensed under Apache-2.0. Built on permissive "
                         "libraries: ofxparse, matplotlib, CustomTkinter.",
                         wraplength=560, justify="left").pack(
                anchor="w", pady=(10, 4))
            aura.AuraButton(card.body, "Project page: quickopen.ai",
                            kind="ghost",
                            command=lambda: open_with_default_app(
                                PROJECT_URL)).pack(anchor="w", pady=(6, 0))

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
