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
kiuc.py — Convert Ulticap ASCII .SCH and .BLK files to KiCad 9 .kicad_sch

Usage:
  # Single sheet
  python kiuc.py design.SCH -o output/
  python kiuc.py design.BLK -o output/

  # Hierarchical design: sub-sheets are discovered and added automatically
  # from the main sheet -- no need to list them
  python kiuc.py parent.SCH -o output/

  # Dump parsed data without writing KiCad files
  python kiuc.py design.SCH --dump

  # Dump all symbol names and attributes (to identify title block etc.)
  python kiuc.py design.SCH --dump-symbols

Note: Binary (.UTSCH) file conversion is not yet available in this release.
"""
from __future__ import annotations
import argparse
import configparser
import sys
from pathlib import Path
from typing import List

from kiuc_model import Schematic, Sheet
from kiuc_ascii  import parse_sch, force_v5_header, collect_hierarchy_paths
from kiuc_writer   import write_schematic, write_block_library, \
                           check_missing_sheets, dump_schematic, dump_symbols, \
                           set_use_kicad_colors, TUNING_SPEC, set_tuning
from kiuc_refdes import (detect_offending_refdes, reannotate_hierarchy,
                               write_log, ReannotateResult)

_CONFIG_FILE    = Path(__file__).parent / 'kiuc.ini'
_TUNING_SECTION = 'tuning'


def _load_tuning_from_ini() -> dict:
    """Load any fine-tuning values saved previously (e.g. via the GUI's
    Fine-tuning dialog) from kiuc.ini, so CLI conversions stay in
    sync with whatever the GUI was last tuned to. Names not present in the
    file are left at kiuc_writer's built-in defaults."""
    cfg = configparser.ConfigParser()
    cfg.read(_CONFIG_FILE, encoding='utf-8')
    values = {}
    if cfg.has_section(_TUNING_SECTION):
        for name, *_rest in TUNING_SPEC:
            if cfg.has_option(_TUNING_SECTION, name):
                try:
                    values[name] = cfg.getfloat(_TUNING_SECTION, name)
                except ValueError:
                    pass
    return values


def main():
    ap = argparse.ArgumentParser(
        description='Convert Ulticap ASCII .SCH schematics to KiCad 9 format',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument('inputs', nargs='+',
                    help='.SCH or .BLK input file(s). For a hierarchical design, give '
                         'just the main (parent) sheet -- sub-sheets are discovered '
                         'and added automatically. Extra files can still be listed '
                         'explicitly if needed, e.g. sheets living in another folder.')
    ap.add_argument('-o', '--out', default='.',
                    help='Output directory (default: current directory)')
    ap.add_argument('-n', '--name', default='',
                    help='Base name for output file(s) (default: derived from first input)')
    ap.add_argument('--dump', action='store_true',
                    help='Print parsed data summary, do not write KiCad files')
    ap.add_argument('--dump-symbols', action='store_true',
                    help='Print all symbol names and their attributes')
    ap.add_argument('--block', action='store_true',
                    help='Convert to KiCad block library (.kicad_blocks) instead of '
                         'a schematic project. No .kicad_pro or .kicad_wks are written; '
                         'title symbols are suppressed.')
    ap.add_argument('--lib-name', metavar='NAME',
                    help='Block library name (default: stem of first input file). '
                         'Only used with --block.')
    ap.add_argument('--v5', action='store_true',
                    help='Rewrite the SCH version header to V5.00 in-place before '
                         'parsing (use for files created with Ulticap V5.x / Windows '
                         'version that have been saved in old format). '
                         'WARNING: modifies each input file in-place.')
    ap.add_argument('--fix-refdes', action='store_true',
                    help='If any reference designator does not end in a digit '
                         '(KiCad will not accept it as annotated -- see '
                         'kiuc_refdes.py), automatically write renamed '
                         '_REANNOT copies of the SCH hierarchy (and the matching '
                         '.DDF, if --ddf is given or one is found alongside the '
                         'first input file) and convert those instead. '
                         'Non-interactive: always applies the fix without prompting. '
                         'Without this flag, an affected design is converted '
                         'unchanged, with a warning printed and a log file written.')
    ap.add_argument('--ddf', default=None,
                    help='Path to the matching .DDF file, used only together with '
                         '--fix-refdes. If omitted, a same-named .DDF next to the '
                         'first input .SCH file is used automatically if one exists.')
    ap.add_argument('--kicad-colors', action='store_true',
                    help='Use KiCad\'s default colour theme instead of Ulticap\'s '
                         'measured colour palette: no explicit colour overrides are '
                         'emitted, so every item inherits its colour from the active '
                         'KiCad theme.')
    args = ap.parse_args()

    # Validate inputs
    inputs = [Path(p) for p in args.inputs]
    sch_files = [p for p in inputs if p.suffix.upper() in ('.SCH', '.BLK')]
    unknown   = [p for p in inputs if p.suffix.upper() not in ('.SCH', '.BLK', '.UTSCH')]

    if unknown:
        ap.error(f"Unrecognised file type(s): {', '.join(str(p) for p in unknown)}\n"
                 f"Only .SCH and .BLK files are accepted. Binary .UTSCH files are not supported in this release.")

    if not sch_files:
        ap.error('At least one .SCH file is required.')

    for f in sch_files:
        if not f.exists():
            ap.error(f"File not found: {f}")

    # Auto-discover hierarchical sub-sheets, mirroring the GUI and viewer:
    # any .SCH input's own referenced sub-sheets are added automatically, in
    # addition to whatever was explicitly listed. Block libraries (.BLK) are
    # never hierarchical, so discovery is skipped for them. The first
    # explicitly-given input stays first regardless of what gets appended --
    # it must remain the root sheet (see write_schematic/write_block_library,
    # which use sheets[0] as root).
    for f in list(sch_files):
        if f.suffix.upper() != '.SCH':
            continue
        for child in collect_hierarchy_paths(f):
            if child not in sch_files:
                sch_files.append(child)
                print(f'Auto-loaded sub-sheet: {child}')

    # Apply V5 header fix if requested
    if args.v5:
        for f in sch_files:
            ok, detail = force_v5_header(f)
            if ok:
                print(f'V5 header fix: {f.name}  [{detail}] → 5 00 00000000')
            else:
                print(f'WARNING: V5 header fix failed for {f.name}: {detail}')

    # Parse
    sheets: List[Sheet] = []
    for f in sch_files:
        print(f'Parsing: {f}')
        sheet, errors = parse_sch(f)
        for e in errors:
            print(e, file=sys.stderr)
        sheets.append(sheet)

    schematic = Schematic(sheets=sheets)

    # Missing sub-sheet warnings
    for w in check_missing_sheets(sheets):
        print(f'WARNING: {w}')

    # Dump modes -- pure inspection, never mutate or write files, so the
    # refdes check/fix below intentionally does not apply here.
    if args.dump:
        _dump(schematic)
        return

    if args.dump_symbols:
        _dump_symbols(schematic)
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Non-digit-ending reference designator check ─────────────────────────
    # KiCad will not accept a refdes that doesn't end in a digit (optionally
    # plus a single unit letter for a genuinely multi-unit symbol) as
    # annotated -- see kiuc_refdes.py for the full rationale. Detection
    # reuses the sheets already parsed above; no second parse unless a fix is
    # actually applied.
    offending = detect_offending_refdes({Path(sh.name).stem.upper(): sh for sh in sheets})
    if offending:
        if args.fix_refdes:
            ddf_path = args.ddf
            if not ddf_path:
                candidate = sch_files[0].with_suffix('.DDF')
                if not candidate.exists():
                    candidate = sch_files[0].with_suffix('.ddf')
                if candidate.exists():
                    ddf_path = str(candidate)

            result = reannotate_hierarchy(sch_files[0], ddf_path=ddf_path,
                                          out_dir=str(out_dir), suffix='_REANNOT')
            write_log(result, out_dir / f'{sch_files[0].stem}_REANNOT_log.txt',
                     root_sch_name=sch_files[0].name)

            print(f'{len(result.offending)} offending component instance(s) found '
                 f'-- fixing automatically (--fix-refdes):')
            for ch in result.changes:
                print(f'  {ch.old}  ->  {ch.new}')
            for p in result.sch_files_written:
                print(f'Written: {p}')
            if result.ddf_file_written:
                print(f'Written: {result.ddf_file_written}')
                print('A fresh KIUB conversion of the new DDF file is required.')
            elif ddf_path:
                print(f'WARNING: DDF path given/found ({ddf_path}) but could not be used; '
                     f'see {out_dir / (sch_files[0].stem + "_REANNOT_log.txt")}')

            # Continue the SAME conversion run on the new, renamed files --
            # one extra, deliberate parse pass over the corrected copies, not
            # a second attempt at the original ones.
            sch_files = result.sch_files_written
            sheets = []
            for f in sch_files:
                sheet, errors = parse_sch(f)
                for e in errors:
                    print(e, file=sys.stderr)
                sheets.append(sheet)
            schematic = Schematic(sheets=sheets)
        else:
            print(f'WARNING: {len(offending)} reference designator(s) do not end in a '
                 f'digit -- KiCad will treat these as unannotated:')
            for o in offending:
                print(f'  WARNING:   {o.refdes}  (symbol {o.symbol_name}, sheet {o.sheet_name})')
            print('WARNING: converting unchanged. Re-run with --fix-refdes to write '
                 'corrected copies automatically, or use kiuc_refdes.py directly.')
            result = ReannotateResult(offending=offending)
            write_log(result, out_dir / f'{sch_files[0].stem}_REANNOT_log.txt',
                     root_sch_name=sch_files[0].name)

    # Convert
    base = args.name or sch_files[0].stem
    set_use_kicad_colors(args.kicad_colors)
    set_tuning(_load_tuning_from_ini())
    if args.block:
        lib = getattr(args, 'lib_name', None) or base
        warns = write_block_library(schematic, out_dir, lib_name=lib)
        for w in warns:
            print(f'WARNING: {w}')
        print(f'Written: {out_dir / (lib + ".kicad_blocks")}')
    else:
        warns = write_schematic(schematic, out_dir, base_name=base)
        for w in warns:
            print(f'WARNING: {w}')

        if len(sheets) == 1:
            print(f'Written: {out_dir / (base + ".kicad_sch")}')
        else:
            for sh in sheets:
                stem = Path(sh.name).stem if sh.name else base
                print(f'Written: {out_dir / (stem + ".kicad_sch")}')


def _dump(sch: Schematic):
    """CLI wrapper: routes dump_schematic output to stdout."""
    def _print(text, tag=''):
        print(text)
    dump_schematic(sch, _print)


def _dump_symbols(sch: Schematic):
    """CLI wrapper: routes dump_symbols output to stdout."""
    def _print(text, tag=''):
        print(text)
    dump_symbols(sch, _print)


if __name__ == '__main__':
    main()
