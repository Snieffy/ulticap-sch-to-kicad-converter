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
kiuc_gui.py — GUI for the Ulticap ASCII → KiCad converter.

All .py files must be in the same folder.
Requires Python 3.8+ with tkinter (standard on Windows/macOS).
On Linux: sudo apt install python3-tk
"""
from __future__ import annotations

import configparser
import subprocess
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
    from kiuc_ascii  import parse_sch, force_v5_header, collect_hierarchy_paths
    from kiuc_writer   import write_schematic, write_block_library, \
                               check_missing_sheets, \
                               check_pkg_type_consistency, \
                               dump_schematic, dump_symbols, \
                               set_use_kicad_colors, \
                               TUNING_SPEC, get_tuning, set_tuning
    from kiuc_model  import Schematic
    from kiuc_refdes import (detect_offending_refdes, reannotate_hierarchy,
                                   write_log, ReannotateResult)
except ImportError as e:
    root = tk.Tk(); root.withdraw()
    messagebox.showerror("Missing files",
        f"Cannot import converter modules:\n{e}\n\n"
        "Make sure all .py files are in the same folder.")
    sys.exit(1)


# ── KiCad path config ──────────────────────────────────────────────────────────

_CONFIG_FILE    = SCRIPT_DIR / 'kiuc.ini'
_CONFIG_SECTION = 'kicad'
_CONFIG_KEY     = 'executable'

def _load_kicad_exe() -> str:
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding='utf-8')
    path = cfg.get(_CONFIG_SECTION, _CONFIG_KEY, fallback='').strip()
    return path if path and Path(path).is_file() else ''

def _save_kicad_exe(path: str) -> None:
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding='utf-8')
    if not cfg.has_section(_CONFIG_SECTION):
        cfg.add_section(_CONFIG_SECTION)
    cfg.set(_CONFIG_SECTION, _CONFIG_KEY, path)
    with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
        cfg.write(f)


# ── Fine-tuning config (persisted alongside the KiCad path) ───────────────────

_TUNING_SECTION = 'tuning'

def _load_tuning() -> dict:
    """Load saved fine-tuning values from kiuc.ini. Any name not
    present in the file (new install, or a newly-added tunable) is simply
    left out, so the caller should overlay this onto kiuc_writer's
    built-in defaults rather than assume every key is present."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding='utf-8')
    values = {}
    if cfg.has_section(_TUNING_SECTION):
        for name, _default, _lo, _hi, _desc, _cat in TUNING_SPEC:
            if cfg.has_option(_TUNING_SECTION, name):
                try:
                    values[name] = cfg.getfloat(_TUNING_SECTION, name)
                except ValueError:
                    pass  # corrupted entry; fall back to current default
    return values

def _save_tuning(values: dict) -> None:
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding='utf-8')
    if not cfg.has_section(_TUNING_SECTION):
        cfg.add_section(_TUNING_SECTION)
    for name, value in values.items():
        cfg.set(_TUNING_SECTION, name, repr(value))
    with open(_CONFIG_FILE, 'w', encoding='utf-8') as f:
        cfg.write(f)

def _browse_kicad_exe(parent=None) -> str:
    if sys.platform.startswith('win'):
        filetypes = [('Executable', '*.exe'), ('All files', '*.*')]
    else:
        filetypes = [('All files', '*')]
    path = filedialog.askopenfilename(
        parent=parent,
        title='Locate the KiCad executable (kicad or kicad.exe)',
        filetypes=filetypes,
    )
    return str(Path(path)) if path else ''


# ── log tag colours (matching KIUB) ───────────────────────────────────────────

_LOG_BG   = '#1e1e1e'
_LOG_FG   = '#d4d4d4'
_TAG_INFO = '#9cdcfe'   # blue   – filenames, section headers
_TAG_OK   = '#4ec9b0'   # teal   – success messages
_TAG_WARN = '#dcdcaa'   # yellow – warnings
_TAG_ERR  = '#f44747'   # red    – errors / tracebacks

# Log buffer drain interval (ms). Background threads append to the buffer;
# this is how often the main thread checks it and flushes to the widget
# in one batched update. Lower = snappier log, higher = less Tk overhead.
_LOG_POLL_MS = 50


# ── tooltip helper ────────────────────────────────────────────────────────────

class _ToolTip:
    """Simple hover tooltip for any widget."""
    def __init__(self, widget, text: str):
        self._widget = widget
        self._text   = text
        self._tip    = None
        widget.bind('<Enter>', self._show)
        widget.bind('<Leave>', self._hide)

    def _show(self, _event=None):
        x = self._widget.winfo_rootx() + 20
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tip = tk.Toplevel(self._widget)
        self._tip.wm_overrideredirect(True)
        self._tip.wm_geometry(f'+{x}+{y}')
        tk.Label(self._tip, text=self._text, justify='left',
                 background='#ffffe0', relief='solid', borderwidth=1,
                 font=('Segoe UI', 8), wraplength=420).pack()

    def _hide(self, _event=None):
        if self._tip:
            self._tip.destroy()
            self._tip = None


# ── refdes reannotation pre-conversion dialog ───────────────────────────────

class _RefdesFixDialog(tk.Toplevel):
    """Modal dialog shown before conversion when reference designators are
    found that KiCad will not accept as annotated (see kiuc_refdes.py).

    Always runs on the main thread, before the background conversion thread
    is started (_do_convert runs in a worker thread via _run_in_thread, and
    Tkinter dialogs are not safe to invoke from there) -- this mirrors the
    existing V5.x-header-fix confirmation in _run_convert, which is also
    a main-thread, pre-thread-launch check.

    Sets self.choice to one of 'fix', 'continue', 'cancel' and self.ddf_path
    (str or None) before the dialog closes.
    """
    def __init__(self, parent, offending, default_ddf: Path | None):
        super().__init__(parent)
        self.title('Reference designators need attention')
        self.transient(parent)
        self.resizable(False, False)
        self.choice = 'cancel'
        self.ddf_path_var = tk.StringVar(value=str(default_ddf) if default_ddf else '')

        pad = dict(padx=12, pady=6)

        msg = (
            f'{len(offending)} reference designator(s) do not end in a digit. '
            'KiCad will treat these as unannotated: ERC will flag them, and '
            'netlist generation will be blocked until fixed.'
        )
        ttk.Label(self, text=msg, wraplength=480, justify='left').pack(
            anchor='w', **pad)

        frm_list = ttk.Frame(self)
        frm_list.pack(fill='both', expand=True, padx=12, pady=(0, 6))
        lb = tk.Listbox(frm_list, height=min(6, max(3, len(offending))))
        lb.pack(fill='both', expand=True)
        for o in offending:
            lb.insert('end', f'  {o.refdes}   (symbol {o.symbol_name}, sheet {o.sheet_name})')

        ttk.Label(self, text=
                 'A fix writes new, renamed copies (suffix "_REANNOT") and converts '
                 'those instead -- your original files are never modified.',
                 wraplength=480, justify='left').pack(anchor='w', **pad)

        frm_ddf = ttk.LabelFrame(self, text='Matching .DDF (optional, for keeping the PCB in sync)')
        frm_ddf.pack(fill='x', padx=12, pady=(0, 6))
        ttk.Entry(frm_ddf, textvariable=self.ddf_path_var).pack(
            side='left', fill='x', expand=True, padx=(8, 4), pady=8)
        ttk.Button(frm_ddf, text='Browse…', command=self._browse_ddf).pack(
            side='left', padx=(0, 8), pady=8)

        frm_btns = ttk.Frame(self)
        frm_btns.pack(fill='x', padx=12, pady=(0, 12))
        ttk.Button(frm_btns, text='Fix automatically',
                  command=self._on_fix).pack(side='left')
        ttk.Button(frm_btns, text='Continue without fixing',
                  command=self._on_continue).pack(side='left', padx=8)
        ttk.Button(frm_btns, text='Cancel conversion',
                  command=self._on_cancel).pack(side='right')

        self.protocol('WM_DELETE_WINDOW', self._on_cancel)
        self.grab_set()

    def _browse_ddf(self):
        path = filedialog.askopenfilename(
            title='Select matching .DDF file', parent=self,
            filetypes=[('Ultiboard DDF', '*.DDF;*.ddf'), ('All files', '*.*')])
        if path:
            self.ddf_path_var.set(path)

    def _on_fix(self):
        self.choice = 'fix'
        self.destroy()

    def _on_continue(self):
        self.choice = 'continue'
        self.destroy()

    def _on_cancel(self):
        self.choice = 'cancel'
        self.destroy()


# ── main application ──────────────────────────────────────────────────────────

class _TuningDialog(tk.Toplevel):
    """Fine-tuning pop-up. Fields are generated entirely from kiuc_writer's
    TUNING_SPEC table -- adding a new tunable to that table is all that's
    needed for it to appear here; no layout changes required.

    Entries are grouped into two sections by their 'category' tag:
    'empirical' (tuned by eye against KiCad's visual output) and 'ulticap'
    (mirror an actual Ulticap/ULTIC.SET value, editable in case the user's
    own Ulticap install differs from the default).

    Always opened on the main thread (button command), so no thread-safety
    concerns -- mirrors _RefdesFixDialog.
    """
    _SECTION_INTRO = {
        'empirical': "These values are empirically tuned against KiCad's visual "
                     "output. Changes apply to the next Convert / Dump and are "
                     "saved to kiuc.ini.",
        'ulticap':   "These mirror actual Ulticap ULTIC.SET values (shown here in "
                     "Ulticap's own units, matching what you'd see in Ulticap "
                     "itself). Only change these if your Ulticap install uses "
                     "non-default settings.",
    }
    _SECTION_TITLE = {
        'empirical': 'Empirically-tuned values',
        'ulticap':   'Ulticap settings',
    }

    def __init__(self, parent):
        super().__init__(parent)
        self.title('Fine-tuning')
        self.transient(parent)
        self.resizable(False, False)
        self.saved = False

        current = get_tuning()
        self._vars: dict[str, tk.StringVar] = {}

        self._specs = {name: (default, lo, hi, desc, cat)
                       for name, default, lo, hi, desc, cat in TUNING_SPEC}

        # group entries by category, preserving TUNING_SPEC's own order
        by_category: dict[str, list] = {}
        for entry in TUNING_SPEC:
            by_category.setdefault(entry[5], []).append(entry)

        row = 0
        for cat in ('empirical', 'ulticap'):
            entries = by_category.get(cat)
            if not entries:
                continue

            ttk.Label(self, text=self._SECTION_TITLE.get(cat, cat),
                     font=('Segoe UI', 10, 'bold')).grid(
                row=row, column=0, columnspan=3, sticky='w',
                padx=12, pady=(12, 2))
            row += 1
            ttk.Label(self, text=self._SECTION_INTRO.get(cat, ''),
                     wraplength=480, justify='left').grid(
                row=row, column=0, columnspan=3, sticky='w', padx=12, pady=(0, 8))
            row += 1

            for name, default, lo, hi, desc, _cat in entries:
                ttk.Label(self, text=name, font=('Consolas', 9, 'bold')).grid(
                    row=row, column=0, sticky='nw', padx=(12, 6), pady=(6, 0))

                var = tk.StringVar(value=str(current.get(name, default)))
                self._vars[name] = var
                ent = ttk.Entry(self, textvariable=var, width=10, font=('Consolas', 9))
                ent.grid(row=row, column=1, sticky='nw', pady=(6, 0))

                ttk.Label(self, text=f'(default {default}, range {lo}–{hi})',
                         foreground='#888').grid(row=row, column=2, sticky='nw',
                                                 padx=(6, 12), pady=(6, 0))
                row += 1
                ttk.Label(self, text=desc, wraplength=480, justify='left',
                         foreground='#555').grid(
                    row=row, column=0, columnspan=3, sticky='w', padx=12, pady=(0, 4))
                row += 1

            ttk.Separator(self, orient='horizontal').grid(
                row=row, column=0, columnspan=3, sticky='ew', padx=12, pady=(4, 0))
            row += 1

        frm_btns = ttk.Frame(self)
        frm_btns.grid(row=row, column=0, columnspan=3, sticky='ew', padx=12, pady=(8, 12))
        ttk.Button(frm_btns, text='Reset to defaults',
                  command=self._on_reset).pack(side='left')
        ttk.Button(frm_btns, text='Cancel',
                  command=self._on_cancel).pack(side='right')
        ttk.Button(frm_btns, text='Save',
                  command=self._on_save).pack(side='right', padx=8)

        self.protocol('WM_DELETE_WINDOW', self._on_cancel)
        self.grab_set()

    def _on_reset(self):
        for name, (default, _lo, _hi, _desc, _cat) in self._specs.items():
            self._vars[name].set(str(default))

    def _on_save(self):
        values = {}
        for name, (default, lo, hi, _desc, _cat) in self._specs.items():
            raw = self._vars[name].get().strip()
            try:
                v = float(raw)
            except ValueError:
                messagebox.showerror('Invalid value',
                    f'{name}: "{raw}" is not a number.', parent=self)
                return
            if not (lo <= v <= hi):
                ok = messagebox.askyesno('Value out of suggested range',
                    f'{name} = {v} is outside the suggested range '
                    f'{lo}\u2013{hi} (default {default}).\n\nUse it anyway?',
                    parent=self)
                if not ok:
                    return
            values[name] = v

        set_tuning(values)
        _save_tuning(values)
        self.saved = True
        self.destroy()

    def _on_cancel(self):
        self.destroy()


class App(tk.Tk):
    def __init__(self, initial_path=None):
        super().__init__()
        self.title('KIUC — Ulticap → KiCad Converter')
        self.resizable(True, True)
        self.minsize(680, 560)

        self._sch_files: list = []       # list of Path: root + auto-discovered hierarchy
        self._last_output_sch: str = ''  # set after successful conversion
        self._kicad_exe: str = _load_kicad_exe()

        self._v5_var = tk.BooleanVar(value=False)
        self._last_dump_suffix: str = '_log'  # updated by each dump action

        # Set while the reannotation-fix flow updates self._infile_var to
        # reflect renamed files, so that update doesn't re-trigger discovery
        # and re-log everything a second time. See _on_infile_changed.
        self._suppress_infile_trace: bool = False

        # Apply any tuning values saved from a previous session. Names not
        # present in the ini (fresh install, or a tunable added since) are
        # simply left at kiuc_writer's built-in defaults.
        set_tuning(_load_tuning())

        self._build_ui()

        if initial_path:
            self._infile_var.set(str(initial_path))

        # Ask for KiCad path on first run (after window is visible)
        if not self._kicad_exe:
            self.after(200, self._ask_kicad_exe)

    # ── UI construction ────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)  # log row expands

        # ── Input section ──────────────────────────────────────────────────────
        # Single file, like kiub_gui.py: sub-sheets of a hierarchical .SCH
        # design are discovered and loaded automatically (see kiuc_ascii's
        # collect_hierarchy_paths), so there is nothing left for the user to
        # manually add/remove/reorder -- doing so only risked mixing sheets
        # from two unrelated designs into a single, incorrectly-merged
        # conversion. What was discovered, and any missing sub-sheet, is
        # reported in the Log window below instead.
        frm_in = ttk.LabelFrame(outer, text=' Input ')
        frm_in.grid(row=0, column=0, sticky='ew', pady=(0, 6))
        frm_in.columnconfigure(1, weight=1)

        ttk.Label(frm_in, text='File:').grid(
            row=0, column=0, sticky='w', padx=6, pady=4)
        self._infile_var = tk.StringVar()
        in_entry = ttk.Entry(frm_in, textvariable=self._infile_var,
                             font=('Consolas', 9))
        in_entry.grid(row=0, column=1, sticky='ew', padx=(0, 4), pady=4)
        self._infile_var.trace_add('write', self._on_infile_changed)
        ttk.Button(frm_in, text='Browse…',
                   command=self._browse_infile).grid(row=0, column=2, padx=(0, 6), pady=4)
        _ToolTip(in_entry,
            'Select the main .SCH file of a hierarchical design, or a .BLK '
            'block-library file.\n\n'
            'Sub-sheets referenced by the main sheet are discovered and '
            'loaded automatically -- see the Log window for what was found, '
            'and for a warning if a referenced sub-sheet cannot be located.')

        # SCH V5.x toggle
        v5_cb = ttk.Checkbutton(frm_in, text='SCH V5.x',
                                variable=self._v5_var)
        v5_cb.grid(row=1, column=0, columnspan=2, sticky='w', padx=6, pady=(0, 6))
        _ToolTip(v5_cb,
            'Enable when converting files created with Ulticap V5.x (Windows 95 version).\n\n'
            'V5.x files have arc encoding differences that require correction before\n'
            'conversion. When this option is active, the version header in each SCH\n'
            'file is rewritten from "4 xx xxxxxxxx" to "5 00 00000000" IN-PLACE\n'
            'before parsing. Make a backup of your files before using this option.')

        # ── Output section ─────────────────────────────────────────────────────
        frm_out = ttk.LabelFrame(outer, text=' Output ')
        frm_out.grid(row=1, column=0, sticky='ew', pady=(0, 6))
        frm_out.columnconfigure(1, weight=1)

        ttk.Label(frm_out, text='Folder:').grid(
            row=0, column=0, sticky='w', padx=6, pady=4)
        self._out_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self._out_var,
                  font=('Consolas', 9)).grid(
            row=0, column=1, sticky='ew', padx=(0, 4), pady=4)
        ttk.Button(frm_out, text='Browse…',
                   command=self._browse_out).grid(row=0, column=2, padx=(0, 6), pady=4)

        ttk.Label(frm_out, text='Base name:').grid(
            row=1, column=0, sticky='w', padx=6, pady=(0, 6))
        self._name_var = tk.StringVar()
        ttk.Entry(frm_out, textvariable=self._name_var,
                  font=('Consolas', 9), width=30).grid(
            row=1, column=1, sticky='w', pady=(0, 6))

        # KiCad-default-palette toggle
        self._kicad_colors_var = tk.BooleanVar(value=False)
        kc_cb = ttk.Checkbutton(frm_out, text='Use KiCad default colour palette',
                                variable=self._kicad_colors_var)
        kc_cb.grid(row=2, column=0, columnspan=2, sticky='w', padx=6, pady=(0, 6))
        _ToolTip(kc_cb,
            'Off (default): graphics and text use Ulticap\'s measured colour\n'
            'palette, emitted as explicit colour overrides in the KiCad output.\n\n'
            'On: no colour overrides are emitted at all -- every item inherits\n'
            'its colour from KiCad\'s active colour theme instead. Use this if\n'
            'the Ulticap palette (designed for a black background) is hard to\n'
            'read against KiCad\'s default white background.')

        # Block mode toggle
        self._block_var = tk.BooleanVar(value=False)
        blk_cb = ttk.Checkbutton(frm_out, text='Block mode (.kicad_blocks)',
                                 variable=self._block_var)
        blk_cb.grid(row=3, column=0, columnspan=2, sticky='w', padx=6, pady=(0, 6))
        _ToolTip(blk_cb,
            'Convert to a KiCad block library instead of a schematic project.\n\n'
            'Output: <name>.kicad_blocks/ with one <sheet>.kicad_block/ per sheet.\n'
            'No .kicad_pro or .kicad_wks files are written.\n'
            'Title symbols (TITLE / TITLE_REV) are omitted.\n\n'
            'Use this when converting a design intended to be imported\n'
            'as a reusable block into a KiCad schematic.')

        # ── Action buttons ─────────────────────────────────────────────────────
        frm_act = ttk.Frame(outer)
        frm_act.grid(row=2, column=0, sticky='ew', pady=(0, 4))

        ttk.Button(frm_act, text='Convert',
                   command=self._run_convert).pack(side='left', padx=(0, 6))
        ttk.Button(frm_act, text='Dump / Inspect',
                   command=self._run_dump).pack(side='left', padx=(0, 6))
        ttk.Button(frm_act, text='Dump Symbols',
                   command=self._run_dump_symbols).pack(side='left', padx=(0, 4))
        ttk.Button(frm_act, text='Save log…',
                   command=self._run_save_log).pack(side='left', padx=(0, 18))

        self._open_btn = ttk.Button(frm_act, text='⎋ Open in KiCad',
                                    command=self._open_in_kicad,
                                    state='disabled')
        self._open_btn.pack(side='left', padx=(0, 6))

        ttk.Button(frm_act, text='⚙ KiCad Path…',
                   command=self._change_kicad_exe).pack(side='left', padx=(0, 6))

        ttk.Button(frm_act, text='⚙ Fine-tuning…',
                   command=self._open_tuning_dialog).pack(side='left')

        # ── Log ────────────────────────────────────────────────────────────────
        frm_log = ttk.LabelFrame(outer, text=' Log ')
        frm_log.grid(row=3, column=0, sticky='nsew', pady=(4, 0))
        outer.rowconfigure(3, weight=1)
        frm_log.columnconfigure(0, weight=1)
        frm_log.rowconfigure(0, weight=1)

        self._log = scrolledtext.ScrolledText(
            frm_log, font=('Consolas', 9), state='disabled',
            wrap='word', height=12,
            background=_LOG_BG, foreground=_LOG_FG,
            insertbackground=_LOG_FG)
        self._log.grid(row=0, column=0, sticky='nsew', padx=4, pady=4)
        self._log.tag_config('ok',   foreground=_TAG_OK)
        self._log.tag_config('warn', foreground=_TAG_WARN)
        self._log.tag_config('err',  foreground=_TAG_ERR)
        self._log.tag_config('info', foreground=_TAG_INFO)

        # Buffered/thread-safe log plumbing: background threads only ever
        # touch self._log_buffer (under the lock) -- never the Tk widget
        # itself. The poll loop, running on the main thread via .after(),
        # is the sole place that drains the buffer into the widget, in one
        # batched update instead of one Tk call per line.
        self._log_buffer: list[tuple[str, str]] = []
        self._log_buffer_lock = threading.Lock()
        self.after(_LOG_POLL_MS, self._poll_log_buffer)

        # ── Status bar ─────────────────────────────────────────────────────────
        self._status = tk.StringVar(value='Ready')
        ttk.Label(outer, textvariable=self._status,
                  relief='sunken', anchor='w',
                  font=('Segoe UI', 8)).grid(
            row=4, column=0, sticky='ew', pady=(4, 0))

    # ── file list management ───────────────────────────────────────────────────

    def _sync_block_mode(self):
        """Enable Block mode only while the file list contains a .BLK file;
        disable it whenever the list is empty or holds only .SCH files."""
        has_blk = any(p.suffix.upper() == '.BLK' for p in self._sch_files)
        self._block_var.set(has_blk)

    def _browse_infile(self):
        path = filedialog.askopenfilename(
            title='Select .SCH or .BLK file',
            filetypes=[('Ulticap Schematic', '*.SCH *.sch *.BLK *.blk'), ('All files', '*.*')])
        if path:
            self._infile_var.set(str(Path(path)))   # fires _on_infile_changed

    def _on_infile_changed(self, *_args):
        """Rebuild the internal file list (root + auto-discovered hierarchy)
        whenever the input file changes, and log what was found. Replaces
        the old manual Add/Remove/Move workflow -- discovery is automatic
        and only a single design is ever loaded at a time, so there is
        nothing left for the user to manage by hand.

        Guarded by self._suppress_infile_trace so the reannotation-fix flow
        (_check_refdes) can update the displayed path after writing renamed
        files without re-running discovery/logging a second time.
        """
        if self._suppress_infile_trace:
            return
        infile = self._infile_var.get().strip()
        if not infile:
            self._sch_files = []
            self._sync_block_mode()
            self._clear_output_fields()
            return
        pth = Path(infile)
        self._sch_files = [pth]
        # Auto-load the rest of a hierarchical design (mirrors the
        # viewer's automatic "Sheets" tree). Block libraries (.BLK) are
        # never hierarchical, so discovery is skipped for them. Skipped
        # entirely for a path that doesn't exist yet (e.g. mid-typing) --
        # _validate() reports a missing file at Convert time instead.
        if pth.exists() and pth.suffix.upper() == '.SCH':
            for child in collect_hierarchy_paths(pth)[1:]:
                if child not in self._sch_files:
                    self._sch_files.append(child)
                    self._log_write(f'Auto-loaded sub-sheet: {child.name}', 'info')
        self._sync_block_mode()
        self._autofill_output()
        if pth.exists() and not self._block_var.get():
            self._warn_missing_sheets()

    def _warn_missing_sheets(self):
        """Parse the currently listed sheets and log a warning for any
        sub-sheet FILE= reference that couldn't be resolved -- e.g. a file
        that was moved, renamed, or simply doesn't exist yet. Runs
        immediately on file selection so the gap is visible before Convert,
        in addition to the same check repeated at conversion time."""
        sheets = []
        for f in self._sch_files:
            sheet, _errors = parse_sch(f)
            sheets.append(sheet)
        for w in check_missing_sheets(sheets):
            self._log_write(f'WARNING: {w}', 'warn')

    def _clear_output_fields(self):
        self._out_var.set('')
        self._name_var.set('')

    def _autofill_output(self):
        """Always fill output folder and base name from the first listed file."""
        if self._sch_files:
            self._out_var.set(str(self._sch_files[0].parent))
            self._name_var.set(self._sch_files[0].stem)

    def _browse_out(self):
        d = filedialog.askdirectory(title='Select output folder')
        if d:
            self._out_var.set(d)

    # ── log helpers ────────────────────────────────────────────────────────────

    def _log_clear(self):
        # Drop any not-yet-flushed buffered lines from a previous run so
        # they can't appear after the clear once the poll loop next fires.
        with self._log_buffer_lock:
            self._log_buffer.clear()
        self._log.config(state='normal')
        self._log.delete('1.0', 'end')
        self._log.config(state='disabled')

    def _log_write(self, text, tag=''):
        """Direct, single-line write. Main-thread use only (e.g.
        _check_refdes, which runs before any worker thread is started).
        Background threads must use _log_write_buffered instead."""
        self._log.config(state='normal')
        self._log.insert('end', text + '\n', tag)
        self._log.see('end')
        self._log.config(state='disabled')

    def _log_write_buffered(self, text, tag=''):
        """Thread-safe write for use from background (worker) threads.
        Touches only the plain-Python buffer, never the Tk widget; the
        main-thread poll loop (_poll_log_buffer) drains it periodically
        in one batched update."""
        with self._log_buffer_lock:
            self._log_buffer.append((text, tag))

    def _poll_log_buffer(self):
        """Runs on the main thread via .after(). Drains any lines a
        worker thread has buffered since the last poll and writes them
        to the log widget in a single batched update, instead of one
        Tk call per line."""
        with self._log_buffer_lock:
            pending = self._log_buffer
            self._log_buffer = []
        if pending:
            self._log.config(state='normal')
            for text, tag in pending:
                self._log.insert('end', text + '\n', tag)
            self._log.see('end')
            self._log.config(state='disabled')
        self.after(_LOG_POLL_MS, self._poll_log_buffer)

    def _set_status(self, text):
        self._status.set(text)

    def _save_log_to_file(self, default_name: str):
        """Offer a Save As dialog and write the current log contents to a file."""
        path = filedialog.asksaveasfilename(
            title='Save log to file',
            defaultextension='.txt',
            initialfile=default_name,
            filetypes=[('Text file', '*.txt'), ('All files', '*.*')])
        if not path:
            return
        text = self._log.get('1.0', 'end')
        try:
            Path(path).write_text(text, encoding='utf-8')
            self._set_status(f'Saved: {Path(path).name}')
        except OSError as exc:
            messagebox.showerror('Save failed', str(exc), parent=self)

    # ── validation ─────────────────────────────────────────────────────────────

    def _validate(self):
        if not self._sch_files:
            messagebox.showwarning('No input', 'Please select a .SCH or .BLK file.')
            return False
        if not self._sch_files[0].exists():
            messagebox.showwarning('File not found',
                                   f'Input file not found:\n{self._sch_files[0]}')
            return False
        if not self._out_var.get():
            messagebox.showwarning('No output', 'Please select an output folder.')
            return False
        return True

    # ── threaded actions ───────────────────────────────────────────────────────

    def _run_in_thread(self, fn):
        t = threading.Thread(target=fn, daemon=True)
        t.start()

    def _run_convert(self):
        if not self._validate():
            return
        # If V5.x toggle is active, confirm before modifying files in-place
        if self._v5_var.get():
            names = '\n'.join(f'  • {f.name}' for f in self._sch_files)
            ok = messagebox.askyesno(
                'Apply V5.x header fix?',
                f'The SCH V5.x option will rewrite the version header of the '
                f'following files IN-PLACE before conversion:\n\n{names}\n\n'
                f'Make sure you have a backup. Proceed?',
                icon='warning')
            if not ok:
                return
        # Cleared here, before _check_refdes, so any messages it logs (the
        # reannotation summary, or a continue-without-fixing warning) are
        # the first entries in the fresh log rather than being wiped out
        # immediately afterwards.
        self._log_clear()
        if not self._check_refdes():
            return
        self._last_dump_suffix = '_log'   # ensure Save log… uses _log suffix
        self._open_btn.config(state='disabled')
        self._last_output_sch = ''
        self._run_in_thread(self._do_convert)

    def _check_refdes(self) -> bool:
        """Pre-conversion check for reference designators KiCad will not
        accept as annotated (see kiuc_refdes.py). Runs on the main
        thread, before _do_convert's worker thread is started -- like the
        V5.x-fix confirmation above, this must happen here rather than
        inside _do_convert, since Tkinter dialogs cannot safely be invoked
        from a background thread.

        Returns False if the user cancelled the conversion entirely;
        True otherwise (including when nothing was found, or the user
        chose to continue without fixing). On 'fix', self._sch_files is
        updated in place to the new, renamed files and the file list
        widget is refreshed before returning.
        """
        try:
            sheets_by_stem = {}
            for f in self._sch_files:
                sh, errors = parse_sch(f)
                for e in errors:
                    self._log_write(e, 'err')
                sheets_by_stem[Path(sh.name).stem.upper()] = sh
        except Exception:
            self._log_write(traceback.format_exc(), 'err')
            return False

        offending = detect_offending_refdes(sheets_by_stem)
        if not offending:
            return True

        root = self._sch_files[0]
        default_ddf = None
        for ext in ('.DDF', '.ddf'):
            candidate = root.with_suffix(ext)
            if candidate.exists():
                default_ddf = candidate
                break

        dlg = _RefdesFixDialog(self, offending, default_ddf)
        self.wait_window(dlg)

        out_dir = Path(self._out_var.get())
        log_path = out_dir / f'{root.stem}_REANNOT_log.txt'

        if dlg.choice == 'cancel':
            return False

        if dlg.choice == 'continue':
            result = ReannotateResult(offending=offending)
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                write_log(result, log_path, root_sch_name=root.name)
            except Exception:
                self._log_write(traceback.format_exc(), 'err')
            self._log_write(
                f'WARNING: {len(offending)} reference designator(s) do not end in a '
                f'digit; converting unchanged. See {log_path.name}', 'warn')
            return True

        # dlg.choice == 'fix'
        ddf_path = dlg.ddf_path_var.get().strip() or None
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            result = reannotate_hierarchy(root, ddf_path=ddf_path,
                                          out_dir=str(out_dir), suffix='_REANNOT')
            write_log(result, log_path, root_sch_name=root.name)
        except Exception:
            self._log_write(traceback.format_exc(), 'err')
            messagebox.showerror('Reannotation failed',
                                 'Could not write the renamed copies. See the log.')
            return False

        self._log_write(f'Reference designators fixed -- {len(result.changes)} renamed:',
                        'ok')
        for ch in result.changes:
            self._log_write(f'  {ch.old}  ->  {ch.new}', 'ok')
        for p in result.sch_files_written:
            self._log_write(f'Written: {p}', 'ok')
        if result.ddf_file_written:
            self._log_write(f'Written: {result.ddf_file_written}', 'ok')
            self._log_write('A fresh KIUB conversion of the new DDF file is required.', 'warn')
        elif ddf_path:
            self._log_write(f'WARNING: could not use DDF path: {ddf_path}', 'warn')

        # Continue the conversion using the new, renamed files. The file
        # list is already correct (reannotate_hierarchy wrote the full
        # renamed hierarchy) -- only the displayed path needs updating, and
        # without re-running discovery/logging a second time.
        self._sch_files = list(result.sch_files_written)
        self._suppress_infile_trace = True
        try:
            self._infile_var.set(str(self._sch_files[0]))
        finally:
            self._suppress_infile_trace = False
        return True

    def _run_dump(self):
        if not self._validate():
            return
        self._log_clear()
        self._last_dump_suffix = '_statistics'
        self._run_in_thread(self._do_dump)

    def _run_dump_symbols(self):
        if not self._validate():
            return
        self._log_clear()
        self._last_dump_suffix = '_symbols'
        self._run_in_thread(self._do_dump_symbols)

    def _run_save_log(self):
        """Save the current log contents to a text file."""
        if not self._log.get('1.0', 'end').strip():
            messagebox.showinfo('Nothing to save', 'The log is empty.', parent=self)
            return
        stem = self._sch_files[0].stem if self._sch_files else 'kiuc'
        suffix = self._last_dump_suffix  # set by each dump action
        self._save_log_to_file(f'{stem}{suffix}.txt')

    def _do_convert(self):
        try:
            self._set_status('Parsing…')
            set_use_kicad_colors(self._kicad_colors_var.get())

            # Apply V5 header fix if requested
            if self._v5_var.get():
                for f in self._sch_files:
                    ok, detail = force_v5_header(f)
                    if ok:
                        self._log_write_buffered(
                            f'V5 header fix: {f.name}  [{detail}] → 5 00 00000000',
                            'info')
                    else:
                        self._log_write_buffered(
                            f'WARNING: V5 header fix failed for {f.name}: {detail}',
                            'warn')

            sheets = []
            for f in self._sch_files:
                self._log_write_buffered(f'Parsing: {f}', 'info')
                sheet, errors = parse_sch(f)
                for e in errors:
                    self._log_write_buffered(e, 'err')
                sheets.append(sheet)

            for w in check_missing_sheets(sheets):
                self._log_write_buffered(f'WARNING: {w}', 'warn')
            for w in check_pkg_type_consistency(sheets):
                self._log_write_buffered(f'WARNING: {w}', 'warn')

            sch  = Schematic(sheets=sheets)
            base = self._name_var.get() or self._sch_files[0].stem
            out  = Path(self._out_var.get())
            out.mkdir(parents=True, exist_ok=True)

            self._set_status('Writing KiCad files…')
            if self._block_var.get():
                warns = write_block_library(sch, out, lib_name=base)
            else:
                warns = write_schematic(sch, out, base_name=base)
            for w in warns:
                self._log_write_buffered(f'WARNING: {w}', 'warn')

            # Report output files and capture the root sheet path
            if self._block_var.get():
                lib_dir = out / f'{base}.kicad_blocks'
                self._log_write_buffered(f'Written: {lib_dir}', 'ok')
                # Point to first .kicad_sch inside the block library for
                # Open in KiCad (falls back to .kicad_sch if no .pro)
                _first_block = next(lib_dir.glob('*/*.kicad_sch'), None)
                self._last_output_sch = str(_first_block) if _first_block else ''
            elif len(sheets) == 1:
                out_path = out / (base + '.kicad_sch')
                self._log_write_buffered(f'Written: {out_path}', 'ok')
                self._last_output_sch = str(out_path)
            else:
                for idx, sh in enumerate(sheets):
                    stem = Path(sh.name).stem if sh.name else base
                    out_path = out / (stem + '.kicad_sch')
                    self._log_write_buffered(f'Written: {out_path}', 'ok')
                    if idx == 0:
                        self._last_output_sch = str(out_path)

            self._log_write_buffered('Conversion complete.', 'ok')
            self._set_status('Done')

            # Enable Open in KiCad button
            self.after(0, lambda: self._open_btn.config(state='normal'))

        except Exception:
            self._log_write_buffered(traceback.format_exc(), 'err')
            self._set_status('Error — see log')

    def _do_dump(self):
        try:
            self._set_status('Parsing…')
            sheets = []
            for f in self._sch_files:
                sheet, errors = parse_sch(f)
                for e in errors:
                    self._log_write_buffered(e, 'err')
                sheets.append(sheet)
            sch = Schematic(sheets=sheets)

            for w in check_missing_sheets(sheets):
                self._log_write_buffered(f'WARNING: {w}', 'warn')

            dump_schematic(sch, self._log_write_buffered)
            self._set_status('Dump complete')
        except Exception:
            self._log_write_buffered(traceback.format_exc(), 'err')
            self._set_status('Error — see log')

    def _do_dump_symbols(self):
        try:
            self._set_status('Parsing…')
            sheets = []
            for f in self._sch_files:
                sheet, errors = parse_sch(f)
                for e in errors:
                    self._log_write_buffered(e, 'err')
                sheets.append(sheet)
            sch = Schematic(sheets=sheets)

            dump_symbols(sch, self._log_write_buffered)
            for w in check_pkg_type_consistency(sheets):
                self._log_write_buffered(f'WARNING: {w}', 'warn')
            self._set_status('Symbol dump complete')
        except Exception:
            self._log_write_buffered(traceback.format_exc(), 'err')
            self._set_status('Error — see log')

    # ── KiCad launcher ─────────────────────────────────────────────────────────

    def _ask_kicad_exe(self):
        """Prompt the user to locate KiCad on first run."""
        messagebox.showinfo(
            'KiCad location required',
            'Please locate the KiCad executable so the "Open in KiCad" button works.\n\n'
            'The path is saved in kiuc.ini and only asked once.',
            parent=self)
        self._change_kicad_exe()

    def _change_kicad_exe(self):
        path = _browse_kicad_exe(parent=self)
        if path:
            self._kicad_exe = path
            _save_kicad_exe(path)
            self._set_status(f'KiCad path saved: {path}')

    def _open_tuning_dialog(self):
        dlg = _TuningDialog(self)
        self.wait_window(dlg)
        if dlg.saved:
            self._set_status('Fine-tuning values saved.')

    def _open_in_kicad(self):
        if not self._last_output_sch:
            return
        # Re-validate path in case it changed since startup
        if not self._kicad_exe or not Path(self._kicad_exe).is_file():
            self._ask_kicad_exe()
            if not self._kicad_exe:
                return
        try:
            # Prefer opening the .kicad_pro (launches the full project);
            # fall back to .kicad_sch if no pro file exists (e.g. block mode).
            _target = Path(self._last_output_sch)
            _pro = _target.with_suffix('.kicad_pro')
            subprocess.Popen([self._kicad_exe, str(_pro if _pro.exists() else _target)])
        except OSError as exc:
            messagebox.showerror(
                'Could not launch KiCad',
                f'Failed to start KiCad:\n{exc}\n\n'
                'Use ⚙ KiCad Path… to set the correct executable.',
                parent=self)


if __name__ == '__main__':
    _initial = sys.argv[1] if len(sys.argv) > 1 else None
    App(initial_path=_initial).mainloop()
