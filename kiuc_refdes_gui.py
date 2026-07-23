#!/usr/bin/env python3
# KIUC - KiCad Ulticap Schematic Converter
# Copyright (C) 2026 Snieffy
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://gnu.org>.
"""
kiuc_refdes_gui.py — standalone GUI for kiuc_refdes.py.

Standalone GUI for the non-digit-ending reference-designator fix (see kiuc_refdes.py
for the full rationale) be tested completely independently of the main
the KIUC converter GUI (kiuc_gui.py).

All .py files must be in the same folder.
Requires Python 3.8+ with tkinter (standard on Windows/macOS).
On Linux: sudo apt install python3-tk

Workflow:
  1. Select the root .SCH file. The full sheet hierarchy is discovered
     automatically (every sheet transitively reachable via FILE=
     references) and listed, so what was found can be checked before
     anything else happens -- no need to remember and re-select every
     sub-sheet by hand.
  2. Offending reference designators (non-digit-ending, or a unit-letter
     suffix on a symbol that isn't genuinely multi-unit) are detected and
     listed immediately, with the new refdes each would get.
  3. A matching .DDF is optional; if one exists alongside the SCH file it
     is pre-filled automatically, but can be changed, browsed for, or left
     empty (SCH-only fix).
  4. Nothing is written until "Apply" is clicked. A log file documenting
     what was found is written automatically as soon as detection runs --
     even if Apply is never clicked -- and is updated with the full
     change/file list if Apply is used. Originals are never modified;
     every output is a new, separately-named copy.
"""
from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
except ImportError:
    print("ERROR: tkinter not available.  On Linux: sudo apt install python3-tk")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent.resolve()
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from kiuc_refdes import (
        _load_hierarchy, detect_offending_refdes, compute_new_refdes,
        _all_refdes, reannotate_hierarchy, write_log, ReannotateResult,
    )
except ImportError as e:
    print(f"ERROR: could not import kiuc_refdes.py: {e}")
    sys.exit(1)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('Refdes Reannotate')
        self.geometry('900x620')
        self.minsize(680, 460)

        self._sch_path: Path | None = None
        self._sheets: dict = {}
        self._offending: list = []
        self._last_result: ReannotateResult | None = None

        self._build_widgets()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_widgets(self):
        pad = dict(padx=8, pady=6)

        # ── Top row: SCH / DDF / Output side-by-side, equal width ──────────
        frm_top = ttk.Frame(self)
        frm_top.pack(fill='x', padx=8, pady=(8, 0))
        for col in (0, 1, 2):
            frm_top.columnconfigure(col, weight=1, uniform='top')

        # 1. Root SCH file (+ discovered hierarchy underneath, compact)
        frm_sch = ttk.LabelFrame(frm_top, text='1. Root .SCH file')
        frm_sch.grid(row=0, column=0, padx=(0, 4), sticky='nsew')
        self._sch_var = tk.StringVar()
        ttk.Entry(frm_sch, textvariable=self._sch_var, state='readonly').pack(
            fill='x', padx=8, pady=(8, 4))
        ttk.Button(frm_sch, text='Browse…', command=self._browse_sch).pack(
            anchor='w', padx=8, pady=(0, 4))
        ttk.Label(frm_sch, text='Discovered hierarchy:').pack(
            anchor='w', padx=8, pady=(4, 0))
        self._hier_list = tk.Listbox(frm_sch, height=3)
        self._hier_list.pack(fill='x', padx=8, pady=(0, 8))

        # 2. Matching DDF file (optional)
        frm_ddf = ttk.LabelFrame(frm_top, text='2. Matching .DDF (optional)')
        frm_ddf.grid(row=0, column=1, padx=4, sticky='nsew')
        self._ddf_var = tk.StringVar()
        ttk.Entry(frm_ddf, textvariable=self._ddf_var).pack(
            fill='x', padx=8, pady=(8, 4))
        frm_ddf_btns = ttk.Frame(frm_ddf)
        frm_ddf_btns.pack(anchor='w', padx=8, pady=(0, 8))
        ttk.Button(frm_ddf_btns, text='Browse…', command=self._browse_ddf).pack(
            side='left')
        ttk.Button(frm_ddf_btns, text='Clear',
                  command=lambda: self._ddf_var.set('')).pack(side='left', padx=(4, 0))

        # 3. Output folder + suffix
        frm_out = ttk.LabelFrame(frm_top, text='3. Output')
        frm_out.grid(row=0, column=2, padx=(4, 0), sticky='nsew')
        ttk.Label(frm_out, text='Folder:').pack(anchor='w', padx=8, pady=(8, 0))
        self._out_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self._out_var).pack(fill='x', padx=8, pady=(0, 4))
        ttk.Button(frm_out, text='Browse…', command=self._browse_out).pack(
            anchor='w', padx=8, pady=(0, 4))
        ttk.Label(frm_out, text='Suffix:').pack(anchor='w', padx=8, pady=(4, 0))
        self._suffix_var = tk.StringVar(value='_REANNOT')
        ttk.Entry(frm_out, textvariable=self._suffix_var, width=16).pack(
            anchor='w', padx=8, pady=(0, 8))

        # ── Actions row, directly above the resizable list/log split ───────
        frm_actions = ttk.Frame(self)
        frm_actions.pack(fill='x', padx=8, pady=8)
        self._apply_btn = ttk.Button(frm_actions, text='Apply (write renamed copies)',
                                     command=self._on_apply, state='disabled')
        self._apply_btn.pack(side='left')
        self._status_var = tk.StringVar(value='Select a root .SCH file to begin.')
        ttk.Label(frm_actions, textvariable=self._status_var).pack(side='left', padx=12)

        # ── Offending list / Log: a resizable vertical split so either pane
        # can be given more room by dragging the sash, rather than a fixed
        # allocation that's wrong for some hierarchies/screens.
        paned = ttk.PanedWindow(self, orient='vertical')
        paned.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        frm_off = ttk.LabelFrame(paned, text='Offending reference designators')
        columns = ('refdes', 'new', 'sheet', 'symbol')
        self._tree = ttk.Treeview(frm_off, columns=columns, show='headings', height=5)
        for col, label, w in (('refdes', 'Refdes', 100), ('new', '→ New', 100),
                              ('sheet', 'Sheet', 160), ('symbol', 'Symbol', 160)):
            self._tree.heading(col, text=label)
            self._tree.column(col, width=w, anchor='w')
        self._tree.pack(fill='both', expand=True, padx=8, pady=8)
        paned.add(frm_off, weight=1)

        frm_log = ttk.LabelFrame(paned, text='Log')
        self._log = scrolledtext.ScrolledText(frm_log, height=6, wrap='word')
        self._log.pack(fill='both', expand=True, padx=8, pady=8)
        self._log.configure(state='disabled')
        paned.add(frm_log, weight=1)

    # ── logging helper ───────────────────────────────────────────────────

    def _log_write(self, msg: str):
        self._log.configure(state='normal')
        self._log.insert('end', msg + '\n')
        self._log.see('end')
        self._log.configure(state='disabled')

    # ── file pickers ─────────────────────────────────────────────────────

    def _browse_sch(self):
        path = filedialog.askopenfilename(
            title='Select root .SCH file',
            filetypes=[('Ulticap schematic', '*.SCH;*.sch'), ('All files', '*.*')])
        if not path:
            return
        self._sch_path = Path(path)
        self._sch_var.set(str(self._sch_path))
        self._out_var.set(str(self._sch_path.parent))

        # Pre-fill a matching .DDF if one exists alongside the SCH file.
        candidate = self._sch_path.with_suffix('.DDF')
        if not candidate.exists():
            candidate = self._sch_path.with_suffix('.ddf')
        self._ddf_var.set(str(candidate) if candidate.exists() else '')

        self._run_detection()

    def _browse_ddf(self):
        path = filedialog.askopenfilename(
            title='Select matching .DDF file',
            filetypes=[('Ultiboard DDF', '*.DDF;*.ddf'), ('All files', '*.*')])
        if path:
            self._ddf_var.set(path)

    def _browse_out(self):
        d = filedialog.askdirectory(title='Select output folder')
        if d:
            self._out_var.set(d)

    # ── detection (dry run) ──────────────────────────────────────────────

    def _run_detection(self):
        if self._sch_path is None:
            return
        self._status_var.set('Scanning hierarchy…')
        self.update_idletasks()

        try:
            self._sheets = _load_hierarchy(self._sch_path)
            self._offending = detect_offending_refdes(self._sheets)
        except Exception as e:
            messagebox.showerror('Error', f'Failed to scan {self._sch_path.name}:\n{e}')
            traceback.print_exc()
            self._status_var.set('Scan failed.')
            return

        self._hier_list.delete(0, 'end')
        for stem, sh in self._sheets.items():
            self._hier_list.insert('end', f'{sh.name}   ({len(sh.components)} components)')

        self._tree.delete(*self._tree.get_children())
        if self._offending:
            all_refdes = _all_refdes(self._sheets)
            seen_old = set()
            for o in self._offending:
                if o.refdes not in seen_old:
                    seen_old.add(o.refdes)
                    new = compute_new_refdes(o.refdes, all_refdes)
                    all_refdes.add(new)
                else:
                    new = ''   # already computed above for this exact refdes text
                self._tree.insert('', 'end', values=(o.refdes, new, o.sheet_name, o.symbol_name))
            self._status_var.set(
                f'{len(self._offending)} offending component instance(s) found.')
            self._apply_btn.configure(state='normal')
        else:
            self._status_var.set('No non-digit-ending reference designators found.')
            self._apply_btn.configure(state='disabled')

        # Log is written immediately after detection, regardless of whether
        # Apply is ever used -- this is the durable record even if the
        # user just closes the app having decided not to proceed.
        result = ReannotateResult(offending=self._offending)
        self._last_result = result
        self._write_log_file(result)
        self._render_log(result)

    # ── apply ─────────────────────────────────────────────────────────────

    def _on_apply(self):
        if self._sch_path is None or not self._offending:
            return
        ddf = self._ddf_var.get().strip() or None
        out_dir = self._out_var.get().strip() or str(self._sch_path.parent)
        suffix = self._suffix_var.get().strip() or '_REANNOT'

        self._apply_btn.configure(state='disabled')
        self._status_var.set('Writing renamed copies…')
        self.update_idletasks()

        def worker():
            try:
                result = reannotate_hierarchy(self._sch_path, ddf_path=ddf,
                                              out_dir=out_dir, suffix=suffix)
            except Exception as e:
                self.after(0, lambda: self._on_apply_error(e))
                return
            self.after(0, lambda: self._on_apply_done(result))

        threading.Thread(target=worker, daemon=True).start()

    def _on_apply_error(self, e: Exception):
        traceback.print_exc()
        messagebox.showerror('Error', f'Reannotation failed:\n{e}')
        self._status_var.set('Reannotation failed.')
        self._apply_btn.configure(state='normal')

    def _on_apply_done(self, result: ReannotateResult):
        self._last_result = result
        self._write_log_file(result)
        self._render_log(result)
        self._status_var.set(
            f'Applied — {len(result.sch_files_written)} SCH file(s)'
            + (' + DDF' if result.ddf_file_written else '') + ' written.')
        self._apply_btn.configure(state='normal')
        if result.ddf_file_written:
            messagebox.showinfo(
                'Reannotation applied',
                'Renamed copies written.\n\n'
                'A fresh KIUB conversion of the new DDF file is required.')
        else:
            messagebox.showinfo('Reannotation applied', 'Renamed copies written.')

    # ── log helpers ──────────────────────────────────────────────────────

    def _write_log_file(self, result: ReannotateResult):
        if self._sch_path is None:
            return
        suffix = self._suffix_var.get().strip() or '_REANNOT'
        out_dir = Path(self._out_var.get().strip() or self._sch_path.parent)
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            log_path = out_dir / f'{self._sch_path.stem}{suffix}_log.txt'
            write_log(result, log_path, root_sch_name=self._sch_path.name)
            self._log_path = log_path
        except Exception as e:
            self._log_write(f'WARNING: could not write log file: {e}')

    def _render_log(self, result: ReannotateResult):
        self._log.configure(state='normal')
        self._log.delete('1.0', 'end')
        self._log.configure(state='disabled')

        if not result.offending:
            self._log_write('No non-digit-ending reference designators found.')
            return

        self._log_write(f'{len(result.offending)} offending component instance(s) found:')
        for o in result.offending:
            self._log_write(f'  {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})')

        if result.applied:
            self._log_write('')
            self._log_write('Refdes changes:')
            for ch in result.changes:
                self._log_write(f'  {ch.old}  ->  {ch.new}')
            self._log_write('')
            self._log_write('Files written:')
            for p in result.sch_files_written:
                self._log_write(f'  {p}')
            if result.ddf_file_written:
                self._log_write(f'  {result.ddf_file_written}')
                self._log_write('')
                self._log_write('A fresh KIUB conversion of the new DDF file is required.')
            if result.ddf_refdes_not_found:
                self._log_write('')
                self._log_write('WARNING: not found in DDF (*C line): '
                                + ', '.join(result.ddf_refdes_not_found))
            if result.errors:
                self._log_write('')
                self._log_write('Errors:')
                for e in result.errors:
                    self._log_write(f'  {e}')
        else:
            self._log_write('')
            self._log_write('Not yet applied. Click "Apply" to write renamed copies, '
                            'or just close this window to leave the schematic unchanged '
                            '(this log is already saved either way).')

        if hasattr(self, '_log_path'):
            self._log_write('')
            self._log_write(f'Log written to: {self._log_path}')


if __name__ == '__main__':
    App().mainloop()
