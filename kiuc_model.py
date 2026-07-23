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
kiuc_model.py — Shared data model for Ulticap schematics.

Coordinates are in Ulticap units: 1 unit = 2 mils = 0.0508 mm.
Y-axis convention: positive Y is UP in both ASCII and binary.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple


# ── constants ──────────────────────────────────────────────────────────────────

MM_PER_UNIT  = 0.0508     # 1 Ulticap unit = 2 mils = 0.0508 mm
MILS_PER_UNIT = 2         # 1 Ulticap unit = 2 mils
MM_PER_MIL   = 0.0254     # for values stored directly in raw mils (e.g. line/circle thickness)

# rotation encoding: 0=0° 1=90° 2=180° 3=270° 4=mirror+0° … 7=mirror+270°
_ROT_ANGLE  = (0, 90, 180, 270, 0,  90, 180, 270)
_ROT_MIRROR = (0,  0,   0,   0, 1,   1,   1,   1)

# PINTYPE tag values (from attribute lines) → KiCad electrical type
PINTYPE_TAG_MAP = {
    'PAS': 'passive',   'IN':  'input',    'OUT': 'output',
    'OC':  'open_collector', 'OE': 'open_emitter',
    'BI':  'bidirectional',  'TRI': 'tri_state',
    'PWR': 'power_in',  'NC':  'no_connect',
}

# Ulticap symbol-polyline linetype code → canonical style name.
# Shared by kiuc_writer.py (maps the name directly to a KiCad stroke "type")
# and kiuc_viewer.py (maps the name to a Qt dash pattern for on-screen
# rendering). Kept as a single source so a newly discovered code — e.g.
# from the binary UTSCH format — only needs to be added in one place.
UC_POLYLINE_STYLE_BY_CODE: Dict[int, str] = {
    0: 'solid', 4096: 'dash', 8192: 'dash_dot', 12288: 'dot',
}

# Ulticap symbol-arc linetype code → canonical style name. Same sharing
# rationale as UC_POLYLINE_STYLE_BY_CODE above; arcs use a distinct,
# smaller code range from polylines so the two are kept as separate tables.
UC_ARC_STYLE_BY_CODE: Dict[int, str] = {
    0: 'solid', 1: 'dash', 2: 'dash_dot', 3: 'dot',
}


def convert_overline(text: str) -> str:
    """Convert Ulticap overline syntax ^text^ to KiCad ~{text}.

    Rules (from spec):
    - First '^' starts overline.
    - Second '^' ends it.
    - If no second '^', overline continues to end of text.
    """
    if '^' not in text:
        return text
    result = []
    i = 0
    while i < len(text):
        if text[i] == '^':
            # Find closing '^'
            j = text.find('^', i + 1)
            if j == -1:
                # No closing: overline to end
                inner = text[i + 1:]
                result.append(f'~{{{inner}}}')
                break
            else:
                inner = text[i + 1:j]
                result.append(f'~{{{inner}}}')
                i = j + 1
        else:
            # Find next '^'
            j = text.find('^', i)
            if j == -1:
                result.append(text[i:])
                break
            else:
                result.append(text[i:j])
                i = j
    return ''.join(result)


def rotation_angle(rot: int) -> int:
    return _ROT_ANGLE[rot & 7]

def rotation_mirror(rot: int) -> bool:
    return bool(_ROT_MIRROR[rot & 7])


# ── geometry primitives ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def __iter__(self):
        return iter((self.x, self.y))


@dataclass
class Polyline:
    """Graphic polyline (list of (x,y) integer tuples).
    Group header token is a combined <line_ctw> value:
    <line_ctw> = line_color | line_type | line_width
      line_color (bits 0-3):  0..15 palette index
      line_type  (bits 12-13): {0:solid,4096:dash,8192:dash-dot,12288:dot}
      line_width (bit 8):      {0:width=6mil,256:width=30mil}
    """
    points: List[Tuple[int, int]] = field(default_factory=list)
    colour: int = 0     # masked palette index (0-15), from the 1,<colour>,... group header
    is_bus: bool = False
    linetype: int = 0   # raw code: 0=solid,4096=dash,8192=dash-dot,12288=dot
    width: int = 6       # mils; 6 (flag=0) or 30 (flag=256)


@dataclass
class Circle:
    """Circle/arc from a *S definition.
    Spec: <cx>,<cy>,<r>,<rotate>,<angle>,<colour>,<thick>,<arc_linetype>,
    rotate:       start angle raw (degrees = value/64), CCW, 0=East
    angle:        arc sweep raw (23040 = full circle = 360 deg)
    thick:        raw file value is a 0/1 flag {0:width=6mil,1:width=30mil};
                  stored here already converted to literal mils (6 or 30).
    arc_linetype: raw code {0:solid,1:dash,2:dash-dot,3:dot}
    """
    cx: int
    cy: int
    r: int
    rotate: int = 0      # raw start angle (deg = rotate/64)
    angle: int = 23040   # raw sweep (23040 = full circle)
    colour: int = 0
    thick: int = 6        # mils (6 or 30; converted from the 0/1 file flag)
    arc_linetype: int = 0  # raw code: 0=solid,1=dash,2=dash-dot,3=dot

    @property
    def start_deg(self) -> float:
        return self.rotate / 64.0

    @property
    def sweep_deg(self) -> float:
        return self.angle / 64.0

    @property
    def is_full_circle(self) -> bool:
        return self.angle >= 23040


@dataclass
class SymbolPin:
    """A pin entry in a *S block.
    Spec: <pin format>, <pin rotation>, <pin rel X>, <pin rel Y>
    pin_format: decorative marker (0=none, 1=inverted, 2=clock, …)
    pin_rotation: 0=EAST(right), 1=NORTH(up), 2=WEST(left), 3=SOUTH(down)
    x, y: relative position within symbol bounding box
    """
    x: int
    y: int
    pin_format: int = 0    # decorative: {0:none,1:INVERTED,2:CLOCK,...}
    pin_rotation: int = 0  # direction: {0:E,1:N,2:W,3:S}
    pin_type: str = 'PAS'  # from paired PINTYPE attribute tag
    number: str = ''       # from paired # attribute tag
    name: str = ''         # from paired LABEL attribute tag

    @property
    def kicad_angle(self) -> int:
        """KiCad pin angle (degrees, CCW from East)."""
        return (self.pin_rotation * 90) % 360


@dataclass
class SymbolAttribute:
    """One attribute slot in a *S definition (template with position info).
    Spec: <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag> = <value>
    """
    tag: str
    default_value: str = ''
    dx: int = 0       # X relative to symbol origin
    dy: int = 0       # Y relative to symbol origin
    dx_poe: int = 0   # X POE relative
    dy_poe: int = 0   # Y POE relative
    size: int = 35    # text height in mils
    colour: int = 0
    visibility: int = 128  # 0=none,64=TAG,128=VALUE,192=TAG+VALUE + alignment


@dataclass
class ComponentAttribute:
    """One attribute on a placed component (*C block).
    Spec: <X rel> <Y rel> <X POE rel> <Y POE rel> <size> <color> <visibility> <tag> = <value>
    """
    tag: str
    value: str
    dx: int = 0
    dy: int = 0
    dx_poe: int = 0
    dy_poe: int = 0
    size: int = 35
    colour: int = 0
    visibility: int = 128


# ── schematic objects ──────────────────────────────────────────────────────────

@dataclass
class Symbol:
    """Symbol definition (*S block)."""
    name: str
    width: int = 0
    height: int = 0
    polylines: List[Polyline]        = field(default_factory=list)
    pins:      List[SymbolPin]       = field(default_factory=list)
    circles:   List[Circle]          = field(default_factory=list)
    sym_attrs: List[SymbolAttribute] = field(default_factory=list)
    # Convenience dict: tag → default_value (for quick lookup)
    attributes: Dict[str, str]       = field(default_factory=dict)
    # Invisible power pins from SIGNAL= tags: list of (net_name, [pin_numbers])
    # e.g. SIGNAL=VCC,14  →  ('VCC', ['14'])
    # e.g. SIGNAL=GND,1,21 → ('GND', ['1', '21'])
    signal_pins: List[Tuple[str, List[str]]] = field(default_factory=list)


@dataclass
class Component:
    """Placed component instance (*C block)."""
    x: int
    y: int
    rotation: int          # 0–3: {0:0°, 1:90°, 2:180°, 3:270°} CCW
    symbol_name: str
    # Full attribute list preserving position/style metadata
    comp_attrs: List[ComponentAttribute] = field(default_factory=list)
    # Backward-compat flat dict (tag → value)
    attributes: Dict[str, str] = field(default_factory=dict)

    # Promoted well-known attributes
    refdes:    Optional[str] = None
    device:    Optional[str] = None
    value:     Optional[str] = None
    pkg_type:  Optional[str] = None
    wirelabel: Optional[str] = None
    file_ref:  Optional[str] = None

    sheet_name: Optional[str] = None

    @property
    def angle(self) -> int:
        return rotation_angle(self.rotation)

    @property
    def mirror(self) -> bool:
        return rotation_mirror(self.rotation)

    def display_refdes(self) -> str:
        return (self.refdes or self.attributes.get('REFDES') or
               (self.symbol_name if self.symbol_name in ('TITLE', 'TITLE_REV') else '?'))


@dataclass
class Wire:
    """Net wire segment (*LV level=1)."""
    x1: int
    y1: int
    x2: int
    y2: int
    net_id: int = 0    # l1 = net number
    is_bus: bool = False  # l2: 0=wire, 1=bus


@dataclass
class UserLine:
    """User/graphic layer line (*LV level=2)."""
    x1: int
    y1: int
    x2: int
    y2: int
    linetype: int = 0   # 0=solid,256=dashed,512=dash-dot,768=dot
    colour: int = 0
    thickness: int = 0  # l2 = line thickness in mils (direct, not x10)


@dataclass
class Junction:
    """Junction dot (*V).

    is_bus_entry: True when the *V record's junction_type field == 1,
    meaning the junction connects a signal wire to a bus.  False (0)
    means an ordinary wire-to-wire (or bus-to-bus) junction.
    """
    x: int
    y: int
    is_bus_entry: bool = False


@dataclass
class Annotation:
    """Free-text annotation (*A).
    Spec: *A <X abs> <Y abs> <X POE abs> <Y POE abs> <size> <color> <visibility> <tag>=<value>
    """
    x: int       # absolute position
    y: int
    x_poe: int   # POE absolute position
    y_poe: int
    text: str
    size: int = 35
    colour: int = 0
    visibility: int = 128
    tag: str = 'LABEL'  # e.g. LABEL, WIDTH, TXT


@dataclass
class Label:
    """Cross-reference / net label (*X records)."""
    x: int
    y: int
    text: str
    size: int = 35
    colour: int = 0
    rotation: int = 0  # raw 1/64-degree units: 0=horizontal, 5760=vertical
    style: int = 0     # 0=normal,2=bold,4=italic,3=bold+italic
    align: int = 0     # alignment A-byte 0-8 {0:BL,1:BC,2:BR,3:CL,4:CC,5:CR,6:TL,7:TC,8:TR}


# ── sheet ──────────────────────────────────────────────────────────────────────

@dataclass
class Sheet:
    """One schematic sheet."""
    name: str                  # filename, e.g. 'DESIGN.SCH'
    customer_name: str = ''    # *P <customer name> (optional documentation)
    major: int = 4             # version major (must be 4)
    minor: int = 60            # version minor (must be 60)
    nrm: str = '00000000'      # 8-digit NRM data
    xmin: int = -3025          # sheet bounds (Ulticap units)
    ymin: int = -2175
    xmax: int = 3021
    ymax: int = 2159
    grid: int = 0              # grid spacing (5th field of bounds line)
    root_sheet: str = ''       # *R <root sheet name>

    symbols:     Dict[str, Symbol]   = field(default_factory=dict)
    components:  List[Component]     = field(default_factory=list)
    wires:       List[Wire]          = field(default_factory=list)
    user_lines:  List[UserLine]      = field(default_factory=list)
    junctions:   List[Junction]      = field(default_factory=list)
    annotations: List[Annotation]    = field(default_factory=list)
    labels:      List[Label]         = field(default_factory=list)

    # Format discriminator: True = file created by Ulticap DOS versions (V4.xx and below).
    # False = file created by Ulticap Windows versions (V5.xx and above).
    # Affects arc rendering: V5.xx has arc encoding differences (complement-arc bug,
    # semicircle flip) that require correction before KiCad output.
    # Set from the version number in the SCH header (major < 5), not from *X records.
    is_less_than_v500: bool = False

    # Legacy fields kept for binary parser compatibility
    app_name: str = 'CAP'
    flags: int = 0
    grid: int = 25
    project_name: str = ''

    # Writer-internal scratch state — NOT part of the parsed Ulticap model.
    # Populated by kiuc_writer.py during S-expression emission to record
    # which (possibly de-duplicated/renamed) lib_id each component was
    # written under, keyed by id(component). Declared here (rather than
    # monkey-patched onto the instance) so static type checkers can see it.
    _lib_id_override: Dict[int, str] = field(default_factory=dict)

    # Writer-internal scratch state, same rationale as _lib_id_override:
    # maps an original Ulticap symbol name to its collision-safe KiCad
    # lib_id (e.g. when two differently-cased or invalid-character symbol
    # names would otherwise clash).
    _lib_name_map: Dict[str, str] = field(default_factory=dict)

    def contains(self, x: int, y: int) -> bool:
        return self.xmin <= x <= self.xmax and self.ymin <= y <= self.ymax

    @property
    def width_mm(self) -> float:
        """Sheet width in mm (for KiCad paper size selection)."""
        return (self.xmax - self.xmin) * 0.0508

    @property
    def height_mm(self) -> float:
        """Sheet height in mm."""
        return (self.ymax - self.ymin) * 0.0508


@dataclass
class Schematic:
    """Top-level container: one or more sheets."""
    sheets: List[Sheet] = field(default_factory=list)

    @property
    def primary(self) -> Optional[Sheet]:
        return self.sheets[0] if self.sheets else None


# ── geometry helpers ───────────────────────────────────────────────────────────

def rot_transform(dx, dy, rotation: int) -> tuple:
    """Apply Ulticap rotation+mirror to a (dx, dy) offset.

    The mirror flag (bit 2) negates the x-component of the rotation result.
    Verified against netlist ground truth for all 8 rotation values:

      rot 0 (  0°, no mirror): (+dx, +dy)
      rot 1 ( 90°, no mirror): (-dy, +dx)
      rot 2 (180°, no mirror): (-dx, -dy)   ← full 180° rotation
      rot 3 (270°, no mirror): (+dy, -dx)
      rot 4 (  0°, mirror):    (-dx, +dy)   ← mirror negates x
      rot 5 ( 90°, mirror):    (+dy, +dx)   ← mirror negates x vs rot 1
      rot 6 (180°, mirror):    (+dx, -dy)   ← mirror negates x vs rot 2
      rot 7 (270°, mirror):    (-dy, -dx)   ← mirror negates x vs rot 3
    """
    r = rotation & 3
    mirror = rotation & 4
    if r == 0:
        ox, oy =  dx,  dy
    elif r == 1:
        ox, oy = -dy,  dx
    elif r == 2:
        ox, oy = -dx, -dy
    else:
        ox, oy =  dy, -dx
    if mirror:
        ox = -ox
    return ox, oy


def pin_conn_point(pin: 'SymbolPin', sym: 'Symbol') -> tuple:
    """Return the pin connection point (wire-attach point) in *S coordinates.

    *S coordinate system: origin = BOTTOM-LEFT of bbox, x+ right, y+ UP.
    The connection point is on the bbox face the pin stub points TOWARD
    (i.e. away from the symbol body):
      rot=0 (E, stub right):  conn = (sym.width,  pin.y)      — right edge
      rot=1 (N, stub up):     conn = (pin.x,      sym.height) — top edge
      rot=2 (W, stub left):   conn = (0,           pin.y)     — left edge
      rot=3 (S, stub down):   conn = (pin.x,       0)         — bottom edge
    """
    r = pin.pin_rotation & 3
    if r == 0:  return (sym.width,  pin.y)
    if r == 1:  return (pin.x,      sym.height)
    if r == 2:  return (0,           pin.y)
    return          (pin.x,      0)


# ── colour / alignment helpers ──────────────────────────────────────────────
# Single source of truth shared by kiuc_writer.py (KiCad export) and
# kiuc_viewer.py (screen/SVG/PDF/PNG rendering), so screen colours, print
# colours, and KiCad output colours never drift apart across the two files.

ULTICAP_PALETTE: Dict[int, tuple] = {
    # Screen-measured values from DOS Ulticap (CRT output, EGA DAC levels).
    # The EGA half-intensity level is 170 (0xAA), not the theoretical 128.
    0:  (0,   0,   0  ),  # black
    1:  (0,   0,   142),  # blue          — invisible pins
    2:  (0,   170, 0  ),  # green         — junctions
    3:  (0,   170, 170),  # cyan          — pin stubs
    4:  (170, 0,   0  ),  # red           — wires
    5:  (170, 0,   170),  # magenta       — symbol body default
    6:  (125, 85,  20 ),  # brown         — user lines
    7:  (170, 170, 170),  # light gray
    8:  (40,  40,  65 ),  # gray          — dark blue-gray
    9:  (32,  32,  235),  # light blue    — buses
    10: (0,   255, 0  ),  # light green
    11: (0,   170, 150),  # light cyan
    12: (170, 0,   85 ),  # light red
    13: (235, 40,  125),  # light magenta
    14: (227, 227, 0  ),  # yellow
    15: (227, 227, 227),  # white
}


def palette_rgb(colour: int) -> tuple:
    """Resolve Ulticap colour index to (R,G,B).
    colour & 0x0F isolates the low nibble (palette index 0-15), stripping any
    flag bits that may be set in higher bits of the colour field (e.g. bit 8
    in symbol polyline group headers).  Safe for direct 0-15 values too:
    c & 0x0F == c for all c in 0..15."""
    return ULTICAP_PALETTE.get(colour & 0x0F, (0, 0, 0))


def ulticap_translate(r_code: int, v: int) -> int:
    """Compute U (displayed alignment) from (R, V).

    Applies the R-dependent 3x3 grid transformation to A=V&31:
      R0,R1 : identity
      R2,R3 : row=2-row, col=2-col  (180° flip)
      R4,R7 : col=2-col             (L<->R mirror)
      R5,R6 : row=2-row             (B<->T mirror)
    Returns U in range 0-8 (H) or 16-24 (V).
    """
    a = v & 31
    is_v = a >= 16
    n = (a - 16) if is_v else a
    row, col = n // 3, n % 3
    r = r_code & 7
    if r in (2, 3):
        row, col = 2 - row, 2 - col
    elif r in (4, 7):
        col = 2 - col
    elif r in (5, 6):
        row = 2 - row
    return (16 if is_v else 0) + row * 3 + col


def is_pwr_symbol(sym) -> bool:
    """True if *sym* is a real KiCad power symbol: exactly one PWR-type pin.

    Such symbols already get correct power_in pin behaviour and graphics
    from the dedicated power-symbol pipeline (the is_pwr branches in
    _comp_sexp / _build_lib_symbols, which resolve the net name from *C/*S
    WIRELABEL or LABEL into the Value property).  They must be excluded
    from the named-stub mechanism — which exists only for pins that would
    NOT otherwise get KiCad's invisible-power_in name-merge, e.g. GND_LINK's
    *S PINTYPE=PAS — and from multi-gate / named-stub-variant lib_symbol
    splitting.  A generic *S WIRELABEL='?' template whose pin is already
    PINTYPE=PWR (e.g. a generic *SPOWER symbol) must flow through the normal power
    pipeline instead, exactly like any other power symbol.

    This is the single source of truth for that distinction, shared by
    kiuc_writer.py and kiuc_viewer.py.
    """
    return sym is not None and len(sym.pins) == 1 and sym.pins[0].pin_type.upper() == 'PWR'


def is_port_sym(sym) -> bool:
    """True when *sym* is a subsheet module-port symbol.

    Two encoding styles:
    - V4.xx: *S has a PORT= attribute in its sym_attrs  (e.g. SPORT_OUT with PORT=OUT)
    - V5.xx: *S has DEVICE= whose value starts with 'PORT_' (e.g. DEVICE=PORT_IN)
      AND the symbol name itself starts with 'PORT_'.
    Both styles produce KiCad (hierarchical_label ...) elements, not regular
    (symbol ...) placements — a hierarchical net connection marker, not a
    real part.

    This is the single source of truth for that distinction, shared by
    kiuc_writer.py and kiuc_viewer.py.
    """
    if sym is None:
        return False
    if sym.attributes.get('PORT') is not None:
        return True
    dev = sym.attributes.get('DEVICE', '')
    if dev.upper().startswith('PORT_') and sym.name.upper().startswith('PORT_'):
        return True
    return False


def u_to_justify(u: int):
    """Map U alignment code to (hjust, vjust, is_vertical, angle_deg).

    U encodes the anchor position within the rendered text box.
    Ulticap and KiCad define top/bottom relative to the screen in the same way
    (anchor at top edge = text hangs below), so no inversion is needed.

    Returns (hjust, vjust, is_vertical, angle_deg).
    """
    is_v = u >= 16
    n = (u - 16) if is_v else u
    col = n % 3    # 0=L, 1=C, 2=R
    row = n // 3   # 0=B, 1=C, 2=T  (Ulticap y-up)

    hjust = ('left', 'center', 'right')[col]
    vjust = ('bottom', 'center', 'top')[row]
    angle = 90 if is_v else 0
    return hjust, vjust, is_v, angle


def tight_bounds(sheet: 'Sheet') -> Optional[Tuple[int, int, int, int]]:
    """Return the true content bounding box of *sheet* as
    (min_x, min_y, max_x, max_y) in Ulticap units.

    Scans every wire endpoint, user-line endpoint, junction, annotation
    anchor, label anchor, and all four corners of every placed component's
    symbol bounding box (after rotation transform).  Visible component
    attribute text anchor positions are also included so that REFDES/VALUE
    labels placed outside the symbol body do not cause clipping.

    Returns None when the sheet contains no scannable objects (empty
    sheet), in which case callers should fall back to sheet.xmin/ymin/
    xmax/ymax.
    """
    xs: List[int] = []
    ys: List[int] = []

    for w in sheet.wires:
        xs += [w.x1, w.x2];  ys += [w.y1, w.y2]

    for ul in sheet.user_lines:
        xs += [ul.x1, ul.x2];  ys += [ul.y1, ul.y2]

    for j in sheet.junctions:
        xs.append(j.x);  ys.append(j.y)

    for a in sheet.annotations:
        xs += [a.x, a.x_poe];  ys += [a.y, a.y_poe]

    for lb in sheet.labels:
        xs.append(lb.x);  ys.append(lb.y)

    for c in sheet.components:
        rot = c.rotation
        sym = sheet.symbols.get(c.symbol_name)

        if sym is not None:
            # All four corners of the symbol bounding box after rotation.
            # Symbol local coords: (0,0) = bottom-left, (width,height) = top-right.
            w, h = sym.width, sym.height
            for dx, dy in ((0, 0), (w, 0), (0, h), (w, h)):
                ox, oy = rot_transform(dx, dy, rot)
                xs.append(c.x + ox);  ys.append(c.y + oy)

            # Visible attribute text anchors (REFDES, VALUE, etc.) — these
            # often extend well beyond the symbol body and are the most
            # common cause of border-clipping on printed output.
            # Use comp_attrs when present (per-instance overrides), fall back
            # to sym_attrs (symbol defaults) for tags not overridden.
            seen_tags: set = set()
            for ca in c.comp_attrs:
                if ca.visibility & 0x7F:   # any visible bits set
                    ox, oy = rot_transform(ca.dx, ca.dy, rot)
                    xs.append(c.x + ox);  ys.append(c.y + oy)
                    seen_tags.add(ca.tag)
            for sa in sym.sym_attrs:
                if sa.tag not in seen_tags and (sa.visibility & 0x7F):
                    ox, oy = rot_transform(sa.dx, sa.dy, rot)
                    xs.append(c.x + ox);  ys.append(c.y + oy)
        else:
            # Unknown symbol — use placement point only
            xs.append(c.x);  ys.append(c.y)

    if not xs:
        return None

    return (min(xs), min(ys), max(xs), max(ys))


def tight_bounds_with_gap(sheet: 'Sheet', gap_units: int) -> Optional[Tuple[int, int, int, int]]:
    """tight_bounds() padded by gap_units on all four sides.

    Shared by kiuc_viewer.py (on-screen sheet-border rectangle, its own
    larger gap for visual comfort) and kiuc_writer.py (paper-size fitting,
    a smaller gap for physical page-fit calculations) — same underlying
    tight-bounds-plus-clearance pattern, different gap value per caller
    since the two gaps answer different questions and are not meant to
    match. Returns None when tight_bounds() itself returns None (empty
    sheet); callers should fall back to sheet.xmin/ymin/xmax/ymax as before.
    """
    tb = tight_bounds(sheet)
    if tb is None:
        return None
    return (tb[0] - gap_units, tb[1] - gap_units,
            tb[2] + gap_units, tb[3] + gap_units)

# ── Ulticap text-box positioning model ──────────────────────────────────────
# Shared by kiuc_viewer.py (on-screen/SVG rendering) and kiuc_writer.py
# (.kicad_sch export) — the single source of truth for these constants.
# Relocated here from kiuc_viewer.py as part of the writer/viewer
# "commonality" work (Step 5): the model itself is a property of the file
# format, not of any one renderer, so it belongs in the shared base module
# both renderers already import from, not duplicated or borrowed across a
# viewer<->writer dependency.
#
# Ulticap does not align text to an anchor point. It positions a virtual
# bounding box relative to the anchor per halign/valign, then always draws
# actual characters LEFT-JUSTIFIED within that box regardless of halign —
# confirmed empirically against real Ulticap files (see pins 5/7/8/9 of a
# real multi-pin symbol during testing). Box width = _UC_LEFT_MARGIN_U +
# sum(per-char advance) +
# _UC_RIGHT_MARGIN_U; margins are a fixed overhead independent of string
# length. Values scale linearly with size_u relative to _UC_REF_SIZE_U.
_UC_REF_SIZE_U          = 35.0   # size_u at which the measurements below were taken
_UC_CHAR_ADVANCE_U      = 20.0   # per-character advance width at _UC_REF_SIZE_U —
                                 # the font is monospaced except for the one
                                 # exception below
_UC_CHAR_ADVANCE_EXCEPTIONS = {'7': 23.0}  # '7' is measurably wider than all
                                 # other characters in Ulticap's DOS font
_UC_LEFT_MARGIN_U       = 3.0    # gap between box left edge and first character's ink
_UC_RIGHT_MARGIN_U      = 18.0   # gap between last character's ink and box right edge —
                                 # fixed regardless of string length (not per-character)
_UC_TOP_MARGIN_U        = 8.0    # gap between box top edge and cap height
_UC_BOTTOM_MARGIN_U     = 3.0    # gap between box bottom edge and baseline
_UC_DESCENDER_U         = 6.0    # descender allowance below the box bottom —
                                 # descriptive only, doesn't participate in
                                 # position math (see _uc_vertical_metrics)
_UC_OVERLINE_GAP_U      = 3.0    # measured gap between the top of overlined text
                                 # (cap height) and the overline decoration itself —
                                 # Ulticap's own fixed convention (ULTICAP_TEXT_MODEL.md
                                 # §3/§4), independent of rendering font, scales with
                                 # size_u the same way the other margins do


def _uc_char_advance(ch: str, size_u: float) -> float:
    """Per-character advance width matching Ulticap's own measured DOS-font
    convention (monospaced except '7'), scaled to the given size_u."""
    base = _UC_CHAR_ADVANCE_EXCEPTIONS.get(ch, _UC_CHAR_ADVANCE_U)
    return base * (size_u / _UC_REF_SIZE_U)


def _uc_box_metrics(text: str, size_u: float):
    """Returns (left_margin, content_width, right_margin, box_width) for a
    string, matching Ulticap's virtual-bounding-box model: margins are a
    fixed overhead independent of string length; content_width is the sum
    of each character's own advance (see _uc_char_advance)."""
    left = _UC_LEFT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    right = _UC_RIGHT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    content_w = sum(_uc_char_advance(ch, size_u) for ch in text)
    return left, content_w, right, left + content_w + right


def _uc_overline_gap(size_u: float) -> float:
    """Measured gap between the top (cap height) of overlined text and the
    overline decoration itself, scaled to size_u — see _UC_OVERLINE_GAP_U
    and ULTICAP_TEXT_MODEL.md §3/§4."""
    return _UC_OVERLINE_GAP_U * (size_u / _UC_REF_SIZE_U)


def _uc_vertical_metrics(size_u: float):
    """Returns (top_margin, bottom_margin) scaled to size_u — the vertical
    counterpart of _uc_box_metrics. Confirmed in ULTICAP_TEXT_MODEL.md §2/§6
    to follow the identical box-then-justify-toward-one-edge rule as
    horizontal: a virtual box is positioned per valign, and content is
    always baseline/bottom-justified within it, offset from the box's
    bottom edge by bottom_margin. Unlike the horizontal case, the box's
    'content' dimension (cap height) is font-dependent by nature — no
    Ulticap-measured, font-independent cap-height number exists to use
    instead — so viewer callers measure it from the real rendering font
    (the same hybrid approach used for the horizontal content-width fix).
    Writer callers, which don't choose or measure a font at all (KiCad
    does its own glyph layout), don't need a content dimension for this
    axis the way the viewer does — see the writer's own vertical-shift
    derivation once implemented.
    _UC_DESCENDER_U is descriptive only (how far descenders may ink below
    the box) and doesn't participate in this position math."""
    top = _UC_TOP_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    bottom = _UC_BOTTOM_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    return top, bottom


def uc_anchor_shift_u(hjust: str, size_u: float) -> float:
    """Local-frame horizontal anchor shift, in Ulticap units, so that a
    KiCad-style direct-anchor (justify hjust) — fed this shifted coordinate
    instead of the raw Ulticap anchor — renders at the same position
    Ulticap's own virtual-box model would put it (ULTICAP_TEXT_MODEL.md
    §2/§5/§7).

    Used by kiuc_writer.py for REFDES/VALUE/property placement. Unlike the
    viewer's hybrid-width fix, this needs no font metrics at all: KiCad
    performs its own glyph layout using whatever font the user has
    configured, so only the ANCHOR COORDINATE needs correcting, using
    Ulticap's fixed, font-independent margins alone. For 'center', content
    width cancels out of the box-model formula entirely (left_margin and
    right_margin are both independent of string length — see
    _uc_box_metrics), making this a pure per-size constant, not dependent
    on the actual text at all.

    Only the horizontal axis is handled here — see the writer's own
    vertical-shift work for the vertical counterpart, and note this only
    applies to locally-horizontal text; callers should skip locally-
    vertical (is_v) text for now, pending separate verification."""
    left = _UC_LEFT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    right = _UC_RIGHT_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    if hjust == 'left':
        return left
    elif hjust == 'right':
        return -right
    else:
        return (left - right) / 2.0


def uc_anchor_shift_v(vjust: str, size_u: float) -> float:
    """Local-frame vertical anchor shift (Ulticap units) — the vertical
    counterpart of uc_anchor_shift_u.

    Derivation mirrors the horizontal case exactly, but rests on one
    assumption that horizontal didn't need: that KiCad's own vertical
    justify keywords are self-referential the same structural way
    Ulticap's box model is — 'bottom' anchors KiCad's own rendered
    baseline, 'top' anchors KiCad's own rendered cap-height top, 'center'
    anchors the midpoint between them. Under that assumption, the box's
    content dimension (cap height) cancels out of all three formulas
    exactly the way content_width cancels for horizontal 'center' — so,
    like uc_anchor_shift_u, this needs no font metrics, only the fixed
    top/bottom margins.

    This assumption is UNVERIFIED against KiCad's actual behaviour (unlike
    the horizontal ink-edge convention, which is unambiguous across text
    systems). If KiCad's vertical justify instead references full
    ascent/descent rather than cap-height/baseline, these three shifts
    will need revisiting once tested live."""
    top = _UC_TOP_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    bottom = _UC_BOTTOM_MARGIN_U * (size_u / _UC_REF_SIZE_U)
    if vjust == 'bottom':
        return bottom
    elif vjust == 'top':
        return -top
    else:
        return (bottom - top) / 2.0
