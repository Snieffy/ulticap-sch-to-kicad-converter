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
kiuc_ascii.py — Parser for Ulticap ASCII .SCH files

File format coverage: Ulticap SCH V1.50 through V5.72. See online documentation.

File structure:
    Header       *P / version line / bounds line / *R
    Body         *S … *C … *LV … *V … *A … *X …
    End          **
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from kiuc_model import (
    Sheet, Symbol, SymbolPin, SymbolAttribute, Circle,
    Component, ComponentAttribute,
    Wire, UserLine, Junction, Annotation, Label,
    Polyline, convert_overline,
)


# ── public API ─────────────────────────────────────────────────────────────────

def parse_sch(path) -> tuple:
    """Parse a single .SCH file and return (Sheet, errors).

    errors is a list of error strings (empty for clean files).
    """
    text = Path(path).read_text(encoding='cp437')
    lines = [l.rstrip('\r\n') for l in text.splitlines()]
    return _AsciiParser(lines, name=Path(path).name).parse()


def force_v5_header(path) -> tuple:
    """Rewrite the SCH version line in-place to mark the file as V5.00.

    Background
    ----------
    Ulticap SCH files have a two-field version line immediately after the
    *P <customer name> record:

        *P CAP
        4 60 00000000       ← this line: <major> <minor> <NRM>

    Files created by the DOS versions of Ulticap (V4.xx and below) encode
    arcs differently from files created by the Windows 95 version (V5.xx+).
    The converter uses the major version number to select the correct arc
    decoding path (is_less_than_v500).

    When a V5.xx file has been incorrectly identified as V4.xx — for example
    because the user saved it with "Save as old format" — the arc fix for
    V5.xx files will not be applied, producing mis-drawn arcs in KiCad.

    This function rewrites the version line to '5 00 00000000', forcing the
    converter to treat the file as V5.xx and apply the arc corrections.

    The NRM field (8-digit project identifier) is reset to '00000000'.
    All other lines in the file are unchanged.

    WARNING: The file is modified in-place.  Make a backup before using
    this function if you need to preserve the original.

    Parameters
    ----------
    path : str or Path
        Path to the .SCH file to modify.

    Returns
    -------
    (True, old_line : str)
        Modification was successful; old_line is the original version line
        (stripped), useful for logging.
    (False, reason : str)
        Modification failed; reason describes the problem.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding='cp437')
    except OSError as exc:
        return (False, f'Cannot read file: {exc}')

    lines = text.splitlines(keepends=True)
    found_p = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('*P ') or stripped == '*P':
            found_p = True
            continue
        if found_p:
            # This is the version line — rewrite it.
            old_line = stripped
            # Preserve line ending of the original line.
            ending = '\r\n' if raw.endswith('\r\n') else '\n'
            lines[i] = '5 00 00000000' + ending
            try:
                # CP437, matching the read above -- this function only
                # intends to touch the version line and leave everything
                # else byte-for-byte unchanged, so the round-trip encoding
                # must be lossless for whatever's in the untouched lines
                # (extended DOS characters included). ascii+replace
                # previously corrupted those in place on every call.
                path.write_text(''.join(lines), encoding='cp437')
            except OSError as exc:
                return (False, f'Cannot write file: {exc}')
            return (True, old_line)

    return (False, 'Version line not found (no *P record in file)')


# ── helpers ────────────────────────────────────────────────────────────────────

def _to_int(s, default=0):
    try:
        return int(s)
    except (ValueError, TypeError):
        return default


def _attr_kv(tail):
    """Extract (tag, value) from '... TAG = value' tail string."""
    eq = tail.find('=')
    if eq < 0:
        return None
    tag = tail[:eq].strip()
    val = tail[eq + 1:].strip()
    if not tag:
        return None
    return tag, convert_overline(val)


def _parse_attr_line(line):
    """Parse a full attribute line.

    Format:
        <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag>=<value>
    Returns (dx,dy,dx_poe,dy_poe,size,colour,vis,tag,val) or None.
    """
    parts = line.split(None, 7)
    if len(parts) < 8:
        return None
    try:
        dx     = int(parts[0])
        dy     = int(parts[1])
        dx_poe = int(parts[2])
        dy_poe = int(parts[3])
        size   = int(parts[4])
        colour = int(parts[5])
        vis    = int(parts[6])
    except ValueError:
        return None
    kv = _attr_kv(parts[7])
    if not kv:
        return None
    tag, val = kv
    return dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val


_PLACEHOLDERS = frozenset(('-', '?', '*', '--', 'N/A', 'n/a', ''))

def _clean(v):
    if v is None:
        return None
    v = v.strip()
    return None if v in _PLACEHOLDERS else v


def _point_on_ortho_segment(px: int, py: int, x1: int, y1: int,
                            x2: int, y2: int) -> bool:
    """True if integer point (px,py) lies exactly on the axis-aligned segment
    (x1,y1)->(x2,y2). Diagonal segments always return False.

    Mirrors kiuc_writer._point_on_ortho_segment; kept as an independent
    copy here since this module must not import from kiuc_writer.
    """
    if x1 == x2:   # vertical
        return px == x1 and min(y1, y2) <= py <= max(y1, y2)
    elif y1 == y2: # horizontal
        return py == y1 and min(x1, x2) <= px <= max(x1, x2)
    return False


def _recover_bus_entry_flags(sheet: Sheet) -> None:
    """OR the parsed *V junction_type flag with a geometric bus-touch check.

    Ulticap always auto-places a junction the instant a wire-end (or a
    cluster of joined wires) lands on a bus -- there is no construct in
    Ulticap for a wire-junction that coincides with a bus point without
    that being a real connection, short of a corrupted or hand-edited
    file. So "this junction's point lies exactly on a bus segment" is, in
    valid Ulticap data, equivalent to "this is a wire-to-bus connection"
    -- the same fact the *V record's junction_type field is meant to
    encode for the same point.

    junction_type was found to come back as 0 for every single junction
    in files re-saved by Ulticap 5.72 (confirmed across a real, otherwise
    fully wire/bus-identical test pair: a V4.91 file has 50 of 99
    junctions correctly flagged junction_type=1, every one of which
    independently sits on a bus segment; the same design saved by V5.72
    has the literal *V token always 0 across all 99 junctions, while the
    wire/bus geometry itself, and the *V record's own net_number field,
    are both intact and decode correctly). That looks like the V5.72
    single-point *V export path simply never populates the field, not a
    parser misalignment: a misaligned read would also corrupt net_number,
    which it does not.

    Rather than special-case V5.72 (Ulticap's own version stamp in the
    header is not reliable, and the apparent "5 00" marker in some test
    files is this converter's own GUI-driven arc-fix flag from
    force_v5_header, not anything Ulticap itself writes), this recomputes
    is_bus_entry from geometry for every junction regardless of version
    and ORs it with whatever the file's *V record already said, so it is
    a strict no-op wherever the flag is already correct: in every test
    file checked, every flagged junction already sits on a bus segment
    and no unflagged junction does, so this OR changes nothing there
    while recovering the missing flag for V5.72-style exports.
    """
    bus_segs = [(w.x1, w.y1, w.x2, w.y2) for w in sheet.wires if w.is_bus]
    if not bus_segs:
        return
    for j in sheet.junctions:
        if j.is_bus_entry:
            continue
        if any(_point_on_ortho_segment(j.x, j.y, *s) for s in bus_segs):
            j.is_bus_entry = True


# ── parser ─────────────────────────────────────────────────────────────────────

class _AsciiParser:
    def __init__(self, lines, name=''):
        self.lines  = lines
        self.name   = name
        self.i      = 0
        self.errors: List[str] = []

    # ── line helpers ──────────────────────────────────────────────────────────

    def _next(self):
        while self.i < len(self.lines):
            l = self.lines[self.i]
            self.i += 1
            if l.strip():
                return l
        return None

    def _consume_until_semicolon(self):
        """Accumulate lines until one ends with ';'; strip the ';' and join."""
        buf = []
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            self.i += 1
            if l.endswith(';'):
                buf.append(l[:-1])
                break
            buf.append(l)
        return ','.join(buf)

    # ── top-level ─────────────────────────────────────────────────────────────

    def parse(self):
        sheet = Sheet(name=self.name)
        self._parse_header(sheet)
        self._parse_body(sheet)
        _recover_bus_entry_flags(sheet)
        return sheet, self.errors

    # ── header ────────────────────────────────────────────────────────────────

    def _parse_header(self, sheet):
        """*P <customer name>
           <major> <minor> [<NRM>]
           <x0>,<y0>,<x1>,<y1>,<id>;
           *R <root sheet name>
        """
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue

            if l.startswith('*P '):
                sheet.customer_name = l[3:].strip()
                self.i += 1

                # version line: <major> <minor> [<NRM>]
                vline = self._next() or ''
                vparts = vline.split()
                if len(vparts) >= 2:
                    sheet.major = _to_int(vparts[0], 4)
                    sheet.minor = _to_int(vparts[1], 60)
                    if len(vparts) >= 3:
                        sheet.nrm = vparts[2]
                    # V4.xx and below = DOS Ulticap; V5.xx+ = Windows Ulticap.
                    # Arc encoding differs between the two families.
                    sheet.is_less_than_v500 = (sheet.major < 5)

                # bounds line: <x0>,<y0>,<x1>,<y1>,<id>;
                bline = (self._next() or '').rstrip(';').strip()
                bparts = [p.strip() for p in bline.split(',')]
                if len(bparts) >= 4:
                    sheet.xmin = _to_int(bparts[0])
                    sheet.ymin = _to_int(bparts[1])
                    sheet.xmax = _to_int(bparts[2])
                    sheet.ymax = _to_int(bparts[3])
                    if len(bparts) >= 5:
                        sheet.grid = _to_int(bparts[4])  # grid spacing
                continue

            if l.startswith('*R '):
                sheet.root_sheet   = l[3:].strip()
                sheet.project_name = sheet.root_sheet  # compat for binary parser
                self.i += 1
                continue

            # Any other directive ends the header
            break

    # ── body ──────────────────────────────────────────────────────────────────

    def _parse_body(self, sheet):
        # is_less_than_v500 is already set from the header version number;
        # no *X scan needed here.
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == '**':
                self.i += 1
                return  # ** marks end of schematic data; extended section not used
            if l.startswith('*S'):
                self._parse_symbol(sheet, l)
            elif l.startswith('*C '):
                self._parse_component(sheet, l)
            elif l.startswith('*LV '):
                self._parse_wire_lv(sheet, l)
            elif l.startswith('*LT '):
                self._parse_wire_lt(sheet, l)
            elif l.startswith('*V '):
                self._parse_junction(sheet, l)
            elif l.startswith('*A '):
                self._parse_annotation(sheet, l)
            elif l.startswith('*X '):
                self._parse_label(sheet, l)
            elif l.startswith('*R ') and not sheet.root_sheet:
                sheet.root_sheet   = l[3:].strip()
                sheet.project_name = sheet.root_sheet
                self.i += 1
            else:
                self.i += 1

    # ── *S symbol ─────────────────────────────────────────────────────────────

    def _parse_symbol(self, sheet, header):
        """*S<name>
           <width> <height>
           <outline>;         section 1
           <pins>;            section 2
           <circles>;         section 3
           <attrs>
           ;                  end of symbol
        """
        sym_name = header[2:].strip()
        self.i += 1
        sym = Symbol(name=sym_name)

        wh = self._next() or ''
        parts = wh.split()
        if len(parts) >= 2:
            sym.width  = _to_int(parts[0])
            sym.height = _to_int(parts[1])

        sym.polylines = self._parse_outline()
        sym.pins      = self._parse_pins()
        sym.circles   = self._parse_circles()
        sym.sym_attrs, sym.attributes, sym.signal_pins = self._parse_symbol_attrs(
            pins=sym.pins, sym_width=sym.width, sym_height=sym.height,
            sym_name=sym_name)

        sheet.symbols[sym_name] = sym

    def _parse_outline(self):
        """Section 1: outline polylines, ends at ';'.

        The section is a flat comma-separated stream terminated by ';'.
        It contains one or more colour-groups; each group starts with the
        two-token header  1,<line_ctw>  followed by x,y coordinate pairs:

            1,<line_ctw>,x0,y0,...,xN,yN,1,<line_ctw>,x0,y0,...,xN,yN;

        <line_ctw> is a combined line_color + line_type + line_width value:
          line_color (bits 0-3):   0..15 palette index
          line_type  (bits 12-13): {0:solid,4096:dash,8192:dash-dot,12288:dot}
          line_width (bit 8):      {0:width=6mil,256:width=30mil}

        Because coordinates can equal 1, the group-start sentinel is identified
        positionally: a '1' that appears at an x-coordinate slot (i.e. right
        after the previous group's last y coordinate) starts a new group.
        The algorithm always consumes tokens in pairs after the header, so
        any '1' seen at an x-position is unambiguously a new group header.
        """
        polys = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return polys

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        if len(nums) < 4:
            return polys

        def _decode_ctw(line_ctw):
            colour   = line_ctw & 0x000F
            linetype = line_ctw & 0x3000
            width    = 30 if (line_ctw & 0x0100) else 6
            return colour, linetype, width

        # Scan positionally: start by expecting the first '1' sentinel
        i = 0
        colour, linetype, width = 0, 0, 6
        points = []
        expecting_header = True

        while i < len(nums):
            if expecting_header:
                # Skip anything before the first '1'
                if nums[i] != 1:
                    i += 1
                    continue
                if i + 1 >= len(nums):
                    break
                if points:
                    polys.append(Polyline(points=points, colour=colour,
                                          linetype=linetype, width=width))
                colour, linetype, width = _decode_ctw(nums[i + 1])
                points = []
                i += 2
                expecting_header = False
            else:
                # At an x-position: a '1' here starts a new group
                if nums[i] == 1 and i + 1 < len(nums):
                    if points:
                        polys.append(Polyline(points=points, colour=colour,
                                              linetype=linetype, width=width))
                    colour, linetype, width = _decode_ctw(nums[i + 1])
                    points = []
                    i += 2
                elif i + 1 < len(nums):
                    points.append((nums[i], nums[i + 1]))
                    i += 2
                else:
                    break

        if points:
            polys.append(Polyline(points=points, colour=colour,
                                  linetype=linetype, width=width))
        return polys

    def _parse_pins(self):
        """Section 2: pins, ends at ';'.
        Format: <pin_format>,<pin_rotation>,<rel_x>,<rel_y>,
        pin_format   = {0:none,1:INVERTED,2:CLOCK,...,10:CLOCK INVERTED}
        pin_rotation = {0:E(right),1:N(up),2:W(left),3:S(down)}
        """
        pins = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return pins

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        i = 0
        while i + 3 < len(nums):
            pin_format   = nums[i]
            pin_rotation = nums[i + 1]
            x            = nums[i + 2]
            y            = nums[i + 3]
            pins.append(SymbolPin(x=x, y=y,
                                  pin_format=pin_format,
                                  pin_rotation=pin_rotation))
            i += 4
        return pins

    def _parse_circles(self):
        """Section 3: circles, ends at ';'.
        Format per circle: <cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<arc_linetype>,
          thick:         0/1 flag {0:width=6mil,1:width=30mil}
          arc_linetype:  {0:solid,1:dash,2:dash-dot,3:dot}
        """
        circles = []
        raw = self._consume_until_semicolon()
        if not raw.strip():
            return circles

        tokens = [t.strip() for t in raw.split(',') if t.strip()]
        nums = []
        for t in tokens:
            try:
                nums.append(int(t))
            except ValueError:
                pass

        i = 0
        while i + 7 < len(nums):
            cx           = nums[i]
            cy           = nums[i + 1]
            r            = nums[i + 2]
            rotate       = nums[i + 3]
            angle        = nums[i + 4]
            colour       = nums[i + 5]
            thick_flag   = nums[i + 6]
            arc_linetype = nums[i + 7]
            thick = 30 if thick_flag else 6   # 0/1 flag → literal mils
            circles.append(Circle(cx=cx, cy=cy, r=r,
                                  rotate=rotate, angle=angle,
                                  colour=colour, thick=thick,
                                  arc_linetype=arc_linetype))
            i += 8
        return circles

    def _parse_symbol_attrs(self, pins=None, sym_width=0, sym_height=0, sym_name=''):
        """Section 4: attribute definitions, ends at lone ';'.

        Groups PINTYPE / # / LABEL records by their shared (dx_poe, dy_poe)
        coordinates and stamps the resolved values directly onto the matching
        SymbolPin.  This is order-independent: # before PINTYPE works fine.

        Matching uses the pin connection point (the wire-attach endpoint on the
        bbox face) rather than the pin body position.  Connection points are
        exact integers that coincide with the POE coords of the paired # / LABEL /
        PINTYPE attr lines, so the match is always exact rather than nearest.
        The nearest-distance fallback is retained for symbols where the POE does
        not land exactly on the connection point.

        TRANSX / TRANSY tags are silently discarded — redundant editor artefacts.
        SIGNAL tags are parsed into signal_pins and excluded from sym_attrs.
        """
        if pins is None:
            pins = []

        def _conn_pt(pin):
            """Pin connection point in *S local coords."""
            r = pin.pin_rotation & 3
            if r == 0: return (sym_width,  pin.y)
            if r == 1: return (pin.x,      sym_height)
            if r == 2: return (0,           pin.y)
            return          (pin.x,      0)

        sym_attrs = []
        attr_dict = {}

        # Collect all attr lines first
        raw_attrs = []
        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == ';':
                self.i += 1
                break
            if l.startswith('*'):
                break
            self.i += 1
            parsed = _parse_attr_line(l)
            if parsed:
                raw_attrs.append(parsed)

        # Build sym_attrs, POE groups, and signal_pins
        from collections import defaultdict
        poe_groups: dict = defaultdict(dict)
        signal_pins = []

        for dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val in raw_attrs:
            if tag in ('TRANSX', 'TRANSY'):
                continue  # discard — redundant, see docstring
            if tag == 'SIGNAL':
                # SIGNAL=VCC,14  or  SIGNAL=GND,1,21
                parts = [p.strip() for p in val.split(',')]
                if len(parts) >= 2:
                    signal_pins.append((parts[0], parts[1:]))
                continue  # metadata only, not a display attribute
            sym_attrs.append(SymbolAttribute(
                tag=tag, default_value=val,
                dx=dx, dy=dy, dx_poe=dx_poe, dy_poe=dy_poe,
                size=size, colour=colour, visibility=vis,
            ))
            attr_dict[tag] = val
            if tag in ('PINTYPE', 'LABEL', 'WIRELABEL') or tag.startswith('#'):
                poe_groups[(dx_poe, dy_poe)][tag] = val

        # Stamp PINTYPE / # / LABEL onto matching SymbolPin.
        #
        # Strategy: try exact match on connection point first (conn_pt == POE key).
        # Connection points are the wire-attach endpoints on the bbox face, which
        # always coincide exactly with the POE coordinates of the paired attr lines
        # in well-formed Ulticap symbols.  Using connection points instead of pin
        # body positions avoids tie-breaking ambiguity when two pins on opposite
        # sides of the body are equidistant from two POE groups (e.g. SHE14_2X3).
        #
        # For symbols where the POE does not land on the connection point (manual
        # placement), fall back to the nearest-distance heuristic.
        if pins and poe_groups:
            for pin in pins:
                cp = _conn_pt(pin)
                if cp in poe_groups:
                    best_key = cp
                else:
                    # Fallback: nearest POE by Euclidean distance.
                    # This only occurs when the POE coordinate in the *S attr
                    # does not coincide with any pin connection point — indicating
                    # a manually edited or corrupt symbol definition.
                    best_key = min(poe_groups,
                                   key=lambda p: (pin.x - p[0])**2 + (pin.y - p[1])**2)
                    num_val = poe_groups[best_key].get('#') or next(
                        (v for k, v in poe_groups[best_key].items() if k.startswith('#')), '')
                    self.errors.append(
                        f"Error: Symbol {sym_name} pin(s) {num_val} "
                        f"don't align with a wire entry point."
                    )
                group = poe_groups[best_key]
                if 'PINTYPE' in group:
                    pin.pin_type = group['PINTYPE']
                num_val = group.get('#')
                if num_val is None:
                    for k, v in group.items():
                        if k.startswith('#'):
                            num_val = v
                            break
                if num_val is not None:
                    pin.number = num_val
                # LABEL takes priority; WIRELABEL is fallback for PWR pins
                if 'LABEL' in group:
                    pin.name = group['LABEL']
                elif 'WIRELABEL' in group:
                    pin.name = group['WIRELABEL']

        return sym_attrs, attr_dict, signal_pins

    # ── *C component ─────────────────────────────────────────────────────────

    def _parse_component(self, sheet, header):
        """*C <X abs> <Y abs> <rotation> <symbol name>
           <attr line> …
           ;
        """
        parts = header.split(None, 4)
        if len(parts) < 5:
            self.i += 1
            return
        x        = _to_int(parts[1])
        y        = _to_int(parts[2])
        rot      = _to_int(parts[3])
        sym_name = parts[4].strip()
        self.i += 1

        comp = Component(x=x, y=y, rotation=rot, symbol_name=sym_name)

        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            if not l:
                self.i += 1
                continue
            if l == ';':
                self.i += 1
                break
            if l.startswith('*'):
                break
            self.i += 1
            parsed = _parse_attr_line(l)
            if parsed:
                dx, dy, dx_poe, dy_poe, size, colour, vis, tag, val = parsed
                comp.comp_attrs.append(ComponentAttribute(
                    tag=tag, value=val,
                    dx=dx, dy=dy, dx_poe=dx_poe, dy_poe=dy_poe,
                    size=size, colour=colour, visibility=vis,
                ))
                comp.attributes[tag] = val

        a = comp.attributes
        comp.refdes    = _clean(a.get('REFDES'))
        comp.device    = _clean(a.get('DEVICE'))
        comp.value     = _clean(a.get('VALUE'))
        comp.pkg_type  = _clean(a.get('PKG_TYPE'))
        comp.wirelabel = _clean(a.get('WIRELABEL'))
        _fr = _clean(a.get('FILE'))
        # If *C has no FILE=, check the *S symbol definition (FILE= lives there
        # when the subsheet box has no per-instance attribute override).
        if _fr is None and comp.symbol_name in sheet.symbols:
            _sym_fr = _clean(sheet.symbols[comp.symbol_name].attributes.get('FILE'))
            if _sym_fr is not None:
                _fr = _sym_fr
        # Normalise: if FILE= has no extension, append .SCH (e.g. SHEET1 → SHEET1.SCH)
        if _fr is not None and '.' not in Path(_fr).name:
            _fr = _fr + '.SCH'
        comp.file_ref  = _fr
        comp.sheet_name = sheet.name

        sheet.components.append(comp)

    # ── *LV wire ─────────────────────────────────────────────────────────────

    def _parse_wire_lv(self, sheet, line):
        """*LV <level> <startx> <starty> <endx> <endy> <l1> <l2> <l3>

        level=1: netlist layer  l1=net_number  l2={0:wire,1:bus}
        level=2: user layer     l1=linetype+colour  l2=thickness×10mil
        other  : log warning
        """
        parts = line.split()
        if len(parts) < 6:
            self.i += 1
            return

        level  = _to_int(parts[1])
        startx = _to_int(parts[2])
        starty = _to_int(parts[3])
        endx   = _to_int(parts[4])
        endy   = _to_int(parts[5])
        l1     = _to_int(parts[6]) if len(parts) > 6 else 0
        l2     = _to_int(parts[7]) if len(parts) > 7 else 0

        if level == 1:
            sheet.wires.append(Wire(
                x1=startx, y1=starty, x2=endx, y2=endy,
                net_id=l1, is_bus=(l2 == 1),
            ))
        elif level == 2:
            linetype  = l1 & 0xFF00
            colour    = l1 % 16
            sheet.user_lines.append(UserLine(
                x1=startx, y1=starty, x2=endx, y2=endy,
                linetype=linetype, colour=colour,
                thickness=l2 * 10,   # l2 is in 10-mil units → store in mils
            ))
        else:
            print(f"[kiuc_ascii] WARNING: unknown *LV level {level}: {line.strip()}")

        self.i += 1

    # ── *LT wire ──────────────────────────────────────────────────────────────

    def _parse_wire_lt(self, sheet, header):
        """*LT <level> <start0>
        <start1> <end1> <l1> <l2> <l3> ;

        start0  : fixed/anchor coordinate
        start1  : start of varying coordinate
        end1    : end of varying coordinate
        l1      : net number (level 0/1)  OR  colour (level 2)
        l2      : {0:wire, 1:bus} (level 0/1)  OR  width {0:5mil, 1:20mil} (level 2)
        l3      : orientation {4:horizontal, 5:vertical, 6:diagonal NE/SW, 7:diagonal SE/NW}

        Multiple segments share the same header and may appear on separate data lines
        before the closing ';'.

        Orientation → coordinates (verified against LTV1_colors.SCH colour test):
          4 horizontal:        y fixed = start0;  x: start1→end1
          5 vertical:          x fixed = start0;  y: start1→end1
          6 diagonal NE/SW (slope +1): x1=(start0+start1)/2, y1=(start1-start0)/2,
                                        x2=(start0+end1)/2,   y2=(end1-start0)/2
          7 diagonal SE/NW (slope -1): x1=(start0+start1)/2, y1=(start0-start1)/2,
                                        x2=(start0+end1)/2,   y2=(start0-end1)/2
        """
        parts = header.split()
        if len(parts) < 3:
            self.i += 1
            return
        level  = _to_int(parts[1])
        start0 = _to_int(parts[2])
        self.i += 1

        while self.i < len(self.lines):
            l = self.lines[self.i].strip()
            done = l.endswith(';')
            if done:
                l = l[:-1].strip()
            self.i += 1

            if not l or l.startswith('*'):
                if l.startswith('*'):
                    self.i -= 1
                break

            seg_parts = l.split()
            if len(seg_parts) >= 5:
                start1 = _to_int(seg_parts[0])
                end1   = _to_int(seg_parts[1])
                l1     = _to_int(seg_parts[2])
                l2     = _to_int(seg_parts[3])
                l3     = _to_int(seg_parts[4])   # orientation

                # Compute (x1,y1) -> (x2,y2) from start0/start1/end1 + orientation
                if l3 == 4:    # HORIZONTAL: y=start0 fixed, x: start1→end1
                    x1, y1, x2, y2 = start1, start0, end1, start0
                elif l3 == 5:  # VERTICAL: x=start0 fixed, y: start1→end1
                    x1, y1, x2, y2 = start0, start1, start0, end1
                elif l3 == 6:  # DIAGONAL NE/SW: x=(start0+start)/2, y=(start-start0)/2
                    x1 = (start0 + start1) // 2
                    y1 = (start1 - start0) // 2
                    x2 = (start0 + end1)   // 2
                    y2 = (end1   - start0) // 2
                elif l3 == 7:  # DIAGONAL SE/NW: x=(start0+start)/2, y=(start0-start)/2
                    x1 = (start0 + start1) // 2
                    y1 = (start0 - start1) // 2
                    x2 = (start0 + end1)   // 2
                    y2 = (start0 - end1)   // 2
                else:
                    if done:
                        break
                    continue

                if level in (0, 1):
                    sheet.wires.append(Wire(
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        net_id=l1, is_bus=(l2 == 1),
                    ))
                elif level == 2:
                    # l1 = linetype + colour (same encoding as *LV level 2):
                    #   high byte: {0:solid,256:dashed,512:dash-dot,768:dot}
                    #   low byte:  colour index
                    # l2 = width {0:5mil, 1:20mil}
                    colour    = l1 & 0x00FF
                    linetype  = l1 & 0xFF00
                    thickness = 5 if l2 == 0 else 20   # mils
                    sheet.user_lines.append(UserLine(
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        linetype=linetype, colour=colour, thickness=thickness,
                    ))

            if done:
                break

    # ── *V junction ──────────────────────────────────────────────────────────

    def _parse_junction(self, sheet, header):
        """*V <X common>
           <Y> <net_number> <junction_type> <type>;

        junction_type: 0 = wire-to-wire (or bus-to-bus) junction,
                       1 = wire-to-bus connection (bus entry).

        Old format groups multiple Y values under one X, one group per line.
        _consume_until_semicolon joins lines with ',' so replace commas
        with spaces before tokenising.
        """
        x = _to_int(header.split()[1])
        self.i += 1

        raw = self._consume_until_semicolon()
        tokens = raw.replace(',', ' ').split()

        i = 0
        while i < len(tokens):
            try:
                y = int(tokens[i])
            except ValueError:
                i += 1
                continue
            junction_type = int(tokens[i + 2]) if i + 2 < len(tokens) else 0
            sheet.junctions.append(Junction(x=x, y=y,
                                            is_bus_entry=(junction_type != 0)))
            i += 4   # Y + net_number + junction_type + type

    # ── *A annotation ─────────────────────────────────────────────────────────

    def _parse_annotation(self, sheet, line):
        """*A <X abs> <Y abs> <X POE abs> <Y POE abs> <size> <color> <visibility> <tag>=<value>
        Single line, no trailing ';'.
        """
        parts = line.split(None, 8)
        if len(parts) < 9:
            self.i += 1
            return
        try:
            x      = int(parts[1]); y      = int(parts[2])
            x_poe  = int(parts[3]); y_poe  = int(parts[4])
            size   = int(parts[5]); colour = int(parts[6])
            vis    = int(parts[7])
        except (ValueError, IndexError):
            self.i += 1
            return

        kv = _attr_kv(parts[8])
        if kv:
            tag, val = kv
        else:
            tag, val = 'LABEL', convert_overline(parts[8])

        sheet.annotations.append(Annotation(
            x=x, y=y, x_poe=x_poe, y_poe=y_poe,
            text=val, size=size, colour=colour, visibility=vis, tag=tag,
        ))
        self.i += 1

    # ── *X label ─────────────────────────────────────────────────────────────

    def _parse_label(self, sheet, line):
        """*X <x> <y> <size> <colour> <rotation-t> <align-t> <text>

        rotation-t: 1/64-degree units; 0=horizontal, 5760=vertical,
                    all other values treated as 0.
        align-t:    0-8 {0:BL,1:BC,2:BR,3:CL,4:CC,5:CR,6:TL,7:TC,8:TR};
                    always visible.
        """
        parts = line.split(None, 7)
        if len(parts) < 8:
            self.i += 1
            return
        try:
            x        = int(parts[1]); y      = int(parts[2])
            size     = int(parts[3]); colour = int(parts[4])
            rotation = int(parts[5])
            align    = int(parts[6]) if parts[6].lstrip('-').isdigit() else 0
            text     = convert_overline(parts[7])
        except (ValueError, IndexError):
            self.i += 1
            return

        # Clamp rotation: only 0 (horizontal) and 5760 (vertical/90°) are valid
        rotation = 5760 if rotation == 5760 else 0
        # Clamp align to valid range 0-8
        align = max(0, min(8, align))

        sheet.labels.append(Label(x=x, y=y, text=text,
                                  size=size, colour=colour,
                                  rotation=rotation, align=align))
        self.i += 1


# ── hierarchical design discovery ─────────────────────────────────────────────
#
# Shared by kiuc_viewer.py (auto-populated "Sheets" tree) and kiuc_gui.py
# (auto-loading the converter's input list from a single selected main
# sheet). Single-sourced here since both consumers already import parse_sch
# from this module.

def natural_sort_key(text: str):
    """Splits a label into alternating text/number chunks so 'U2' sorts
    before 'U10' instead of after it (plain string sort would put '1'
    ahead of '2' character-by-character, e.g. U10 < U2)."""
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in re.split(r'(\d+)', text)]


def load_sheet(path) -> Optional[Sheet]:
    path = Path(path)
    if not path.exists() or path.suffix.upper() not in ('.SCH', '.BLK'):
        return None
    sheet, errors = parse_sch(path)
    for e in errors:
        print(e, file=sys.stderr)
    return sheet


def resolve_file_ref(ref: str, folder: Path) -> Optional[Path]:
    """Resolve one component's FILE_REF string to an actual file on disk,
    against the folder its parent sheet lives in. Handles the '.SCH'
    suffix being implicit and Ulticap sometimes storing the reference in
    the wrong case (falls back to a case-insensitive folder scan). Returns
    None if nothing on disk matches. Shared by hierarchy_children (builds
    the whole Sheets-panel tree) and the canvas's double-click-to-enter-
    subsheet handler (resolves a single clicked component)."""
    name = ref if ref.upper().endswith(('.SCH', '.BLK')) else ref + '.SCH'
    candidate = folder / name
    if candidate.exists():
        return candidate
    match = next((p for p in folder.iterdir()
      if p.suffix.upper() in ('.SCH', '.BLK')
      and p.name.upper() == name.upper()), None)
    return match


def hierarchy_children(sheet: Sheet, folder: Path) -> List[Tuple[str, Path]]:
    """Sub-sheets referenced by the currently loaded sheet (its components'
    FILE_REF attribute — the same field kiuc_writer.py follows to write
    sub-sheets recursively), resolved against the folder the parent file
    lives in. Returns (display_label, resolved_path) pairs for files that
    actually exist on disk, naturally sorted by refdes (U2 before U10) with
    duplicates removed."""
    seen = set()
    out: List[Tuple[str, Path]] = []
    for comp in sheet.components:
        ref = getattr(comp, 'file_ref', None)
        if not ref:
            continue
        candidate = resolve_file_ref(ref, folder)
        if candidate is None:
            continue
        key = candidate.resolve()
        if key in seen:
            continue
        seen.add(key)
        out.append((candidate.stem, candidate))
    out.sort(key=lambda entry: natural_sort_key(entry[0]))
    return out


class HierNode:
    """One node of the sub-sheet hierarchy tree."""
    __slots__ = ('label', 'path', 'children')
    def __init__(self, label: str, path: Path, children: List['HierNode']):
        self.label = label
        self.path = path
        self.children = children


def build_hierarchy_tree(root_path: Path, max_depth: int = 12) -> Optional[HierNode]:
    """Recursively resolve the full sub-sheet hierarchy starting at
    root_path (parses every descendant file, not just the next level —
    needed to show grandchildren etc. all at once instead of one level
    at a time). Guards against two failure modes a hand-edited Ulticap
    hierarchy could in principle hit:
      - a cycle (a sheet that, directly or via descendants, references one
        of its own ancestors) — detected per-branch, not globally, so two
        unrelated sub-sheets are still free to reference the same file;
      - runaway depth, as a backstop in case cycle detection ever misses
        an unusual reference pattern.
    Returns None if root_path itself can't be read."""
    root_path = Path(root_path)
    if not root_path.exists():
        return None

    def walk(path: Path, ancestors: frozenset, depth: int, label: str) -> HierNode:
        node = HierNode(label, path, [])
        key = path.resolve()
        if key in ancestors or depth >= max_depth:
            return node   # cycle or depth guard — leave as a leaf, don't recurse further
        sheet = load_sheet(path)
        if sheet:
            branch = ancestors | {key}
            for child_label, child_path in hierarchy_children(sheet, path.parent):
                node.children.append(walk(child_path, branch, depth + 1, child_label))
        return node

    return walk(root_path, frozenset(), 0, root_path.stem)


def collect_hierarchy_paths(root_path: Path) -> List[Path]:
    """Return every unique .SCH/.BLK path in the hierarchy rooted at root_path,
    naturally sorted, starting with the root itself. Used by batch export and
    by kiuc_gui.py's auto-load."""
    tree = build_hierarchy_tree(root_path)
    if not tree:
        return [Path(root_path)]
    seen: set = set()
    paths: List[Path] = []
    def walk(node: HierNode):
        key = node.path.resolve()
        if key in seen:
            return
        seen.add(key)
        paths.append(node.path)
        for child in node.children:
            walk(child)
    walk(tree)
    return paths
