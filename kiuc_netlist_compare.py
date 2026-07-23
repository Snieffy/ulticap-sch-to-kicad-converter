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
Compare a KiCad .net file against an Ulticap .net file by net membership.

The net names are ignored.  A net is considered equal when the complete set of
component-pin entries is equal, for example {R1-1, IC2-5, C3-2}.

GUI workflow:
  1. Select the KiCad .net file.
  2. Select the Ulticap .net file.
  3. Choose or accept the automatically generated report filename.
  4. Click Compare.

The report filename is generated from the two input filenames by default:
  <kicad-stem>__VS__<ulticap-stem>__net_compare.txt
"""

from __future__ import annotations

import html
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Tuple
import tkinter as tk
from tkinter import filedialog, messagebox

Pin = Tuple[str, str]          # (reference, pin)
PinGroup = FrozenSet[Pin]


@dataclass(frozen=True)
class NetGroup:
    name: str
    pins: PinGroup


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def pin_sort_key(pin: Pin) -> Tuple[str, int, str]:
    """Natural-ish sort: R2 before R10, then pin text."""
    ref, p = pin
    m = re.match(r"^([A-Za-z_]+)(\d+)$", ref)
    if m:
        return (m.group(1).upper(), int(m.group(2)), p)
    return (ref.upper(), 10**9, p)


def format_pin(pin: Pin) -> str:
    ref, p = pin
    return f"{ref}-{p}" if p else f"{ref}-(empty pin)"


def format_pins(pins: Iterable[Pin]) -> str:
    return ", ".join(format_pin(pin) for pin in sorted(pins, key=pin_sort_key))


def safe_stem(path: Path) -> str:
    stem = path.stem.strip() or "netlist"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("_") or "netlist"


def default_report_path(kicad_path: str, ulticap_path: str) -> str:
    if not kicad_path or not ulticap_path:
        return ""
    kp = Path(kicad_path)
    up = Path(ulticap_path)
    return str(kp.with_name(f"{safe_stem(kp)}__VS__{safe_stem(up)}__net_compare.txt"))


# ---------------------------------------------------------------------------
# KiCad S-expression .net parser
# ---------------------------------------------------------------------------

def _read_text(path: str) -> str:
    data = Path(path).read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp437", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
    return data.decode("latin-1", errors="replace")


def _find_balanced_forms(text: str, head: str) -> List[str]:
    """Return complete S-expression forms starting with '(head'."""
    forms: List[str] = []
    i = 0
    needle = f"({head}"
    n = len(text)

    while True:
        start = text.find(needle, i)
        if start < 0:
            break

        # Avoid matching words that merely begin with the head.
        after = start + len(needle)
        if after < n and text[after] not in " \t\r\n()":
            i = after
            continue

        depth = 0
        in_string = False
        escaped = False
        for j in range(start, n):
            ch = text[j]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        forms.append(text[start:j + 1])
                        i = j + 1
                        break
        else:
            raise ValueError(f"Unbalanced S-expression while reading {head!r}")
    return forms


def _sexpr_string_value(form: str, key: str) -> Optional[str]:
    m = re.search(r"\(" + re.escape(key) + r"\s+\"((?:\\.|[^\"])*)\"\)", form)
    if not m:
        return None
    return bytes(m.group(1), "utf-8").decode("unicode_escape")


def parse_kicad_netlist(path: str) -> List[NetGroup]:
    text = _read_text(path)
    groups: List[NetGroup] = []

    for net_form in _find_balanced_forms(text, "net"):
        name = _sexpr_string_value(net_form, "name") or "(unnamed)"
        pins: List[Pin] = []

        for node_form in _find_balanced_forms(net_form, "node"):
            ref = _sexpr_string_value(node_form, "ref") or ""
            pin = _sexpr_string_value(node_form, "pin") or ""
            if ref:
                pins.append((ref.strip(), pin.strip()))

        groups.append(NetGroup(name=name, pins=frozenset(pins)))

    if not groups:
        raise ValueError("No KiCad '(net ...)' groups found. Is this a KiCad Eeschema .net file?")
    return groups


# ---------------------------------------------------------------------------
# Ulticap .net parser
# ---------------------------------------------------------------------------

def parse_ulticap_netlist(path: str) -> List[NetGroup]:
    text = _read_text(path)
    groups: List[NetGroup] = []
    current_name: Optional[str] = None
    current_pins: List[Pin] = []

    def flush() -> None:
        nonlocal current_name, current_pins
        if current_name is not None:
            groups.append(NetGroup(name=current_name, pins=frozenset(current_pins)))
        current_name = None
        current_pins = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("+") or line.startswith("="):
            continue

        if line.startswith("*"):
            flush()
            current_name = line[1:].strip()
            continue

        if current_name is None:
            continue

        # Ulticap usually emits comma-separated COMP-PIN entries.
        for item in line.split(","):
            item = item.strip()
            if not item:
                continue
            m = re.match(r"^([^\s,\-]+)\s*-\s*(\S*)$", item)
            if m:
                current_pins.append((m.group(1).strip(), m.group(2).strip()))
            else:
                # Keep unusual tokens visible instead of silently dropping them.
                current_pins.append((item, ""))

    flush()

    if not groups:
        raise ValueError("No Ulticap '* netname' groups found. Is this an Ulticap .net file?")
    return groups


# ---------------------------------------------------------------------------
# Comparison and report generation
# ---------------------------------------------------------------------------

def index_by_group(groups: Sequence[NetGroup]) -> Dict[PinGroup, List[NetGroup]]:
    out: Dict[PinGroup, List[NetGroup]] = {}
    for g in groups:
        out.setdefault(g.pins, []).append(g)
    return out


def group_sort_key(g: NetGroup) -> Tuple[int, str, str]:
    first_pin = format_pin(sorted(g.pins, key=pin_sort_key)[0]) if g.pins else ""
    return (len(g.pins), first_pin, g.name)


def build_report(kicad_path: str, ulticap_path: str) -> str:
    kicad = parse_kicad_netlist(kicad_path)
    ulticap = parse_ulticap_netlist(ulticap_path)

    k_by_group = index_by_group(kicad)
    u_by_group = index_by_group(ulticap)

    matched: List[Tuple[NetGroup, NetGroup]] = []
    missing_ulticap: List[NetGroup] = []
    extra_kicad: List[NetGroup] = []

    for u in sorted(ulticap, key=group_sort_key):
        candidates = k_by_group.get(u.pins, [])
        if candidates:
            matched.append((candidates[0], u))
        else:
            missing_ulticap.append(u)

    for k in sorted(kicad, key=group_sort_key):
        if k.pins not in u_by_group:
            extra_kicad.append(k)

    k_name = Path(kicad_path).name
    u_name = Path(ulticap_path).name

    lines: List[str] = []
    lines.append(f"{safe_stem(Path(kicad_path))} / {safe_stem(Path(ulticap_path))} netlist group comparison")
    lines.append("=" * 72)
    lines.append("")
    lines.append("Comparison rule: net names ignored; each net is compared as a set of (component reference, pin).")
    lines.append("")
    lines.append(f"KiCad file: {k_name}")
    lines.append(f"Ulticap file: {u_name}")
    lines.append("")
    lines.append(f"KiCad {k_name} nets: {len(kicad)}")
    lines.append(f"Ulticap {u_name} nets: {len(ulticap)}")
    lines.append(f"Matched groups: {len(matched)} / {len(ulticap)} Ulticap groups")
    lines.append(f"Ulticap groups missing in KiCad: {len(missing_ulticap)}")
    lines.append(f"Extra KiCad groups not in Ulticap: {len(extra_kicad)}")
    lines.append("")

    if missing_ulticap:
        lines.append("Ulticap groups missing in KiCad:")
        for u in sorted(missing_ulticap, key=group_sort_key):
            lines.append(f"- Ulticap {u.name} ({len(u.pins)} pins): {format_pins(u.pins)}")
        lines.append("")

    if extra_kicad:
        lines.append("Extra KiCad-only groups:")
        for k in sorted(extra_kicad, key=group_sort_key):
            pins_text = format_pins(k.pins) if k.pins else "(empty net)"
            lines.append(f"- KiCad {k.name} ({len(k.pins)} pins): {pins_text}")
        lines.append("")

    lines.append("Matched groups:")
    for k, u in sorted(matched, key=lambda ku: group_sort_key(ku[1])):
        same_name = k.name == u.name or k.name.lstrip("/") == u.name
        name_note = "same/similar net name" if same_name else "different net name"
        pins_text = format_pins(u.pins) if u.pins else "(empty net)"
        lines.append(f"- KiCad {k.name} == Ulticap {u.name} ({len(u.pins)} pins, {name_note})")
        lines.append(f"  KiCad pins:   {pins_text}")
        lines.append(f"  Ulticap pins: {pins_text}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Tkinter GUI
# ---------------------------------------------------------------------------

class NetCompareApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("KiCad / Ulticap Netlist Group Compare")
        self.geometry("760x260")
        self.minsize(720, 240)

        self.kicad_var = tk.StringVar()
        self.ulticap_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Select both files, then click Compare.")

        self._build_ui()

    def _build_ui(self) -> None:
        outer = tk.Frame(self, padx=12, pady=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        self._file_row(outer, 0, "KiCad .net:", self.kicad_var, self.browse_kicad)
        self._file_row(outer, 1, "Ulticap .net:", self.ulticap_var, self.browse_ulticap)
        self._file_row(outer, 2, "Report file:", self.output_var, self.browse_output)

        buttons = tk.Frame(outer)
        buttons.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(18, 8))
        tk.Button(buttons, text="Compare", width=16, command=self.compare).pack(side="left")
        tk.Button(buttons, text="Quit", width=10, command=self.destroy).pack(side="right")

        status = tk.Label(outer, textvariable=self.status_var, anchor="w", justify="left", wraplength=720)
        status.grid(row=4, column=0, columnspan=3, sticky="ew")

    def _file_row(self, parent: tk.Frame, row: int, label: str, var: tk.StringVar, command) -> None:
        tk.Label(parent, text=label, anchor="e", width=13).grid(row=row, column=0, sticky="e", pady=5)
        entry = tk.Entry(parent, textvariable=var)
        entry.grid(row=row, column=1, sticky="ew", padx=8, pady=5)
        tk.Button(parent, text="Browse...", command=command).grid(row=row, column=2, sticky="ew", pady=5)

    def _update_default_output(self) -> None:
        current = self.output_var.get().strip()
        default = default_report_path(self.kicad_var.get(), self.ulticap_var.get())
        if default and (not current or current.endswith("__net_compare.txt")):
            self.output_var.set(default)

    def browse_kicad(self) -> None:
        path = filedialog.askopenfilename(title="Select KiCad .net file", filetypes=[("Netlist files", "*.net"), ("All files", "*.*")])
        if path:
            self.kicad_var.set(path)
            self._update_default_output()

    def browse_ulticap(self) -> None:
        path = filedialog.askopenfilename(title="Select Ulticap .net file", filetypes=[("Netlist files", "*.net"), ("All files", "*.*")])
        if path:
            self.ulticap_var.set(path)
            self._update_default_output()

    def browse_output(self) -> None:
        initial = self.output_var.get().strip() or default_report_path(self.kicad_var.get(), self.ulticap_var.get())
        initialdir = str(Path(initial).parent) if initial else os.getcwd()
        initialfile = Path(initial).name if initial else "net_compare.txt"
        path = filedialog.asksaveasfilename(
            title="Save comparison report as",
            defaultextension=".txt",
            initialdir=initialdir,
            initialfile=initialfile,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def compare(self) -> None:
        kicad = self.kicad_var.get().strip()
        ulticap = self.ulticap_var.get().strip()
        output = self.output_var.get().strip() or default_report_path(kicad, ulticap)

        if not kicad or not ulticap:
            messagebox.showerror("Missing file", "Select both the KiCad and Ulticap .net files.")
            return
        if not output:
            messagebox.showerror("Missing report file", "Choose an output report filename.")
            return

        try:
            report = build_report(kicad, ulticap)
            Path(output).write_text(report, encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Compare failed", str(exc))
            self.status_var.set(f"Compare failed: {exc}")
            return

        self.output_var.set(output)
        self.status_var.set(f"Report written: {output}")
        messagebox.showinfo("Done", f"Report written:\n{output}")


def main() -> None:
    app = NetCompareApp()
    app.mainloop()


if __name__ == "__main__":
    main()
