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
kiuc_refdes.py — standalone reference-designator reannotation tool.

Some Ulticap component reference designators (e.g. mating-connector pairs
like PL95MV/SK95MV) are literal, user-meaningful strings that do not end in
a digit. KiCad's schematic editor data model requires every reference
designator to end in a number to be considered "annotated": ERC flags a
non-digit-ending refdes as unannotated, and KiCad's netlist generation
refuses to proceed until it's fixed. There is no value KiCad will accept
here that doesn't change the refdes text, so any fix breaks the existing
match between the Ulticap SCH and its accompanying Ultiboard DDF/PCB unless
BOTH are updated identically and consistently. There is no fully
automatic, risk-free solution to this -- this tool exists to make the
*manual* part of the job (deciding whether to do it, and verifying the
result) as small and well-documented as possible, by doing the tedious,
error-prone part (finding and consistently renaming every occurrence
across both file formats and an entire sheet hierarchy) for the user.

This module NEVER edits files in place. It always produces new, renamed
copies (default suffix "_REANNOT"), leaving every original file -- SCH,
sub-sheets, and DDF -- completely untouched.

New refdes values are computed as old_refdes + the lowest unused integer
suffix starting at 1 (e.g. "PL95MV" -> "PL95MV1"), checked for collisions
against every refdes anywhere in the sheet hierarchy (KiCad refdes
uniqueness is project-wide, not per-sheet). This intentionally matches
what KiCad's own Annotate tool does to a no-digit refdes, so if the user
later runs Annotate in KiCad, an already-suffixed refdes like this is a
no-op there -- nothing drifts further out of sync after this fix is
applied.

Pure logic, no UI: every "should I do this" decision is a parameter, not
an interactive prompt, so this module can be wrapped by:
  - the main Ulticap->KiCad converter GUI (kiuc_gui.py), in-process
  - a separate, standalone GUI for this tool alone
  - the command line, directly (see the __main__ block below)
  - a subprocess call from another project entirely (e.g. KIUB)
"""
from __future__ import annotations

import re
import argparse
import datetime
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from kiuc_ascii import parse_sch
from kiuc_model import Sheet


# ── refdes-validity check ────────────────────────────────────────────────────

# A KiCad-valid refdes is PREFIX + DIGITS, optionally followed by a SINGLE
# trailing letter indicating the unit -- but ONLY when the symbol genuinely
# HAS multiple units. KiCad's own ERC distinguishes two completely separate
# checks: "unannotated" (ERCE_UNANNOTATED) and "invalid part count for a
# multi-unit symbol" (ERCE_EXTRA_UNITS) -- they sound like they could cover
# this independently, but a direct KiCad test (refdes "R1A" placed on a
# genuinely single-unit symbol) confirmed KiCad's annotation/netlist gate
# does NOT treat that shape as already-annotated: it still produces the
# same mandatory-annotate block as a bare no-digit refdes. So the trailing
# single letter is only accepted as a real unit suffix -- and the refdes
# only counted as already annotated -- when the symbol it's placed on
# actually has more than one unit (Ulticap's PARTS attribute > 1, the same
# field already used elsewhere in this project's kiuc_writer.py for
# multi-gate lib_symbol detection). A refdes with that exact shape on a
# PARTS<=1 symbol is treated exactly like a bare no-digit refdes (e.g.
# "PL95MV"): offending, and fixed the same way (append the next free
# integer suffix).
#
# e.g. "U2A"/"U2B" (gates of a real dual-gate IC, PARTS=2) are valid,
# already-annotated refdes. "R1A" on a PARTS=1 (or absent) resistor symbol
# is NOT valid, despite matching the same string shape, and must be
# treated as offending.
_REFDES_SHAPE_RE = re.compile(r'^(.+\d)([A-Za-z])\Z')   # captures (base, unit_letter)


def is_genuinely_annotated(refdes: str, parts: int) -> bool:
    """True if KiCad will accept this refdes as already-annotated, given
    the actual unit/part count of the symbol it's placed on.

    parts is the symbol's PARTS attribute (Ulticap; 1 or absent = single
    unit). A bare PREFIX+DIGITS refdes (no trailing letter) is always
    valid regardless of parts. A refdes with one trailing letter after the
    digits is only valid when parts > 1; otherwise it's offending, exactly
    like a refdes with no trailing digit at all.
    """
    if not refdes:
        return False
    m = _REFDES_SHAPE_RE.match(refdes)
    if m:
        return parts > 1
    return refdes[-1].isdigit()


# ── result data structures ──────────────────────────────────────────────────
# Plain data, no behaviour -- every UI wrapper renders these however it likes.

@dataclass
class OffendingRef:
    refdes: str
    sheet_name: str          # the .SCH stem this component lives on
    symbol_name: str


@dataclass
class RefdesChange:
    old: str
    new: str


@dataclass
class ReannotateResult:
    """Everything a caller (any UI) needs to report what happened."""
    offending: List[OffendingRef] = field(default_factory=list)
    changes: List[RefdesChange] = field(default_factory=list)
    sch_files_written: List[Path] = field(default_factory=list)
    ddf_file_written: Optional[Path] = None
    ddf_refdes_not_found: List[str] = field(default_factory=list)
    applied: bool = False            # True only once new files were actually written
    errors: List[str] = field(default_factory=list)


# ── hierarchy loading ────────────────────────────────────────────────────────

def _load_hierarchy(root_sch_path) -> Dict[str, Sheet]:
    """Parse root_sch_path and every sub-sheet it (transitively) references
    via FILE=, returning {stem_upper: Sheet}.

    A sheet referenced but not found on disk is silently skipped -- this
    tool only needs to touch sheets it can actually read and rewrite; a
    missing sub-sheet is a separate, pre-existing problem this tool isn't
    responsible for surfacing.
    """
    root_sch_path = Path(root_sch_path)
    folder = root_sch_path.parent
    sheets: Dict[str, Sheet] = {}

    def _visit(path: Path):
        stem = path.stem.upper()
        if stem in sheets:
            return
        if not path.exists():
            return
        sh, _errors = parse_sch(str(path))
        sheets[stem] = sh
        for comp in sh.components:
            if comp.file_ref:
                _visit(folder / comp.file_ref)

    _visit(root_sch_path)
    return sheets


# ── detection ─────────────────────────────────────────────────────────────────

def _symbol_parts(sh: Sheet, symbol_name: str) -> int:
    """The symbol's PARTS attribute (1 if absent/unparseable, matching the
    same default used throughout kiuc_writer.py for single-gate symbols).
    """
    sym = sh.symbols.get(symbol_name)
    if sym is None:
        return 1
    try:
        return int(sym.attributes.get('PARTS', '1'))
    except (TypeError, ValueError):
        return 1


def detect_offending_refdes(sheets: Dict[str, Sheet]) -> List[OffendingRef]:
    """Scan every sheet in the hierarchy for components whose refdes KiCad
    will not accept as already-annotated (see is_genuinely_annotated).
    One entry per offending COMPONENT INSTANCE (not per unique refdes
    text) -- collision-checking and the log both need to know exactly
    which sheet/component each one belongs to, and the same literal refdes
    text could in principle appear validly on more than one placement in
    different design contexts before it's renamed.
    """
    found: List[OffendingRef] = []
    for stem, sh in sheets.items():
        for comp in sh.components:
            rd = comp.refdes
            if not rd:
                continue
            if comp.file_ref:
                continue   # hierarchical sheet-symbol placement, not a real refdes
            parts = _symbol_parts(sh, comp.symbol_name)
            if not is_genuinely_annotated(rd, parts):
                found.append(OffendingRef(refdes=rd, sheet_name=stem,
                                          symbol_name=comp.symbol_name))
    return found


def _all_refdes(sheets: Dict[str, Sheet]) -> Set[str]:
    """Every refdes in use anywhere in the hierarchy -- project-wide
    uniqueness, matching KiCad's own refdes scope (a refdes must be unique
    across the whole project, not just within one sheet).
    """
    out: Set[str] = set()
    for sh in sheets.values():
        for comp in sh.components:
            if comp.refdes and not comp.file_ref:
                out.add(comp.refdes)
    return out


# ── new-name computation ─────────────────────────────────────────────────────

def compute_new_refdes(old_refdes: str, all_refdes: Set[str]) -> str:
    """old_refdes + the lowest unused integer suffix, starting at 1.
    Collision-checked against every refdes anywhere in the hierarchy.
    """
    n = 1
    while f'{old_refdes}{n}' in all_refdes:
        n += 1
    return f'{old_refdes}{n}'


# ── SCH text-level rewriting ─────────────────────────────────────────────────
# All rewriting operates on the RAW source text (read/written as latin-1, a
# lossless 1:1 byte<->codepoint mapping, so every byte outside the matched
# regions round-trips exactly) -- never by re-serializing through the
# parser/writer. This tool's only job is a surgical, minimal rename; every
# other byte of the original file (formatting, comments, unrelated data)
# must come through completely unchanged.

def _split_at_nrm_marker(text: str) -> Tuple[str, str]:
    """Split raw SCH text into (schematic_part, nrm_part) at the '**' line.

    Everything from a line that is exactly '**' onward is appended by a
    separate tool (NRM -- Netlist Representation Model), used by Ulticap to
    interface with other programs, and is not always present. The existing
    parser (_AsciiParser.parse) already stops reading at this exact marker,
    so refdes/FILE= occurrences anywhere in the NRM section are outside
    what this project's schematic conversion ever looks at -- and, since
    NRM's own data format isn't characterized here, outside what this tool
    should attempt to rewrite. schematic_part includes the '**' marker line
    itself; nrm_part is everything after it, both returned with their
    original line endings intact so concatenating them back together
    reproduces the input exactly when no substitution applies.
    """
    idx = 0
    while True:
        nl = text.find('\n', idx)
        if nl == -1:
            return text, ''   # no '**' found -- nothing to split off
        line = text[idx:nl]
        if line.rstrip('\r') == '**':
            return text[:nl + 1], text[nl + 1:]
        idx = nl + 1


def _rewrite_refdes_in_sch_text(text: str, old: str, new: str) -> str:
    """Replace REFDES=<old> with REFDES=<new> on a *C component instance's
    attribute line. Anchored to end-of-line so e.g. old='PL95MV' can never
    accidentally match an unrelated, already-distinct 'PL95MV1'.
    """
    pattern = re.compile(r'REFDES=' + re.escape(old) + r'(\r\n|\r|\n|$)')
    return pattern.sub(lambda m: f'REFDES={new}' + m.group(1), text)


def _rewrite_file_ref_in_sch_text(text: str, old_child_stem: str,
                                  new_child_stem: str) -> str:
    """Replace a FILE=<old_child_stem>[.SCH] hierarchical sub-sheet
    cross-reference with the new (renamed) child sheet's stem, so a parent
    sheet's copy correctly loads the corresponding renamed child copy
    instead of the original.

    Anchored to end-of-line, case-insensitive on the value (this
    codebase's own parser resolves FILE= case-insensitively -- see
    kiuc_ascii.py), and matches with or without the .SCH extension
    since Ulticap source files are observed to store it either way.
    """
    pattern = re.compile(
        r'FILE=' + re.escape(old_child_stem) + r'(\.SCH)?(\r\n|\r|\n|$)',
        re.IGNORECASE)
    return pattern.sub(
        lambda m: f'FILE={new_child_stem}' + (m.group(1) or '') + m.group(2),
        text)


# ── DDF text-level rewriting ─────────────────────────────────────────────────

def _rewrite_refdes_in_ddf_text(text: str, old: str, new: str) -> Tuple[str, bool]:
    """Replace the '*C <REFDES> ...' component-definition line in a DDF.
    Confirmed stable across DDF V2.x-V5.x (same '*C <refdes> /<value>
    <footprint>!<n>' line shape verified against a real V4.x file).

    Deliberately does NOT touch '*X ... <REFDES>' silkscreen text lines --
    confirmed these are plain cosmetic text in the DDF, not data KIUB
    binds the component to, so leaving them stale is harmless and not
    touching them avoids any risk to a record type whose full structure
    isn't otherwise relevant here.

    Returns (new_text, found) -- found is False if the refdes's *C line
    wasn't located, so callers can flag a mismatch rather than silently
    producing a DDF that doesn't actually contain the expected change.
    """
    pattern = re.compile(r'(^\*C )' + re.escape(old) + r'(\s)', re.MULTILINE)
    new_text, n = pattern.subn(lambda m: m.group(1) + new + m.group(2), text)
    return new_text, (n > 0)


def _all_ddf_refdes(ddf_text: str) -> Set[str]:
    """Every refdes already in use anywhere in a DDF's '*C <refdes> ...'
    component-definition lines.

    A DDF can contain board-only items with no schematic representation at
    all -- mechanical parts such as mounting spacers are a real example,
    confirmed against an actual board file alongside this tool's
    development. The SCH-derived collision set (_all_refdes) has no
    visibility into these, so without this check a computed new refdes
    could coincide with an existing DDF-only refdes that the SCH model
    never knew about, producing a genuine duplicate reference in the
    output DDF.
    """
    return set(re.findall(r'^\*C (\S+) ', ddf_text, re.MULTILINE))


# ── main entry point ─────────────────────────────────────────────────────────

def reannotate_hierarchy(root_sch_path, ddf_path: Optional[str] = None,
                         out_dir: Optional[str] = None,
                         suffix: str = '_REANNOT') -> ReannotateResult:
    """Detect offending refdes across the whole hierarchy rooted at
    root_sch_path, and -- if any are found -- write renamed copies of
    EVERY sheet in the hierarchy (not just the ones containing an
    offending refdes: a parent's FILE= reference must point at the new
    name of every child, so every sheet has to move together as one
    consistent set) plus the DDF, if provided, with collision-safe new
    refdes values applied throughout.

    Always non-destructive: every file written is a NEW file (suffix
    appended to the original stem); nothing at root_sch_path's original
    path, any sub-sheet's original path, or ddf_path is ever modified.

    New refdes values are collision-checked against every refdes in the
    SCH hierarchy AND, when a ddf_path is given, against every refdes
    already present in the DDF -- including board-only items (e.g.
    mechanical mounting hardware) that have no schematic representation at
    all and so would otherwise be invisible to the SCH-only check.

    If no offending refdes are found anywhere in the hierarchy, returns
    immediately with result.applied == False and empty change/file lists
    -- callers should treat that as "nothing to do", not an error.
    """
    result = ReannotateResult()
    root_sch_path = Path(root_sch_path)
    folder = root_sch_path.parent
    sheets = _load_hierarchy(root_sch_path)

    offending = detect_offending_refdes(sheets)
    result.offending = offending
    if not offending:
        return result

    # Read the DDF early (before computing new names) so its own refdes
    # namespace -- which can include board-only items with no schematic
    # representation at all, e.g. mechanical mounting spacers -- is part
    # of the collision check from the start, not just the SCH model's.
    ddf_path_obj: Optional[Path] = None
    ddf_text: Optional[str] = None
    if ddf_path:
        ddf_path_obj = Path(ddf_path)
        if ddf_path_obj.exists():
            ddf_text = ddf_path_obj.read_text(encoding='cp437')
        else:
            result.errors.append(f'DDF path does not exist: {ddf_path_obj}')

    all_refdes = _all_refdes(sheets)
    if ddf_text is not None:
        all_refdes |= _all_ddf_refdes(ddf_text)

    changes: List[RefdesChange] = []
    seen_old: Set[str] = set()
    for o in offending:
        if o.refdes in seen_old:
            continue   # same literal refdes already scheduled for renaming
        seen_old.add(o.refdes)
        new = compute_new_refdes(o.refdes, all_refdes)
        all_refdes.add(new)   # reserve it so a later offending refdes can't collide with it
        changes.append(RefdesChange(old=o.refdes, new=new))
    result.changes = changes

    out_dir = Path(out_dir) if out_dir else folder
    out_dir.mkdir(parents=True, exist_ok=True)

    # Every sheet in the hierarchy moves to the same suffix, so FILE=
    # cross-references between sheets stay internally consistent in the
    # copied set -- a parent referencing an un-renamed child would break
    # the hierarchy link in the copy.
    stem_map = {stem: f'{stem}{suffix}' for stem in sheets.keys()}

    for stem, sh in sheets.items():
        src_path = folder / sh.name   # Sheet.name is always a bare filename
        if not src_path.exists():
            result.errors.append(f'Could not locate source file for sheet {stem}; skipped')
            continue

        text = src_path.read_text(encoding='cp437')
        sch_part, nrm_part = _split_at_nrm_marker(text)

        for ch in changes:
            sch_part = _rewrite_refdes_in_sch_text(sch_part, ch.old, ch.new)

        for old_child_stem, new_child_stem in stem_map.items():
            if old_child_stem == stem:
                continue
            sch_part = _rewrite_file_ref_in_sch_text(sch_part, old_child_stem, new_child_stem)

        text = sch_part + nrm_part
        out_path = out_dir / f'{stem_map[stem]}{src_path.suffix}'
        out_path.write_text(text, encoding='cp437')
        result.sch_files_written.append(out_path)

    if ddf_text is not None:
        for ch in changes:
            ddf_text, found = _rewrite_refdes_in_ddf_text(ddf_text, ch.old, ch.new)
            if not found:
                result.ddf_refdes_not_found.append(ch.old)
        ddf_out = out_dir / f'{ddf_path_obj.stem}{suffix}{ddf_path_obj.suffix}'
        ddf_out.write_text(ddf_text, encoding='cp437')
        result.ddf_file_written = ddf_out

    result.applied = True
    return result


# ── log file ─────────────────────────────────────────────────────────────────

def write_log(result: ReannotateResult, log_path,
             root_sch_name: str = '') -> None:
    """Always called regardless of whether the user accepted, declined, or
    had no DDF available at all -- this is the durable record of what was
    found and what (if anything) was done about it. Useful both as
    documentation of an applied change and, when the user declines, as the
    only record that the issue was ever flagged.
    """
    lines: List[str] = []
    lines.append('Reference-designator reannotation log')
    lines.append(f'Generated: {datetime.datetime.now().isoformat(timespec="seconds")}')
    if root_sch_name:
        lines.append(f'Source design: {root_sch_name}')
    lines.append('')

    if not result.offending:
        lines.append('No non-digit-ending reference designators found.')
    else:
        lines.append(f'{len(result.offending)} offending component instance(s) found:')
        for o in result.offending:
            lines.append(f'  {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})')
        lines.append('')

        if result.applied:
            lines.append('Reannotation APPLIED. New copies were written; originals untouched.')
            lines.append('')
            lines.append('Refdes changes:')
            for ch in result.changes:
                lines.append(f'  {ch.old}  ->  {ch.new}')
            lines.append('')
            lines.append('Files written:')
            for p in result.sch_files_written:
                lines.append(f'  {p}')
            if result.ddf_file_written:
                lines.append(f'  {result.ddf_file_written}')
                lines.append('')
                lines.append('A fresh KIUB conversion of the new DDF file is required.')
            if result.ddf_refdes_not_found:
                lines.append('')
                lines.append('WARNING: the following refdes were NOT found in the DDF '
                             '(*C definition line) and were left unchanged there:')
                for rd in result.ddf_refdes_not_found:
                    lines.append(f'  {rd}')
        else:
            lines.append('Reannotation NOT applied (declined by user, or no DDF '
                         'available and the SCH-only fix was not selected).')
            lines.append('The original schematic will be converted unchanged. KiCad')
            lines.append('will flag these components as unannotated (ERC warning),')
            lines.append('and netlist generation will be blocked until annotated.')

    if result.errors:
        lines.append('')
        lines.append('Errors:')
        for e in result.errors:
            lines.append(f'  {e}')

    Path(log_path).write_text('\n'.join(lines) + '\n', encoding='utf-8')


# ── command-line interface ──────────────────────────────────────────────────

def _cli():
    p = argparse.ArgumentParser(
        description='Detect and fix Ulticap reference designators that do not '
                    'end in a digit (required by KiCad schematic annotation), '
                    'producing renamed copies of the SCH hierarchy and its '
                    'matching DDF. Never modifies the originals.')
    p.add_argument('sch', help='Path to the root .SCH file')
    p.add_argument('--ddf', default=None, help='Path to the matching .DDF file (optional)')
    p.add_argument('-o', '--out-dir', default=None,
                   help='Output directory (default: same folder as the SCH file)')
    p.add_argument('--apply', action='store_true',
                   help='Actually write the renamed files. Without this flag, '
                        'only detects and reports -- no files are written '
                        '(dry run).')
    p.add_argument('--suffix', default='_REANNOT',
                   help='Suffix appended to output filenames (default: _REANNOT)')
    args = p.parse_args()

    sch_path = Path(args.sch)
    out_dir = Path(args.out_dir) if args.out_dir else sch_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    if not args.apply:
        sheets = _load_hierarchy(sch_path)
        offending = detect_offending_refdes(sheets)
        result = ReannotateResult(offending=offending)
    else:
        result = reannotate_hierarchy(sch_path, ddf_path=args.ddf,
                                      out_dir=args.out_dir, suffix=args.suffix)

    if not result.offending:
        print('No non-digit-ending reference designators found.')
    else:
        print(f'{len(result.offending)} offending component instance(s) found:')
        for o in result.offending:
            print(f'  {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})')

        if not args.apply:
            print()
            print('Dry run only -- no files written. Re-run with --apply to write '
                 'renamed copies.')
        else:
            print()
            print('Refdes changes:')
            for ch in result.changes:
                print(f'  {ch.old}  ->  {ch.new}')
            print()
            print('Files written:')
            for f in result.sch_files_written:
                print(f'  {f}')
            if result.ddf_file_written:
                print(f'  {result.ddf_file_written}')
                print()
                print('A fresh KIUB conversion of the new DDF file is required.')
            if result.ddf_refdes_not_found:
                print()
                print('WARNING: not found in DDF (*C line): '
                     + ', '.join(result.ddf_refdes_not_found))
            if result.errors:
                print()
                print('Errors:')
                for e in result.errors:
                    print(f'  {e}')

    log_path = out_dir / f'{sch_path.stem}{args.suffix}_log.txt'
    write_log(result, log_path, root_sch_name=sch_path.name)
    print()
    print(f'Log written: {log_path}')


if __name__ == '__main__':
    _cli()
