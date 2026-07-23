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
kiuc_writer.py — Write Ulticap schematics as KiCad 9 .kicad_sch files.

Design goals:
  - Fully self-contained output: all symbol geometry embedded, no library references.
  - *C data takes precedence over *S template data for each component instance.
  - *STITLE / *STITLE_REV are rendered as ordinary placed symbols at their Ulticap
    coordinates; their pre-defined fields (PROJECT, REV, DATE, COMPANY, …) also
    populate the KiCad (title_block …) record for BOM/print-header integration.
  - An empty .kicad_wks is written alongside each project so KiCad's built-in
    title block is suppressed and never overlaps the rendered Ulticap title symbol.
  - Paper size is chosen from the tight coordinate scan (tight_bounds) so it fits
    the actual content rather than the oversized Ulticap canvas bounds.
  - Content is centred on the full paper area.
  - Placement offset is snapped to the Ulticap grid so all coords stay on-grid.
  - Overline text already converted upstream (kiuc_model.convert_overline).

Coordinate conventions:
  Ulticap: 1 unit = 2 mils = 0.0508 mm, origin = arbitrary, Y+ = up
  KiCad:   mm, origin = top-left corner of paper, Y+ = down

  Sheet bounds (xlo, ylo, xhi, yhi) define the scrollable canvas; tight_bounds
  scans all placed objects to find the true content extents, which are used for
  paper selection and centering.  The Ulticap paper size is not stored in the
  SCH file and is not needed for conversion.

  Transform (all arithmetic in integer Ulticap units, one multiply at output):
    ox_u, oy_u = grid-snapped margins (integer multiples of sheet.grid)
    kx(u) = (u  - xlo + ox_u) * MM_PER_UNIT
    ky(u) = (yhi - u  + oy_u) * MM_PER_UNIT   (Y-flip)

  Virtual Reference Point (VRP) — where Ulticap displays coordinate (0, 0):
    vrp_x = -xlo - (425 - grid)   [Ulticap units]
    vrp_y = -ylo - (425 - grid)
  The VRP is calculated and stored on the sheet but not used for placement.
"""
from __future__ import annotations

import math
import json as _json
import uuid as _uuid_mod
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from kiuc_model import (
    Sheet, Schematic, Symbol, SymbolPin, Component, Wire, Junction,
    Annotation, Label, Polyline, Circle, UserLine,
    MM_PER_UNIT, MM_PER_MIL, PINTYPE_TAG_MAP,
    UC_POLYLINE_STYLE_BY_CODE, UC_ARC_STYLE_BY_CODE,
    rot_transform as _rot_transform,
    pin_conn_point as _pin_conn_point,
    tight_bounds_with_gap as _tight_bounds_with_gap,
    palette_rgb, ulticap_translate, is_pwr_symbol, is_port_sym, u_to_justify,
    uc_anchor_shift_u, uc_anchor_shift_v, _uc_vertical_metrics, _uc_box_metrics,
)


# ══════════════════════════════════════════════════════════════════════════════
# ULTICAP → KICAD FORMAT-DECODE TABLES
# ══════════════════════════════════════════════════════════════════════════════
# Everything in this block maps an Ulticap-format code, tag, or value to its
# KiCad equivalent. Grouped here (rather than scattered next to first use) so
# a future change — a newly discovered Ulticap version quirk, or eventually
# the binary UTSCH format, which exposes more features than ASCII SCH/BLK —
# only requires browsing one place instead of the whole file.
#
# Local to this file: none of these are referenced from kiuc_viewer.py or
# elsewhere (unlike the shared tables in kiuc_model.py, e.g. UC_POLYLINE_
# STYLE_BY_CODE, which are genuinely used by more than one file). Not exposed
# via TUNING_SPEC/GUI/kiuc.ini either — these are fixed format facts, not
# user-tunable rendering preferences.

# Ulticap pin_format -> KiCad pin shape (the graphical marker on the stub)
#
# IEC 60617 shapes (codes 3-8) are GRAPHICAL NOTATION CONVENTIONS only.
# They carry no active-low semantic — that depends entirely on the logic
# convention used in the schematic.  KiCad misnames its IEC tokens
# (input_low, output_low, clock_low, falling_edge_clock) implying an
# active-low assertion that IEC notation does not make.  To avoid injecting
# false semantics into the converted schematic we fall back to the equivalent
# ANSI shapes, which are semantically neutral:
#
#   IEC IN  (3)             → plain pin            ('line')
#   IEC OUT (4)             → plain pin            ('line')
#   IEC INVERTED IN  (5)    → bubble               ('inverted')
#   IEC INVERTED OUT (6)    → bubble               ('inverted')
#   IEC INVERTED CLOCK (7)  → inverted clock       ('inverted_clock')  [unambiguous]
#   IEC BIDIRECTIONAL (8)   → plain pin            ('line')  [no KiCad equivalent]
#   BUS (9)                 → plain pin            ('line')  [no KiCad equivalent]
#
# Codes 3/4 lose their IEC active-indicator mark entirely: this is a KiCad
# representational gap, not a conversion error.
_PIN_FORMAT_SHAPE: Dict[int, str] = {
    0:  'line',              # NONE
    1:  'inverted',          # INVERTED (bubble)
    2:  'clock',             # CLOCK (chevron)
    3:  'line',              # IEC IN       → ANSI plain (see note above)
    4:  'line',              # IEC OUT      → ANSI plain (see note above)
    5:  'inverted',          # IEC INVERTED IN  → ANSI bubble
    6:  'inverted',          # IEC INVERTED OUT → ANSI bubble
    7:  'inverted_clock',    # IEC INVERTED CLOCK → inverted clock (unambiguous)
    8:  'line',              # IEC BIDIRECTIONAL → plain (no KiCad equivalent)
    9:  'line',              # BUS               → plain (no KiCad equivalent)
    10: 'inverted_clock',    # CLOCK INVERTED
}

# Ulticap PORT= value → KiCad hierarchical_label shape
# V4 PORT= values seen: 'IN', 'OUT', 'BI', 'PAS', 'pas' (case-insensitive).
# V5 uses DEVICE= (e.g. 'PORT_IN', 'PORT_BUS_OUT') — strip 'PORT_BUS_' / 'PORT_' prefix.
# KiCad shapes: input, output, bidirectional, passive (tri_state unused by Ulticap).
_PORT_SHAPE: Dict[str, str] = {
    'IN':  'input',
    'OUT': 'output',
    'BI':  'bidirectional',
    'PAS': 'passive',
}

# KiCad hierarchical_label angle from Ulticap component rotation.
# Ulticap port pin always has pin_rotation=0 (EAST stub).
# After applying comp.rotation the stub direction maps to a KiCad angle:
#   rot 0,6 → EAST  → 0°    rot 1,5 → NORTH → 90°
#   rot 2,4 → WEST  → 180°  rot 3,7 → SOUTH → 270°
_HIER_LABEL_ANGLE: Dict[int, int] = {
    0: 0, 1: 90, 2: 180, 3: 270,
    4: 180, 5: 90, 6: 0, 7: 270,
}

# KiCad hierarchical_label justify from angle.
# The anchor (at X Y) is always the wire-tip end of the stub.
# The label shape body and text extend INWARD (away from the wire).
# Justify describes where the anchor sits relative to the text:
#   angle=0   (stub right, body left):  anchor right of text → 'right'  [verified]
#   angle=90  (stub up,    body down):  anchor above text    → 'left'   [verified]
#   angle=180 (stub left,  body right): anchor left of text  → 'left'   [verified]
#   angle=270 (stub down,  body up):    anchor below text    → 'right'  [verified]
_HIER_LABEL_JUSTIFY: Dict[int, str] = {
    0: 'right', 90: 'left', 180: 'left', 270: 'right',
}

# Mapping from Ulticap pre-defined tag names (uppercase) to KiCad title block
# field keys.  Only the four fields that have a direct named equivalent in KiCad
# are mapped; all others (ADDRESS, CITY, COUNTRY, ENG, DATE, …) remain as plain
# placed text in the symbol — Ulticap allows custom field names so there is too
# much variation to reliably map them to KiCad comment slots.
# TITLE_REV is intentionally excluded: its fields may carry different revision-
# specific data and must not overwrite the primary TITLE values.
_TITLE_FIELD_MAP: Dict[str, str] = {
    'PROJECT':      'title',
    'REV':          'rev',
    'INITIAL_DATE': 'date',
    'COMPANY':      'company',
}

# Mapping from Ulticap pre-defined tag names (uppercase) to KiCad text variables,
# applied only when rendering the TITLE component.  Only the four named KiCad
# fields plus PAGE/OF (sheet-level variables) are substituted; every other field
# keeps its literal Ulticap value as plain text in the symbol.
# TITLE_REV attributes always use their literal values (plain text).
_TITLE_VAR_MAP: Dict[str, str] = {
    'PROJECT':      '${TITLE}',
    'REV':          '${REVISION}',
    'INITIAL_DATE': '${ISSUE_DATE}',
    'COMPANY':      '${COMPANY}',
    'PAGE':         '${#}',
    'OF':           '${##}',
}


# ── .kicad_pro project file ─────────────────────────────────────────────────
# Verbatim JSON of a project freshly created and saved (schematic + PCB editors
# both opened and saved once) by KiCad 9.0.7, used as the literal template
# when no .kicad_pro exists yet for a given output project. Captures every
# top-level section (board design rules, net classes, pcbnew paths, etc.)
# exactly as KiCad itself writes them, so a from-scratch project file opens
# cleanly with no missing-key surprises. Only meta.filename, erc.rule_severities,
# and schematic.connection_grid_size are overridden per-project at write time;
# everything else (including the empty top-level "sheets": [] — confirmed
# empirically to remain valid and self-correcting; KiCad regenerates sheet
# bookkeeping from the .kicad_sch files themselves on next save, even when
# the array starts empty or stale) is passed through unchanged.
_KICAD_PRO_TEMPLATE_JSON = '{"board":{"3dviewports":[],"design_settings":{"defaults":{"apply_defaults_to_fp_fields":false,"apply_defaults_to_fp_shapes":false,"apply_defaults_to_fp_text":false,"board_outline_line_width":0.05,"copper_line_width":0.2,"copper_text_italic":false,"copper_text_size_h":1.5,"copper_text_size_v":1.5,"copper_text_thickness":0.3,"copper_text_upright":false,"courtyard_line_width":0.05,"dimension_precision":4,"dimension_units":3,"dimensions":{"arrow_length":1270000,"extension_offset":500000,"keep_text_aligned":true,"suppress_zeroes":true,"text_position":0,"units_format":0},"fab_line_width":0.1,"fab_text_italic":false,"fab_text_size_h":1.0,"fab_text_size_v":1.0,"fab_text_thickness":0.15,"fab_text_upright":false,"other_line_width":0.1,"other_text_italic":false,"other_text_size_h":1.0,"other_text_size_v":1.0,"other_text_thickness":0.15,"other_text_upright":false,"pads":{"drill":0.8,"height":1.27,"width":2.54},"silk_line_width":0.1,"silk_text_italic":false,"silk_text_size_h":1.0,"silk_text_size_v":1.0,"silk_text_thickness":0.1,"silk_text_upright":false,"zones":{"min_clearance":0.5}},"diff_pair_dimensions":[],"drc_exclusions":[],"meta":{"version":2},"rule_severities":{"annular_width":"error","clearance":"error","connection_width":"warning","copper_edge_clearance":"error","copper_sliver":"warning","courtyards_overlap":"error","creepage":"error","diff_pair_gap_out_of_range":"error","diff_pair_uncoupled_length_too_long":"error","drill_out_of_range":"error","duplicate_footprints":"warning","extra_footprint":"warning","footprint":"error","footprint_filters_mismatch":"ignore","footprint_symbol_mismatch":"warning","footprint_type_mismatch":"ignore","hole_clearance":"error","hole_to_hole":"warning","holes_co_located":"warning","invalid_outline":"error","isolated_copper":"warning","item_on_disabled_layer":"error","items_not_allowed":"error","length_out_of_range":"error","lib_footprint_issues":"warning","lib_footprint_mismatch":"warning","malformed_courtyard":"error","microvia_drill_out_of_range":"error","mirrored_text_on_front_layer":"warning","missing_courtyard":"ignore","missing_footprint":"warning","net_conflict":"warning","nonmirrored_text_on_back_layer":"warning","npth_inside_courtyard":"ignore","padstack":"warning","pth_inside_courtyard":"ignore","shorting_items":"error","silk_edge_clearance":"warning","silk_over_copper":"warning","silk_overlap":"warning","skew_out_of_range":"error","solder_mask_bridge":"error","starved_thermal":"error","text_height":"warning","text_on_edge_cuts":"error","text_thickness":"warning","through_hole_pad_without_hole":"error","too_many_vias":"error","track_angle":"error","track_dangling":"warning","track_segment_length":"error","track_width":"error","tracks_crossing":"error","unconnected_items":"error","unresolved_variable":"error","via_dangling":"warning","zones_intersect":"error"},"rules":{"max_error":0.005,"min_clearance":0.0,"min_connection":0.0,"min_copper_edge_clearance":0.5,"min_groove_width":0.0,"min_hole_clearance":0.25,"min_hole_to_hole":0.25,"min_microvia_diameter":0.2,"min_microvia_drill":0.1,"min_resolved_spokes":2,"min_silk_clearance":0.0,"min_text_height":0.8,"min_text_thickness":0.08,"min_through_hole_diameter":0.3,"min_track_width":0.0,"min_via_annular_width":0.1,"min_via_diameter":0.5,"solder_mask_to_copper_clearance":0.0,"use_height_for_length_calcs":true},"teardrop_options":[{"td_onpthpad":true,"td_onroundshapesonly":false,"td_onsmdpad":true,"td_ontrackend":false,"td_onvia":true}],"teardrop_parameters":[{"td_allow_use_two_tracks":true,"td_curve_segcount":0,"td_height_ratio":1.0,"td_length_ratio":0.5,"td_maxheight":2.0,"td_maxlen":1.0,"td_on_pad_in_zone":false,"td_target_name":"td_round_shape","td_width_to_size_filter_ratio":0.9},{"td_allow_use_two_tracks":true,"td_curve_segcount":0,"td_height_ratio":1.0,"td_length_ratio":0.5,"td_maxheight":2.0,"td_maxlen":1.0,"td_on_pad_in_zone":false,"td_target_name":"td_rect_shape","td_width_to_size_filter_ratio":0.9},{"td_allow_use_two_tracks":true,"td_curve_segcount":0,"td_height_ratio":1.0,"td_length_ratio":0.5,"td_maxheight":2.0,"td_maxlen":1.0,"td_on_pad_in_zone":false,"td_target_name":"td_track_end","td_width_to_size_filter_ratio":0.9}],"track_widths":[],"tuning_pattern_settings":{"diff_pair_defaults":{"corner_radius_percentage":80,"corner_style":1,"max_amplitude":1.0,"min_amplitude":0.2,"single_sided":false,"spacing":1.0},"diff_pair_skew_defaults":{"corner_radius_percentage":80,"corner_style":1,"max_amplitude":1.0,"min_amplitude":0.2,"single_sided":false,"spacing":0.6},"single_track_defaults":{"corner_radius_percentage":80,"corner_style":1,"max_amplitude":1.0,"min_amplitude":0.2,"single_sided":false,"spacing":0.6}},"via_dimensions":[],"zones_allow_external_fillets":false},"ipc2581":{"dist":"","distpn":"","internal_id":"","mfg":"","mpn":""},"layer_pairs":[],"layer_presets":[],"viewports":[]},"boards":[],"cvpcb":{"equivalence_files":[]},"erc":{"erc_exclusions":[],"meta":{"version":0},"pin_map":[[0,0,0,0,0,0,1,0,0,0,0,2],[0,2,0,1,0,0,1,0,2,2,2,2],[0,0,0,0,0,0,1,0,1,0,1,2],[0,1,0,0,0,0,1,1,2,1,1,2],[0,0,0,0,0,0,1,0,0,0,0,2],[0,0,0,0,0,0,0,0,0,0,0,2],[1,1,1,1,1,0,1,1,1,1,1,2],[0,0,0,1,0,0,1,0,0,0,0,2],[0,2,1,2,0,0,1,0,2,2,2,2],[0,2,0,1,0,0,1,0,2,0,0,2],[0,2,1,1,0,0,1,0,2,0,0,2],[2,2,2,2,2,2,2,2,2,2,2,2]],"rule_severities":{"bus_definition_conflict":"error","bus_entry_needed":"error","bus_to_bus_conflict":"error","bus_to_net_conflict":"error","different_unit_footprint":"error","different_unit_net":"error","duplicate_reference":"error","duplicate_sheet_names":"error","endpoint_off_grid":"warning","extra_units":"error","footprint_filter":"ignore","footprint_link_issues":"warning","four_way_junction":"ignore","global_label_dangling":"warning","hier_label_mismatch":"error","label_dangling":"error","label_multiple_wires":"warning","lib_symbol_issues":"warning","lib_symbol_mismatch":"warning","missing_bidi_pin":"warning","missing_input_pin":"warning","missing_power_pin":"error","missing_unit":"warning","multiple_net_names":"warning","net_not_bus_member":"warning","no_connect_connected":"warning","no_connect_dangling":"warning","pin_not_connected":"error","pin_not_driven":"error","pin_to_pin":"warning","power_pin_not_driven":"error","same_local_global_label":"warning","similar_label_and_power":"warning","similar_labels":"warning","similar_power":"warning","simulation_model_issue":"ignore","single_global_label":"ignore","unannotated":"error","unconnected_wire_endpoint":"warning","undefined_netclass":"error","unit_value_mismatch":"error","unresolved_variable":"error","wire_dangling":"error"}},"libraries":{"pinned_footprint_libs":[],"pinned_symbol_libs":[]},"meta":{"filename":"BLANK.kicad_pro","version":3},"net_settings":{"classes":[{"bus_width":12,"clearance":0.2,"diff_pair_gap":0.25,"diff_pair_via_gap":0.25,"diff_pair_width":0.2,"line_style":0,"microvia_diameter":0.3,"microvia_drill":0.1,"name":"Default","pcb_color":"rgba(0, 0, 0, 0.000)","priority":2147483647,"schematic_color":"rgba(0, 0, 0, 0.000)","track_width":0.2,"via_diameter":0.6,"via_drill":0.3,"wire_width":6}],"meta":{"version":4},"net_colors":null,"netclass_assignments":null,"netclass_patterns":[]},"pcbnew":{"last_paths":{"gencad":"","idf":"","netlist":"","plot":"","pos_files":"","specctra_dsn":"","step":"","svg":"","vrml":""},"page_layout_descr_file":""},"schematic":{"annotate_start_num":0,"bom_export_filename":"${PROJECTNAME}.csv","bom_fmt_presets":[],"bom_fmt_settings":{"field_delimiter":",","keep_line_breaks":false,"keep_tabs":false,"name":"CSV","ref_delimiter":",","ref_range_delimiter":"","string_delimiter":"\\""},"bom_presets":[],"bom_settings":{"exclude_dnp":false,"fields_ordered":[{"group_by":false,"label":"Reference","name":"Reference","show":true},{"group_by":false,"label":"Qty","name":"${QUANTITY}","show":true},{"group_by":true,"label":"Value","name":"Value","show":true},{"group_by":true,"label":"DNP","name":"${DNP}","show":true},{"group_by":true,"label":"Exclude from BOM","name":"${EXCLUDE_FROM_BOM}","show":true},{"group_by":true,"label":"Exclude from Board","name":"${EXCLUDE_FROM_BOARD}","show":true},{"group_by":true,"label":"Footprint","name":"Footprint","show":true},{"group_by":false,"label":"Datasheet","name":"Datasheet","show":true}],"filter_string":"","group_symbols":true,"include_excluded_from_bom":true,"name":"Default Editing","sort_asc":true,"sort_field":"Reference"},"connection_grid_size":50.0,"drawing":{"dashed_lines_dash_length_ratio":12.0,"dashed_lines_gap_length_ratio":3.0,"default_line_thickness":6.0,"default_text_size":50.0,"field_names":[],"intersheets_ref_own_page":false,"intersheets_ref_prefix":"","intersheets_ref_short":false,"intersheets_ref_show":false,"intersheets_ref_suffix":"","junction_size_choice":3,"label_size_ratio":0.375,"operating_point_overlay_i_precision":3,"operating_point_overlay_i_range":"~A","operating_point_overlay_v_precision":3,"operating_point_overlay_v_range":"~V","overbar_offset_ratio":1.23,"pin_symbol_size":25.0,"text_offset_ratio":0.15},"legacy_lib_dir":"","legacy_lib_list":[],"meta":{"version":1},"net_format_name":"","page_layout_descr_file":"","plot_directory":"","space_save_all_events":true,"spice_current_sheet_as_root":false,"spice_external_command":"spice \\"%I\\"","spice_model_current_sheet_as_root":true,"spice_save_all_currents":false,"spice_save_all_dissipations":false,"spice_save_all_voltages":false,"subpart_first_id":65,"subpart_id_separator":0},"sheets":[],"text_variables":{}}'


# ── paper sizes ────────────────────────────────────────────────────────────────
# (name, width_mm, height_mm) — landscape orientation, sorted smallest first.
# Only standard sizes A5..A0 are considered as candidates (KIUC intentionally
# does not use Letter/Legal/Tabloid).
_PAPER_SIZES: List[Tuple[str, float, float]] = [
    ('A5',      210.0,  148.0),
    ('A4',      297.0,  210.0),
    ('A3',      420.0,  297.0),
    ('A2',      594.0,  420.0),
    ('A1',      841.0,  594.0),
    ('A0',     1189.0,  841.0),
]

# KiCad reserves a fixed 12.7mm border on all four sides of every sheet,
# regardless of paper size, as the boundary of the actual usable drawing
# area (measured empirically in KiCad 9.0.7 across multiple paper sizes).
_KICAD_BORDER_MM = 12.7

# Tolerance applied when checking whether content fits a standard paper
# size's usable area (paper minus the 12.7mm KiCad border on each side).
PAPER_FIT_EPSILON_MM = 0.5

# Maximum slack (mm, per axis) allowed between content and a standard
# paper's usable area before falling back to a custom (paper "User" W H)
# size instead of the oversized standard one. Not wired into TUNING_SPEC/
# the GUI/kiuc.ini -- this is an internal layout-fit constant, not a
# rendering value a user would tune by eye.
CUSTOM_SIZE_RANGE_MM = 50.0


def _nearest_paper(content_w_mm: float,
                    content_h_mm: float) -> Tuple[Optional[str], float, float]:
    """Choose a paper size for the given content extents.

    content_w_mm, content_h_mm: tight schematic bounding-box dimensions
    (already including the _TB_GAP margin applied in _build_sch). The
    Ulticap title block is rendered as a plain symbol and included in the
    tight bounds, so no additional height reservation is needed here.

    Returns (name, paper_w_mm, paper_h_mm):
      - name is a standard size string (e.g. 'A4') when a standard sheet
        is used, or None when a custom (paper "User" W H) size is needed.
      - paper_w_mm/paper_h_mm are the full physical paper dimensions in
        either case (a standard size's fixed dimensions, or the computed
        custom dimensions).

    Selection rules, in order:
      1. Correctness: a standard size only qualifies if content fits its
         *usable* area (paper minus the 12.7mm KiCad border on each side),
         not its raw physical dimensions -- otherwise content could crowd
         or overlap KiCad's border/frame.
      2. Underflow floor: A5 is always the minimum -- never fall back to
         a custom size smaller than A5, even if that leaves a lot of
         slack for a very small schematic.
      3. Efficiency: of the qualifying standard sizes, if the smallest one
         leaves more than CUSTOM_SIZE_RANGE_MM of slack on either axis,
         use a custom size sized to the content instead of the oversized
         standard one.
      4. Overflow: content too large even for A0's usable area falls back
         to a custom size sized to the content.
    """
    for name, pw, ph in _PAPER_SIZES:
        usable_w = pw - 2 * _KICAD_BORDER_MM
        usable_h = ph - 2 * _KICAD_BORDER_MM
        fits = (content_w_mm <= usable_w + PAPER_FIT_EPSILON_MM and
                content_h_mm <= usable_h + PAPER_FIT_EPSILON_MM)
        if not fits:
            continue
        if name == 'A5':
            return name, pw, ph   # underflow floor -- never go custom below A5
        slack_w = usable_w - content_w_mm
        slack_h = usable_h - content_h_mm
        if slack_w > CUSTOM_SIZE_RANGE_MM or slack_h > CUSTOM_SIZE_RANGE_MM:
            break   # too much waste -- fall through to custom sizing below
        return name, pw, ph

    # Custom size: content plus the KiCad border on each side, rounded up
    # to the nearest whole mm so the physical page is never smaller than
    # required due to floating-point rounding.
    custom_w = math.ceil(content_w_mm + 2 * _KICAD_BORDER_MM)
    custom_h = math.ceil(content_h_mm + 2 * _KICAD_BORDER_MM)
    return None, float(custom_w), float(custom_h)


# ── coordinate transform ───────────────────────────────────────────────────────

class _Transform:
    """Convert Ulticap sheet coordinates → KiCad mm.

    All margin arithmetic is performed in integer Ulticap units and snapped
    to the sheet grid before the single final multiply by MM_PER_UNIT.
    This guarantees that every schematic coordinate lands on an exact
    grid-aligned mm value with no floating-point accumulation.

    Sheet bounds (xhi, yhi) are not guaranteed to be grid multiples, so
    content dimensions are ceiled to the next grid boundary.  This makes
    the Y-flip reference (ylo + content_h_snap) always on-grid without
    displacing any existing content.  For already-aligned bounds the ceil
    is a no-op (delta = 0).
    """

    def __init__(self, sheet: Sheet, paper_w: float, paper_h: float,
                 xmin: int = None, ymin: int = None,
                 xmax: int = None, ymax: int = None):
        import math
        grid  = sheet.grid or 25   # default to 25 if not set
        xlo   = xmin if xmin is not None else sheet.xmin
        ylo   = ymin if ymin is not None else sheet.ymin
        _xmax = xmax if xmax is not None else sheet.xmax
        _ymax = ymax if ymax is not None else sheet.ymax

        # Snap content dimensions up to the nearest grid multiple so that
        # the flip reference (ylo + content_h_snap) is always on-grid.
        content_w_snap = math.ceil((_xmax - xlo) / grid) * grid
        content_h_snap = math.ceil((_ymax - ylo) / grid) * grid
        y_ref = ylo + content_h_snap   # on-grid: ylo is grid-aligned, snap is too

        # Ideal centred margins (may be fractional); snap to grid → exact integer.
        # Content is centred on the full paper — no title block exclusion zone.
        ox_u = round((paper_w / MM_PER_UNIT - content_w_snap) / 2 / grid) * grid
        oy_u = round((paper_h / MM_PER_UNIT - content_h_snap) / 2 / grid) * grid

        self._xlo  = xlo
        self._yref = y_ref
        self._ox_u = ox_u
        self._oy_u = oy_u

    def x(self, u: int) -> float:
        return round((u - self._xlo + self._ox_u) * MM_PER_UNIT, 4)

    def y(self, u: int) -> float:
        return round((self._yref - u + self._oy_u) * MM_PER_UNIT, 4)

    def d(self, u: int) -> float:
        """Distance/length only (no offset, no Y-flip)."""
        return round(abs(u) * MM_PER_UNIT, 4)

    def xy(self, ux: int, uy: int) -> str:
        return f'(xy {_f(self.x(ux))} {_f(self.y(uy))})'


def vrp(sheet: Sheet) -> Tuple[int, int]:
    """Return the Virtual Reference Point for a sheet in Ulticap units.

    The VRP is the schematic coordinate (0, 0) as displayed by Ulticap.
    Ulticap places a fixed border margin of (425 - grid) units between the
    stored sheet edge (xmin/ymin) and the display origin:

        vrp_x = -xmin - (425 - grid)
        vrp_y = -ymin - (425 - grid)

    Verified against empirical measurements for grid=25 (offset=400) and
    grid=50 (offset=375).  Not used for KiCad placement; retained for
    diagnostics and future origin-preserving export modes.
    """
    g = sheet.grid or 25
    offset = 425 - g
    return (-sheet.xmin - offset, -sheet.ymin - offset)


# ── formatting helpers ─────────────────────────────────────────────────────────

def _f(v: float) -> str:
    """Format float: strip trailing zeros, keep at least one decimal."""
    s = f'{v:.4f}'.rstrip('0')
    return s if '.' in s and s[-1] != '.' else s.rstrip('.') + '.0' if '.' not in s else s + '0'

def _esc(s: str) -> str:
    return str(s).replace('\\', '\\\\').replace('"', '\\"')


def _build_lib_name_map(symbol_names) -> dict:
    """Map original Ulticap *S symbol names to KiCad-safe LIB_ID names.

    KiCad's LIB_ID grammar treats backslash, '/' and ':' as structural
    separators (LIBNICKNAME:SYMBOLNAME). A name containing any of these --
    e.g. the Ulticap part name '74LS151\\SO' -- is rejected by KiCad's
    parser even when the quoted string is correctly escaped, since the
    rejection happens at the LIB_ID-structure level, not the
    string-escaping level.

    This replaces each offending character with '_' and disambiguates any
    resulting collisions (two distinct original names that sanitize to the
    same string) by appending a numeric suffix.

    Returns a dict mapping every name in symbol_names to its KiCad-safe form.
    Names containing none of the offending characters map to themselves.
    """
    seen = {}     # sanitized -> original (first owner)
    mapping = {}  # original -> sanitized
    for orig in symbol_names:
        safe = orig.replace('\\', '_').replace('/', '_').replace(':', '_')
        if safe == orig:
            mapping[orig] = orig
            seen.setdefault(safe, orig)
            continue
        candidate = safe
        n = 2
        while candidate in seen and seen[candidate] != orig:
            candidate = f'{safe}_{n}'
            n += 1
        seen[candidate] = orig
        mapping[orig] = candidate
    return mapping

_uid_counter = 0

def _uid() -> str:
    global _uid_counter
    _uid_counter += 1
    return str(_uuid_mod.UUID(int=_uid_counter))


# ── pin length calculation ─────────────────────────────────────────────────────


def _pin_length(pin: SymbolPin, sym: Symbol) -> float:
    """Pin length = distance from connection point to stub end, in Ulticap units."""
    r = pin.pin_rotation & 3
    if r == 0:  return max(sym.width  - pin.x, 0)
    if r == 1:  return max(sym.height - pin.y, 0)
    if r == 2:  return max(pin.x,              0)
    return          max(pin.y,              0)


# Ulticap pin_format / port shape / hierarchical-label angle & justify —
# see the format-decode table block near the top of this file.


def _resolve_pins(sym: Symbol, comp: Component) -> List[Tuple[SymbolPin, str, str, str]]:
    """
    For each pin in sym.pins, resolve (pin, pin_number, pin_name, pin_type).

    Pin number, name, and type are already stamped onto each SymbolPin by
    _parse_symbol_attrs using POE-coordinate matching (order-independent).
    These stamped values are the *S defaults.

    *C comp_attrs with '#' tags override the number/name/type for specific
    pins.  Matching is done in Ulticap unit space: a *C attr's (dx_poe,
    dy_poe) is compared directly against each pin's connection point
    coordinates from _pin_conn_point — both are in the same *S local
    coordinate system, so no rotation, mm conversion, or tolerance is needed.
    An exact integer match is the correct and only valid condition.  A *C
    attr with no exact match is stale/corrupt data and is silently discarded.

    When a matched *C '#' attr has an empty value ('#='), the *S-stamped
    pin.number is used instead (multi-gate shared supply pin convention).
    """
    # Build *C lookup dicts keyed by (dx_poe, dy_poe) in Ulticap units
    c_num_by_poe:  dict = {}
    c_type_by_poe: dict = {}
    c_name_by_poe: dict = {}
    for ca in comp.comp_attrs:
        key = (ca.dx_poe, ca.dy_poe)
        if ca.tag.startswith('#'):
            c_num_by_poe[key] = ca.value
        elif ca.tag == 'PINTYPE':
            c_type_by_poe[key] = ca.value
        elif ca.tag in ('LABEL', 'WIRELABEL'):
            # WIRELABEL overrides the pin name just like LABEL does.
            # Each component gets its own embedded lib symbol (keyed by RefDes),
            # so instance-specific WIRELABEL values are safe here — LNK1=CGND
            # and LNK2=GND produce separate GND_LINK_LNK1 / GND_LINK_LNK2 entries.
            c_name_by_poe[key] = ca.value

    # *S WIRELABEL default == '?' marks a "named-stub" pin (e.g. GND_LINK):
    # a placeholder pin whose real net name is supplied per-instance via *C
    # WIRELABEL.  Such pins are rendered HIDDEN power_in at the real
    # *S-derived POE (unlike SIGNAL= pins, which are also off-grid) so that
    # KiCad's documented invisible-power_in name-merge joins the name
    # supplied by *C into the broader same-named net, while a real wire can
    # still be drawn to the pin's exact location — KiCad permits drawing a
    # wire directly to a hidden pin. A separate cosmetic text label (added
    # in _lib_sym_pins) shows the name, since the hidden pin's own name text
    # is no longer visible.
    #
    # Excluded when sym is already a real power symbol (is_pwr_symbol):
    # a PWR-type pin already resolves to power_in via PINTYPE_TAG_MAP below
    # and already gets its net name from the dedicated power-symbol Value
    # resolution, so the named-stub override would be redundant at best —
    # and, for a generic WIRELABEL='?' power-flag template (e.g. *SPOWER),
    # would wrongly hide the pin and swap in GND_LINK's cosmetic
    # treatment on top of an already-correct real power symbol.
    _named_stub_poes = set() if is_pwr_symbol(sym) else {
        (sa.dx_poe, sa.dy_poe)
        for sa in sym.sym_attrs
        if sa.tag == 'WIRELABEL' and sa.default_value == '?'
    }

    result = []
    for pin in sym.pins:
        # Pin connection point in *S local coords — same space as *C dx_poe/dy_poe
        poe_x, poe_y = _pin_conn_point(pin, sym)
        key = (poe_x, poe_y)

        if key in c_num_by_poe or key in c_name_by_poe or key in c_type_by_poe:
            # *C override found: use *C values, falling back to *S for empties
            raw_num = c_num_by_poe.get(key, '')
            number  = raw_num if raw_num else pin.number
            pt      = c_type_by_poe.get(key, pin.pin_type)
            name    = c_name_by_poe.get(key, pin.name)
        else:
            # No *C override: use *S-stamped values directly
            number = pin.number if pin.number else str(len(result) + 1)
            pt     = pin.pin_type
            name   = pin.name

        if key in _named_stub_poes and key in c_name_by_poe:
            ktype = 'power_in'
        else:
            ktype = PINTYPE_TAG_MAP.get(pt.upper(), 'passive')
        result.append((pin, number, name, ktype))
    return result


def _synth_gate_pin_data(sym: Symbol, unit_num: int,
                          total_units: int) -> List[Tuple[SymbolPin, str, str, str]]:
    """Build (pin, number, name, ktype) tuples for a multi-gate symbol's
    unit_num-th unit (1-based) with no placed *C instance to resolve from.

    A design may legitimately place only some gates of a multi-gate IC and
    leave the rest unused (e.g. one gate of a dual 74LS139) -- but the
    library symbol must still declare every unit its *S PARTS attribute
    promises, or KiCad silently treats the whole part as single-unit and
    drops the gate letter from every placed instance's Reference, including
    the ones that ARE placed. For units with no instance, pin data comes
    straight from the *S declaration: Ulticap encodes a per-gate pin's
    numbers as one comma-separated *S value in declaration order (e.g.
    '#=4,12' means unit 1's number is 4, unit 2's is 12) -- the same
    convention the design's own gate lettering (e.g. U10A/U10B, sorted
    alphabetically -- see gate_units/_gate_sort_key) relies on to line up
    with placed instances elsewhere in this file. A pin number with no
    comma is shared unchanged across every unit (e.g. a non-power pin
    common to all gates).
    """
    result = []
    for pin in sym.pins:
        raw = pin.number or ''
        parts = raw.split(',')
        number = parts[unit_num - 1].strip() if len(parts) >= total_units else raw
        ktype = PINTYPE_TAG_MAP.get(pin.pin_type.upper(), 'passive')
        result.append((pin, number, pin.name, ktype))
    return result


# ── KiCad pin angle ───────────────────────────────────────────────────────────

def _pin_kicad_angle(pin_rotation: int) -> int:
    """
    Ulticap pin_rotation: direction the stub POINTS (away from body).
    KiCad pin angle: direction the pin points FROM the connection point TOWARD the body.
    So KiCad angle = Ulticap direction + 180°.
    Ulticap: 0=E,1=N,2=W,3=S  → KiCad: 180,270,0,90
    """
    return (pin_rotation * 90 + 180) % 360




def _comp_centre(comp: 'Component', sym) -> tuple:
    """Return bbox centre in Ulticap schematic coords using _rot_transform.

    Always pass (w/2, h/2) — _rot_transform handles dimension swapping
    automatically for 90°/270° rotations via its sign-and-swap rules.
    """
    if sym is None:
        return comp.x, comp.y
    # Use float division to avoid rounding gaps at pin connection points
    dx, dy = _rot_transform(sym.width / 2, sym.height / 2, comp.rotation)
    return comp.x + dx, comp.y + dy


# ── symbol geometry in lib_symbols ────────────────────────────────────────────

# Ulticap colour palette — screen-measured values from DOS Ulticap (CRT/EGA output).
# The EGA half-intensity DAC level is 170 (0xAA), not the theoretical register value 128.
# Colour fields > 15 carry flag bits in the upper nibble/byte; the palette index
# is always in the low nibble (bits 0-3), extracted via  colour & 0x0F.
_DEFAULT_ANNOT_COLOUR = 15  # White = Ulticap annotation default; no KiCad text override needed

# Ulticap semantic colour defaults for KiCad S-expression output.
# Values match the screen-measured palette above (palette indices in comments).
_KICAD_COL_WIRE     = (170, 0,   0  )   # palette[4]  red        — net wires
_KICAD_COL_BUS      = (32,  32,  235)   # palette[9]  light blue — buses
_KICAD_COL_JUNCTION = (0,   170, 0  )   # palette[2]  green      — junction dots
_KICAD_COL_STUB     = (0,   170, 170)   # palette[3]  cyan       — pin stubs / bus entries
_KICAD_COL_GND_LINK = (255, 0,   0  )   # bright red  — GND_LINK cosmetic stub (intentional)

# mm-per-mil (MM_PER_MIL) now lives in kiuc_model.py alongside MM_PER_UNIT,
# since both are Ulticap-measurement-to-mm conversions and MM_PER_MIL may be
# needed elsewhere in future. Imported above.

# KiCad's own default text size (50 mil / 1.27mm), used as the fallback
# whenever an Ulticap attribute/label has no explicit size.
_DEFAULT_TEXT_SIZE_MM = '1.27'

# Default REFDES/VALUE property offset from the symbol anchor (mm), used
# when no explicit *C/*S FILE= placement is present for that property.
_REFDES_DY_MM = -2.54
_VALUE_DY_MM  = 2.54


def _col(rgb: tuple) -> str:
    """Format an RGB tuple as a KiCad S-expression (color R G B 1) string."""
    return f'(color {rgb[0]} {rgb[1]} {rgb[2]} 1)'

# Palette mode toggle. False (default) = emit Ulticap's measured RGB values
# as explicit colour overrides, as before. True = "KiCad default palette":
# never emit an explicit colour override, regardless of the Ulticap colour
# index, so every item inherits whatever colour KiCad's active theme
# assigns to that item type. Set via set_use_kicad_colors().
USE_KICAD_DEFAULT_COLORS = False


def set_use_kicad_colors(enabled: bool) -> None:
    """Select the colour-emission mode for all subsequent writes.

    enabled=False (default): Ulticap's measured RGB palette is emitted as
        explicit overrides wherever a non-default Ulticap colour is used.
    enabled=True ('KiCad default palette'): no explicit colour overrides
        are ever emitted; every graphic/text item inherits its colour from
        KiCad's active colour theme instead.
    """
    global USE_KICAD_DEFAULT_COLORS
    USE_KICAD_DEFAULT_COLORS = bool(enabled)


def _colour_effects(colour: int, sz: str, just_str: str) -> str:
    """Build KiCad (effects ...) string for text, with (color R G B 1) inside (font ...).

    Annotation default colour is 15 (white on Ulticap's black background).
    Any other colour is emitted explicitly inside the font block, unless
    USE_KICAD_DEFAULT_COLORS is set, in which case no override is ever
    emitted and KiCad's theme colour is used instead.
    """
    if not USE_KICAD_DEFAULT_COLORS and colour != _DEFAULT_ANNOT_COLOUR:
        r, g, b = palette_rgb(colour)
        font = f'(font (size {sz} {sz}) (color {r} {g} {b} 1))'
    else:
        font = f'(font (size {sz} {sz}))'
    return f'(effects {font}{just_str})'

def _colour_stroke(colour: int, width_mm: str, line_type: str = 'default') -> str:
    """Build KiCad (stroke …) string, adding (color R G B 1) when
    USE_KICAD_DEFAULT_COLORS is not set.

    No palette-index suppression is applied: every Ulticap colour index,
    including the default foreground (index 5), is emitted explicitly.
    KiCad's own default for symbol lines, wires, and sheet borders does not
    match palette[5], so suppressing the override would render the wrong colour.
    The only gate is USE_KICAD_DEFAULT_COLORS, which bypasses all overrides."""
    if not USE_KICAD_DEFAULT_COLORS:
        r, g, b = palette_rgb(colour)
        return (f'(stroke (width {width_mm}) (type {line_type}) '
                f'(color {r} {g} {b} 1))')
    return f'(stroke (width {width_mm}) (type {line_type}))'


def _colour_fill(colour: int) -> str:
    """Build KiCad (fill …) string for a solid colour-filled shape, using
    the same colour convention as _colour_stroke (see USE_KICAD_DEFAULT_COLORS)."""
    if not USE_KICAD_DEFAULT_COLORS:
        r, g, b = palette_rgb(colour)
        return f'(fill (type color) (color {r} {g} {b} 1))'
    return '(fill (type outline))'


def _thick_line_rect_pts(x1: float, y1: float, x2: float, y2: float,
                         half_w: float):
    """Corners (closed, 5 points) of a rectangle spanning a straight line
    segment with flat (butt) ends.

    KiCad's schematic renderer always draws stroked lines with rounded
    end caps and offers no way to select a flat/butt cap instead — a
    thick (30mil) *S line therefore visually overshoots its nominal
    endpoints by half its width in every direction. Substituting a
    filled rectangle sidesteps that entirely: a filled shape's boundary
    is exactly its geometry, with no cap treatment to overshoot.
    Only meaningful for a single straight *solid* line — see
    _sym_polyline and _sym_polylines_geometry for the gating/chain-merge
    logic that decides which symbol-body lines qualify (never applied
    to graphic-layer UserLine records, and never applied across a
    non-collinear join, since that reintroduces gaps at outer corners).
    """
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-9:
        return None
    px, py = -dy / length * half_w, dx / length * half_w
    return [
        (x1 + px, y1 + py),
        (x2 + px, y2 + py),
        (x2 - px, y2 - py),
        (x1 - px, y1 - py),
        (x1 + px, y1 + py),
    ]


def _filled_rect_polyline_lines(x1: float, y1: float, x2: float, y2: float,
                                half_w_mm: float, colour: int) -> Optional[List[str]]:
    """Emit a (polyline ...) filled rectangle standing in for a single thick
    straight solid line (flat/butt caps instead of KiCad's rounded caps).
    Stroke is a hairline (0.001mm) rather than 0, coloured to match the
    fill, so no default-theme-coloured edge is visible around the shape.
    Returns None if the line is degenerate (zero length)."""
    rect = _thick_line_rect_pts(x1, y1, x2, y2, half_w_mm)
    if rect is None:
        return None
    pts = ' '.join(f'(xy {_f(x)} {_f(y)})' for x, y in rect)
    return [
        '      (polyline',
        f'        (pts {pts})',
        f'        {_colour_stroke(colour, "0.001", "solid")}',
        f'        {_colour_fill(colour)}',
        '      )',
    ]


def _sym_polyline(pl: Polyline, tr_rel: _RelTransform,
                  force_stroke: bool = False) -> List[str]:
    """Emit one (polyline ...) for a symbol body line.

    force_stroke=True bypasses the filled-rectangle flat-cap substitution
    even for an otherwise-eligible thick solid line. Used by
    _sym_polylines_geometry's chain analysis for lines that are joined
    to other lines at a real (non-collinear) corner, in a closed loop, or
    at a branch point — flat-capping any of those would reintroduce the
    "corner cutout" gap the rectangle trick was meant to fix.

    A single Polyline record with 3+ points is also eligible, provided
    every point is exactly collinear: Ulticap sometimes records a
    straight thick line as one polyline with an extra inline vertex
    (e.g. a redundant midpoint) rather than as two separate 2-point
    Polyline records, which would otherwise fall through to a plain
    stroked line and show the same rounded-cap/join artifact the
    rectangle trick exists to avoid. The true endpoints are found by
    projection onto the line direction, not just points[0]/points[-1],
    so an out-of-order point list is still handled correctly.
    """
    # width=6mil is the file's default flag value; 30mil is the "thick" case.
    is_thick = pl.width != 6
    is_solid = pl.linetype == 0

    flat_cap_endpoints = None
    if is_thick and is_solid and not force_stroke:
        if len(pl.points) == 2:
            flat_cap_endpoints = pl.points[0], pl.points[1]
        elif len(pl.points) >= 3:
            p0, pN = pl.points[0], pl.points[-1]
            dx, dy = pN[0] - p0[0], pN[1] - p0[1]
            if dx or dy:
                collinear = all((p[0] - p0[0]) * dy - (p[1] - p0[1]) * dx == 0
                                for p in pl.points)
                if collinear:
                    def _proj(p):
                        return (p[0] - p0[0]) * dx + (p[1] - p0[1]) * dy
                    flat_cap_endpoints = min(pl.points, key=_proj), max(pl.points, key=_proj)

    if flat_cap_endpoints is not None:
        (x1, y1), (x2, y2) = flat_cap_endpoints
        rx1, ry1 = tr_rel.x(x1), tr_rel.y(y1)
        rx2, ry2 = tr_rel.x(x2), tr_rel.y(y2)
        half_w_mm = _sym_line_width_mm(is_thick) / 2
        lines = _filled_rect_polyline_lines(rx1, ry1, rx2, ry2, half_w_mm, pl.colour)
        if lines is not None:
            return lines
        # fall through to the stroked-line path if degenerate (zero length)

    pts = ' '.join(f'(xy {_f(tr_rel.x(x))} {_f(tr_rel.y(y))})' for x, y in pl.points)
    w_mm = _f(_sym_line_width_mm(is_thick))
    lt   = UC_POLYLINE_STYLE_BY_CODE.get(pl.linetype, 'solid')
    stroke = _colour_stroke(pl.colour, w_mm, lt)
    return [
        '      (polyline',
        f'        (pts {pts})',
        f'        {stroke}',
        '        (fill (type none))',
        '      )',
    ]


def _sym_polylines_geometry(polylines: List[Polyline], tr_rel: _RelTransform) -> List[str]:
    """Emit all symbol-body polylines for one *S symbol, gating the
    filled-rectangle flat-cap substitution by chain analysis first.

    Ulticap *S bodies commonly encode multi-sided shapes (title-block
    borders, box outlines, etc.) as several separate 2-point thick solid
    Polyline entries rather than one connected multi-point polyline.
    Applying the flat-cap rectangle trick to each such entry independently
    reintroduces a gap at outer corners (each flat-cap rectangle stops
    exactly at its own nominal endpoint, so neither of two segments
    meeting at a corner covers the small square outside that corner).

    Two eligible (thick/solid/2-point) segments are treated as "joined"
    only if they share an exact endpoint *and* match both colour and
    width — a purposeful colour/width change along an otherwise straight
    run means the segments are NOT collapsed together, so that data is
    preserved rather than silently overridden.

    Each resulting connected component is then classified:
      - size 1 (no join)                        → standalone flat-cap rect
      - simple open chain, exactly collinear     → merged into ONE
                                                    flat-cap rect spanning
                                                    the two true outer
                                                    endpoints (handles a
                                                    single line that was
                                                    simply split into two
                                                    file records)
      - closed loop, branch point (3+ segments
        meeting), or a chain that bends          → real corner/shape:
                                                    every member falls
                                                    back to a normal
                                                    stroked line (rounded
                                                    joins/caps, which is
                                                    visually acceptable
                                                    there)
    """
    n = len(polylines)
    eligible = [i for i, pl in enumerate(polylines)
                if pl.width != 6 and pl.linetype == 0 and len(pl.points) == 2]

    # ── union-find over eligible segments sharing an endpoint + colour + width ──
    parent = {i: i for i in eligible}

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    point_map: Dict[Tuple[Tuple[int, int], int, int], List[int]] = {}
    for i in eligible:
        pl = polylines[i]
        for p in (pl.points[0], pl.points[1]):
            key = (p, pl.colour, pl.width)
            point_map.setdefault(key, []).append(i)
    for idxs in point_map.values():
        for k in range(1, len(idxs)):
            _union(idxs[0], idxs[k])

    components: Dict[int, List[int]] = {}
    for i in eligible:
        components.setdefault(_find(i), []).append(i)

    force_stroke = set()          # indices that must NOT use the flat-cap rect
    merge_of: Dict[int, Tuple[Tuple[int, int], Tuple[int, int], int, int]] = {}
    consumed = set()              # non-leader members of a merge group (skip entirely)

    for members in components.values():
        if len(members) < 2:
            continue  # standalone — handled by the default per-item path

        deg: Dict[Tuple[int, int], int] = {}
        for i in members:
            for p in polylines[i].points:
                deg[p] = deg.get(p, 0) + 1
        point_count = len(deg)
        seg_count = len(members)
        deg1_points = [p for p, d in deg.items() if d == 1]
        has_branch = any(d >= 3 for d in deg.values())
        is_simple_chain = (not has_branch and point_count == seg_count + 1
                          and len(deg1_points) == 2)

        if not is_simple_chain:
            # closed loop, branch point, or otherwise irregular → real shape
            force_stroke.update(members)
            continue

        a, b = deg1_points
        # exact-integer collinearity test of every point against line a→b
        abx, aby = b[0] - a[0], b[1] - a[1]
        collinear = all((p[0] - a[0]) * aby - (p[1] - a[1]) * abx == 0
                        for i in members for p in polylines[i].points)
        if not collinear:
            force_stroke.update(members)
            continue

        leader = min(members)
        colour = polylines[leader].colour
        width = polylines[leader].width
        merge_of[leader] = (a, b, colour, width)
        consumed.update(m for m in members if m != leader)

    lines: List[str] = []
    for i in range(n):
        if i in consumed:
            continue
        if i in merge_of:
            (x1, y1), (x2, y2), colour, width = merge_of[i]
            rx1, ry1 = tr_rel.x(x1), tr_rel.y(y1)
            rx2, ry2 = tr_rel.x(x2), tr_rel.y(y2)
            half_w_mm = _sym_line_width_mm(width != 6) / 2
            rect_lines = _filled_rect_polyline_lines(rx1, ry1, rx2, ry2, half_w_mm, colour)
            if rect_lines is not None:
                lines += rect_lines
                continue
            # degenerate (shouldn't happen for a valid 2+-endpoint chain) —
            # fall back to emitting the original segment normally
        lines += _sym_polyline(polylines[i], tr_rel, force_stroke=i in force_stroke)
    return lines


def _sym_circle(c: Circle, tr_rel: _RelTransform,
                is_less_than_v500: bool = False) -> List[str]:
    """Emit circle or arc for a symbol body.

    KiCad arc format: (arc (start X Y) (mid X Y) (end X Y) ...)
    mid is the point on the arc halfway between start and end.

    Arc direction rule (empirically verified against ground-truth KiCad schematics):
    - SCH sweep < 0 (CW):  use start/sweep as-is.
    - SCH sweep > 0 (CCW): reverse the arc so it is CW in KiCad.
        new_start = old_start + old_sweep  (normalised to (−180, +180])
        new_sweep = −old_sweep
    All arcs stored in KiCad must have negative (CW) sweep for correct rendering.
    """
    cx_mm  = tr_rel.x(c.cx)
    cy_mm  = tr_rel.y(c.cy)
    r_mm   = tr_rel.d(c.r)
    # thick=6mil is the file's default flag value; keep emitting '0' (KiCad
    # thick=6/30 is the file's thin/thick flag value (see _sym_line_width_mm
    # for the real-mil conversion, driven by THIN_LINE_WIDTH/THICK_LINE_WIDTH).
    sw_mm  = _f(_sym_line_width_mm(c.thick != 6))
    lt     = UC_ARC_STYLE_BY_CODE.get(c.arc_linetype, 'solid')
    stroke = _colour_stroke(c.colour, sw_mm, lt)

    if c.is_full_circle:
        return [
            '      (circle',
            f'        (center {_f(cx_mm)} {_f(cy_mm)})',
            f'        (radius {_f(r_mm)})',
            f'        {stroke}',
            '        (fill (type none))',
            '      )',
        ]

    rotate_raw = c.rotate
    angle_raw  = c.angle

    # V5.xx arc encoding bugs — DOS Ulticap (V4.xx and below) files are unaffected:
    # DOS files use 270° arcs (angle_raw=17280 > 11520) legitimately, so the
    # complement fix must NOT fire on them.
    if not is_less_than_v500:
        # Bug 1 — complement arc: angle > 180° saved as exterior arc.
        # Fix: reconstruct the original interior arc.
        if angle_raw > 11520:
            corrected_angle = -(23040 - angle_raw)
            rotate_raw = rotate_raw - corrected_angle
            angle_raw  = corrected_angle
        # Bug 2 — semicircle direction: V5.72 flips +180° to -180°, producing the
        # opposite (lower) semicircle.  V4.60 never uses exactly -180°.
        # Fix: negate back to +180° so the existing reversal rule produces the
        # correct upper semicircle.
        elif angle_raw == -11520:
            angle_raw = 11520

    # Apply arc direction rule using (possibly corrected) raw values
    start_deg = rotate_raw / 64.0
    sweep_deg = angle_raw  / 64.0
    if sweep_deg > 0:
        # Reverse: new start = old end, new sweep = negated
        raw = start_deg + sweep_deg
        # Normalise to (−180, +180]
        while raw >  180: raw -= 360
        while raw <= -180: raw += 360
        start_deg = raw
        sweep_deg = -sweep_deg

    mid_deg = start_deg + sweep_deg / 2
    end_deg = start_deg + sweep_deg

    def pt(deg):
        rad = math.radians(deg)
        return (_f(cx_mm + r_mm * math.cos(rad)),
                _f(cy_mm + r_mm * math.sin(rad)))

    sx, sy = pt(start_deg)
    mx, my = pt(mid_deg)
    ex, ey = pt(end_deg)

    return [
        '      (arc',
        f'        (start {sx} {sy})',
        f'        (mid {mx} {my})',
        f'        (end {ex} {ey})',
        f'        {stroke}',
        '        (fill (type none))',
        '      )',
    ]


class _RelTransform:
    """Symbol-relative transform for lib_symbols geometry.

    Ulticap *S: origin=BOTTOM-LEFT of bbox, x+ right, y+ UP (Cartesian).
    KiCad lib_symbols: origin=bbox CENTRE, x+ right, y+ UP.
    Both systems have y+ UP, so no y-flip is needed — only a shift to centre.
    half_w, half_h: bbox half-dimensions in Ulticap units.
    """
    def __init__(self, half_w: float = 0, half_h: float = 0):
        self.cx = half_w
        self.cy = half_h

    def x(self, u: int) -> float:
        return round((u - self.cx) * MM_PER_UNIT, 4)

    def y(self, u: int) -> float:
        return round((u - self.cy) * MM_PER_UNIT, 4)  # no flip: both *S and KiCad lib use y+ UP

    def d(self, u: int) -> float:
        return round(abs(u) * MM_PER_UNIT, 4)


def _lib_key(comp: Component) -> str:
    """Unique lib_symbols key for this component instance.

    Each component gets its own embedded symbol so that pin numbers from *C
    are always correct (multi-gate chips share a *S but have different pin sets).
    Format: "SymName_RefDes" or just "SymName" if no refdes available.
    """
    rd = (comp.refdes or '').strip().rstrip('?') or None
    if rd:
        return f'{comp.symbol_name}_{rd}'
    return comp.symbol_name


def _lib_sym_header(lib_id: str, key: str, sym, comp, pin_data,
                    is_power: bool = False) -> List[str]:
    """Emit the lib symbol header: (symbol "lib_id" ... properties ...)

    is_power=True: add (power) flag; force pin_numbers/pin_names hidden;
    use '#PWR' as Reference template and the *S WIRELABEL/LABEL default
    as Value template (matching KiCad power symbol convention).
    """
    _ref_sz = _f(_text_size_mm(next((sa.size for sa in sym.sym_attrs if sa.tag=='REFDES'), 32)))
    _val_sz = _f(_text_size_mm(next((sa.size for sa in sym.sym_attrs if sa.tag=='VALUE'),  32)))

    if is_power:
        # Power symbols: pin numbers and names always hidden; (power) flag required
        _pn_nums  = '      (pin_numbers (hide yes))'
        _pn_names = '      (pin_names (offset 0) (hide yes))'
        # Reference template = '#PWR' (KiCad convention for power symbols)
        _ref_val  = '#PWR'
        # Value template = *S WIRELABEL/LABEL default (net name for this symbol type)
        _val_val  = ''
        for sa in sym.sym_attrs:
            if sa.tag in ('WIRELABEL', 'LABEL') and sa.default_value not in ('', '-', '?'):
                _val_val = sa.default_value
                break
        if not _val_val:
            _val_val = key  # fallback to symbol name
    else:
        _s_num_vis  = {sa.default_value: sa.visibility for sa in sym.sym_attrs if sa.tag == '#'}
        _s_name_vis = {sa.default_value: sa.visibility for sa in sym.sym_attrs
                       if sa.tag in ('LABEL', 'WIRELABEL')}
        _c_num_vis  = {ca.value: ca.visibility for ca in comp.comp_attrs if ca.tag == '#'}
        _c_name_vis = {ca.value: ca.visibility for ca in comp.comp_attrs
                       if ca.tag in ('LABEL', 'WIRELABEL')}

        def _eff_nv(n): return _c_num_vis.get(n, _s_num_vis.get(n, 128))
        def _eff_lv(n): return _c_name_vis.get(n, _s_name_vis.get(n, 128))

        _all_nums_hidden  = bool(pin_data) and all((_eff_nv(num) & 128) == 0
                                 for _, num, _, _ in pin_data)
        # Empty pin name (no LABEL attr) is intrinsically invisible; don't let
        # its default vis=128 prevent the global (pin_names (hide yes)) from firing.
        _all_names_hidden = bool(pin_data) and all(
            (not nm) or ((_eff_lv(nm) & 128) == 0)
            for _, _, nm, _ in pin_data)
        _pn_nums  = '      (pin_numbers (hide yes))' if _all_nums_hidden else ''
        _pn_names = ('      (pin_names (hide yes))'
                     if _all_names_hidden else f'      (pin_names (offset {_f(OFFSET_PNAME)}))')
        _ref_val  = ''
        _val_val  = ''

    hdr = [f'    (symbol "{lib_id}"']
    if is_power:
        hdr.append('      (power)')
    if _pn_nums:
        hdr.append(_pn_nums)
    hdr.append(_pn_names)
    hdr += [
        '      (exclude_from_sim no)', '      (in_bom yes)', '      (on_board yes)',
        f'      (property "Reference" "{_esc(_ref_val)}"',
        '        (at 0 0 0)',
        f'        (effects (font (size {_ref_sz} {_ref_sz})) (hide yes))',
        '      )',
        f'      (property "Value" "{_esc(_val_val)}"',
        '        (at 0 0 0)',
        f'        (effects (font (size {_val_sz} {_val_sz})) (hide yes))',
        '      )',
        '      (property "Footprint" ""',
        '        (at 0 0 0)',
        f'        (effects (font (size {_ref_sz} {_ref_sz})) (hide yes))',
        '      )',
        '      (property "Datasheet" ""',
        '        (at 0 0 0)',
        f'        (effects (font (size {_ref_sz} {_ref_sz})) (hide yes))',
        '      )',
        '      (property "Description" ""',
        '        (at 0 0 0)',
        f'        (effects (font (size {_ref_sz} {_ref_sz})) (hide yes))',
        '      )',
    ]
    if is_power:
        hdr += [
            '      (property "ki_keywords" "global power"',
            '        (at 0 0 0)',
            f'        (effects (font (size {_ref_sz} {_ref_sz})) (hide yes))',
            '      )',
        ]
    return hdr


def _lib_sym_geometry(key: str, sym, tr_rel,
                      is_less_than_v500: bool = False) -> List[str]:
    """Emit the _0_1 sub-unit: polylines, arcs, circles (shared geometry)."""
    lines = [f'      (symbol "{_esc(key)}_0_1"']
    lines += _sym_polylines_geometry(sym.polylines, tr_rel)
    for c in sym.circles:
        lines += _sym_circle(c, tr_rel, is_less_than_v500)
    lines.append('      )')
    return lines


def _lib_sym_signal_pins(sym) -> List[str]:
    """Emit hidden power_in pins for SIGNAL= tags (invisible power connections).

    Each distinct net (one SIGNAL= entry) gets one position; all pin numbers
    belonging to that net are stacked at the same coordinates (same-net stacking).
    KiCad connects hidden power_in pins by net name, so stacking is correct.

    Placement — center-stagger algorithm:
      1. Determine the long axis (width >= height → long=Y, else long=X).
      2. Fix the short-axis coordinate at the body centre.
      3. Stagger group positions bidirectionally from the body centre along the
         long axis: i=0 → +base_off, i=1 → -base_off, i=2 → +(base_off+step), …
         This keeps all groups inside the body and symmetrically distributed.
      4. base_off is chosen as the smallest integer >= 3 such that
         (centre_long + base_off) % 5 != 0.  Combined with step=10
         (gcd(10,25)=5), this guarantees that every stagger coordinate is
         non-zero mod 5, so it can never be a multiple of 25 for any group
         index — giving the required one-coordinate-off-grid property.
         KiCad only connects a pin when BOTH coordinates are on the active
         grid (25 or 50 mil), so one off-grid coordinate prevents false
         connections while keeping the pins fully inside the bounding box
         and protected from accidental mouse interaction.
      5. Positions are clamped to [5, long_size-5] to stay inside the body.
      6. All pins use angle=0 and length=0 (hidden, length irrelevant).
    """
    if not getattr(sym, 'signal_pins', None):
        return []

    w, h   = sym.width, sym.height
    cx, cy = w // 2, h // 2
    long_h = (w >= h)          # True → stagger along Y, fixed X=cx
    # 20 Ulticap units = 1.016 mm (40 mil) — minimum separation between group
    # positions, chosen to exceed the 0.762 mm unconnected-pin circle KiCad
    # draws around every hidden pin, so circles of adjacent groups never overlap.
    _STEP  = 20

    def _base_off(centre: int) -> int:
        """Smallest int >= 10 s.t. (centre + b) % 5 != 0.
        Floor of 10 ensures group-0 to group-1 spacing (= 2*base_off) is also
        >= 20 units, matching the same minimum separation as _STEP.
        """
        for b in range(10, 35):
            if (centre + b) % 5 != 0:
                return b
        return 11  # unreachable

    if long_h:
        base = _base_off(cy)
        lo, hi = 5, h - 5
    else:
        base = _base_off(cx)
        lo, hi = 5, w - 5

    def _group_pos(i: int):
        """(ux, uy) for group index i."""
        half  = i // 2
        sign  = 1 if i % 2 == 0 else -1
        delta = sign * (base + half * _STEP)
        if long_h:
            var = max(lo, min(hi, cy + delta))
            return cx, var
        else:
            var = max(lo, min(hi, cx + delta))
            return var, cy

    tr    = _RelTransform(half_w=w / 2, half_h=h / 2)
    lines = []
    for gi, (net_name, pin_nums) in enumerate(sym.signal_pins):
        ux, uy = _group_pos(gi)
        lx = _f(tr.x(ux))
        ly = _f(tr.y(uy))
        for pnum in pin_nums:
            lines += [
                '      (pin power_in line',
                f'        (at {lx} {ly} 0)',
                '        (length 0)',
                '        (hide yes)',
                f'        (name "{_esc(net_name)}" (effects (font (size {_DEFAULT_TEXT_SIZE_MM} {_DEFAULT_TEXT_SIZE_MM}))))',
                f'        (number "{_esc(pnum)}" (effects (font (size {_DEFAULT_TEXT_SIZE_MM} {_DEFAULT_TEXT_SIZE_MM}))))',
                '      )',
            ]
    return lines


def _lib_sym_pins(key: str, unit_suffix: str, sym, comp, pin_data, tr_rel) -> List[str]:
    """Emit a _N_1 sub-unit: TXT labels + pins.

    unit_suffix is e.g. '_1_1' for single-gate or multi-gate unit 1, '_2_1' for unit 2.
    """
    _s_num_vis  = {sa.default_value: sa.visibility for sa in sym.sym_attrs if sa.tag == '#'}
    _s_name_vis = {sa.default_value: sa.visibility for sa in sym.sym_attrs
                   if sa.tag in ('LABEL', 'WIRELABEL')}
    _c_num_vis  = {ca.value: ca.visibility for ca in comp.comp_attrs if ca.tag == '#'}
    _c_name_vis = {ca.value: ca.visibility for ca in comp.comp_attrs
                   if ca.tag in ('LABEL', 'WIRELABEL')}

    def _eff_nv(n): return _c_num_vis.get(n, _s_num_vis.get(n, 128))
    def _eff_lv(n): return _c_name_vis.get(n, _s_name_vis.get(n, 128))

    _pin_num_sz  = _f(_text_size_mm(next((sa.size for sa in sym.sym_attrs if sa.tag=='#'),    22),
                                     tune=PIN_NUM_HEIGHT_TUNE))
    _pin_name_sz = _f(_text_size_mm(next((sa.size for sa in sym.sym_attrs if sa.tag=='LABEL'),22),
                                     tune=PIN_NAME_HEIGHT_TUNE))

    # Named-stub detection (mirrors _resolve_pins): a *S pin whose WIRELABEL
    # default is '?' is a placeholder pin (e.g. GND_LINK) — the real net name
    # comes per-instance via *C WIRELABEL, and the pin is rendered hidden so
    # KiCad's invisible-power_in name-merge applies.  Since hiding the pin
    # also hides its name text, a separate cosmetic (text ...) label shows
    # the WIRELABEL value, positioned/sized/justified from the *C WIRELABEL
    # attribute's own dx/dy/size/visibility (NOT the pin's POE) — the same
    # data source already used for ordinary standalone *C-driven labels.
    # A red underline beneath that text flags that it is NOT a live binding
    # to the actual (hidden) pin name: editing the text alone does not
    # rename the net, since KiCad has no mechanism linking a graphic text
    # string to a pin's name field.
    #
    # Excluded for real power symbols (is_pwr_symbol) — see _resolve_pins
    # for why: a PWR-type pin already gets correct power_in behaviour and
    # its net name from the dedicated power-symbol Value resolution, so it
    # must render through the normal pin path below, not the named-stub
    # hide/cosmetic-text path.
    _named_stub_poes = set() if is_pwr_symbol(sym) else {
        (sa.dx_poe, sa.dy_poe) for sa in sym.sym_attrs
        if sa.tag == 'WIRELABEL' and sa.default_value == '?'}
    _stub_wirelabel_ca = {}   # (dx_poe, dy_poe) -> *C WIRELABEL attribute object
    if _named_stub_poes:
        for ca in comp.comp_attrs:
            if ca.tag == 'WIRELABEL' and (ca.dx_poe, ca.dy_poe) in _named_stub_poes:
                _stub_wirelabel_ca[(ca.dx_poe, ca.dy_poe)] = ca

    lines = [f'      (symbol "{_esc(key)}{unit_suffix}"']

    # TXT graphic text labels (R=0, lib frame)
    # Symbol text angles are stored in tenths of a degree in KiCad.
    for sa in sym.sym_attrs:
        if sa.tag != 'TXT' or not sa.default_value.strip(): continue
        _tx = _f(tr_rel.x(sa.dx)); _ty = _f(tr_rel.y(sa.dy))
        _tsz = _f(_text_size_mm(sa.size, tune=PIN_NAME_HEIGHT_TUNE)) if sa.size else _DEFAULT_TEXT_SIZE_MM
        _th, _tv, _tiv, _ta = _vis_justify(0, sa.visibility)
        _ta10 = _ta * 10   # convert degrees to tenths-of-a-degree for symbol text
        lines += [
            f'      (text "{_esc(sa.default_value)}"',
            f'        (at {_tx} {_ty} {_ta10})',
            f'        (effects (font (size {_tsz} {_tsz})){_justify_str(_th,_tv)})',
            '      )',
        ]

    # Pins
    for pin, number, name, ktype in pin_data:
        cx_s, cy_s = _pin_conn_point(pin, sym)
        px   = _f(tr_rel.x(cx_s)); py = _f(tr_rel.y(cy_s))
        ang  = _pin_kicad_angle(pin.pin_rotation)
        plen = _f(tr_rel.d(_pin_length(pin, sym)))
        shape = _PIN_FORMAT_SHAPE.get(pin.pin_format, 'line')
        # Name/number visibility from LABEL and # vis bits (*C overrides *S).
        # PINTYPE vis only controls the PINTYPE annotation text (never shown),
        # not the pin stub. All pins from the pin section have visible stubs;
        # invisible pins are defined via SIGNAL= tags or the named-stub
        # pattern below, handled separately.
        _num_hide  = ' (hide yes)' if (_eff_nv(number) & 128) == 0 else ''
        _name_hide = ' (hide yes)' if (_eff_lv(name)   & 128) == 0 else ''

        _stub_key = (cx_s, cy_s)
        _is_named_stub = _stub_key in _stub_wirelabel_ca
        _pin_hide = '\n        (hide yes)' if _is_named_stub else ''

        lines += [
            f'      (pin {ktype} {shape}',
            f'        (at {px} {py} {ang})',
            f'        (length {plen}){_pin_hide}',
            f'        (name "{_esc(name)}" (effects (font (size {_pin_name_sz} {_pin_name_sz})){_name_hide}))',
            f'        (number "{_esc(number)}" (effects (font (size {_pin_num_sz} {_pin_num_sz})){_num_hide}))',
            '      )',
        ]

        if _is_named_stub:
            # Hiding the pin removes its drawn stub entirely, leaving the wire
            # that attaches at the connection point with no visible lead-in.
            # Draw a plain red line covering the same stub geometry (same
            # endpoints the real pin stub would have used) so the connection
            # still reads visually as the named-stub mechanism.
            import math as _math
            _ang_rad = _math.radians(ang)
            _stub_dx = float(plen) * _math.cos(_ang_rad)
            _stub_dy = float(plen) * _math.sin(_ang_rad)
            _stub_x0, _stub_y0 = float(px), float(py)
            _stub_x1, _stub_y1 = _stub_x0 + _stub_dx, _stub_y0 + _stub_dy
            lines += [
                '      (polyline',
                f'        (pts (xy {_f(_stub_x0)} {_f(_stub_y0)}) (xy {_f(_stub_x1)} {_f(_stub_y1)}))',
                f'        (stroke (width 0) (type default) {_col(_KICAD_COL_GND_LINK)})',
                '        (fill (type none))',
                '      )',
            ]

            # Cosmetic text showing the WIRELABEL value, since the pin's own
            # name text is hidden along with the pin.  Positioned/sized/
            # justified from the *C WIRELABEL attribute's own dx/dy/size/
            # visibility — NOT the pin's POE.  Coloured red and wrapped in
            # '*' to flag that it has no live binding to the actual (hidden)
            # pin name: editing this text does not rename the net.  Using
            # the text's own color (rather than a separately drawn underline)
            # means the marker always tracks the text automatically, even if
            # the text content or position changes later.
            _wl_ca = _stub_wirelabel_ca[_stub_key]
            _wx = _f(tr_rel.x(_wl_ca.dx)); _wy = _f(tr_rel.y(_wl_ca.dy))
            _wsz = _f(_text_size_mm(_wl_ca.size) if _wl_ca.size else float(_DEFAULT_TEXT_SIZE_MM))
            _wh, _wv, _, _wang = _vis_justify(0, _wl_ca.visibility)
            _wang10 = _wang * 10
            lines += [
                f'      (text "*{_esc(_wl_ca.value)}*"',
                f'        (at {_wx} {_wy} {_wang10})',
                f'        (effects (font (size {_wsz} {_wsz}) {_col(_KICAD_COL_GND_LINK)}){_justify_str(_wh,_wv)})',
                '      )',
            ]
    lines.append('      )')
    return lines


# ── hierarchical / port detection ─────────────────────────────────────────────

def _is_hier_sheet(comp: Component) -> bool:
    """True when this *C instance is a hierarchical sub-sheet placement.

    Detection: the *C block has a FILE= attribute (comp.file_ref is not None).
    This covers both V4.xx (HIERARCH_x symbol with FILE=? in *S, overridden in *C)
    and V5.xx (e.g. SHEETBUS_6 with FILE= directly in *C).
    """
    return comp.file_ref is not None


def _build_lib_symbols(sheet: Sheet) -> List[str]:
    """
    Emit (lib_symbols ...) with one embedded symbol per unique *S name.

    Multi-gate chips (same *S used with different pin sets) produce a single
    KiCad lib symbol with one _N_1 sub-unit per gate (N=1,2,...).  The shared
    _0_1 sub-unit holds the polylines/arcs/circles.  Each gate sub-unit holds
    the TXT labels and the pins for that gate.

    Single-gate chips get _0_1 (geometry) + _1_1 (TXT + pins) as usual.
    Skips TITLE and power symbols.
    """
    # Detect multi-gate: same *S symbol referenced with different pin sets.
    # Only *C '#' attrs whose (dx_poe, dy_poe) exactly matches a *S pin
    # connection point are counted — stale/corrupt attrs are discarded.
    def _valid_c_pin_attrs(sym, comp):
        """Yield *C '#' attrs that have an exact POE match in *S."""
        if sym is None:
            return
        s_poes = {_pin_conn_point(p, sym) for p in sym.pins}
        for ca in comp.comp_attrs:
            if ca.tag.startswith('#') and (ca.dx_poe, ca.dy_poe) in s_poes:
                yield ca

    pin_sets: Dict[str, set] = {}
    for comp in sheet.components:
        sn = comp.symbol_name
        if _is_hier_sheet(comp): continue           # sheet placements → (sheet ...), not lib symbol
        sym = sheet.symbols.get(sn)
        if sym is None: continue
        if is_port_sym(sym): continue              # port symbols → (hierarchical_label ...), not lib symbol
        # Power symbols (single PWR pin) always single-gate — skip multi detection
        if is_pwr_symbol(sym): continue
        # PARTS=1 (or absent) means explicitly single-gate — skip multi detection
        if int(sym.attributes.get('PARTS', '1')) <= 1: continue
        pin_nums = frozenset(ca.value for ca in _valid_c_pin_attrs(sym, comp))
        pin_sets.setdefault(sn, set()).add(pin_nums)
    # multi_gate is every symbol name that reaches this point at all: PARTS>1
    # was already required to survive the 'continue' above, and Ulticap's
    # dual pin-number declaration (e.g. '#=4,12') structurally requires a
    # separate *C instance per gate to disambiguate which number applies --
    # so a single placed instance already means "one gate of a multi-gate
    # part", not "a single-gate part that happens to look similar". A design
    # is free to place only one gate of a multi-gate IC and leave the other
    # unused; that must still be treated as multi-gate (REFDES letter
    # stripped, unit number assigned) or its Reference ends up with a
    # trailing gate letter baked in, which KiCad's ERC reports as
    # unannotated.
    multi_gate = set(pin_sets.keys())

    # ── Named-stub variant detection ──────────────────────────────────────────
    # A "named-stub" pin has *S WIRELABEL default == '?': a placeholder whose
    # real name comes per-instance from *C WIRELABEL/LABEL (e.g. GND_LINK:
    # LNK1 -> CGND, LNK2 -> GND).  When two instances of the same *S symbol
    # resolve different names at the stub's POE, each instance needs its own
    # lib_symbols entry (its own embedded pin name) rather than collapsing to
    # whichever instance happened to be encountered first.
    def _named_stub_poes_of(sym):
        return {(sa.dx_poe, sa.dy_poe) for sa in sym.sym_attrs
                if sa.tag == 'WIRELABEL' and sa.default_value == '?'}

    def _stub_names_of(sym, comp):
        """Names this instance resolves at each named-stub POE, as a sorted tuple."""
        stub_poes = _named_stub_poes_of(sym)
        if not stub_poes:
            return None
        names = []
        for ca in comp.comp_attrs:
            if ca.tag in ('LABEL', 'WIRELABEL') and (ca.dx_poe, ca.dy_poe) in stub_poes:
                names.append((ca.dx_poe, ca.dy_poe, ca.value))
        return tuple(sorted(names))

    stub_name_variants: Dict[str, set] = {}   # sn -> set of distinct name tuples
    for comp in sheet.components:
        sn = comp.symbol_name
        if sn in multi_gate: continue
        if _is_hier_sheet(comp): continue
        sym = sheet.symbols.get(sn)
        if sym is None or is_port_sym(sym): continue
        if is_pwr_symbol(sym): continue
        names = _stub_names_of(sym, comp)
        if names:
            stub_name_variants.setdefault(sn, set()).add(names)
    named_stub_variant_syms = {sn for sn, variants in stub_name_variants.items()
                                if len(variants) > 1}

    # For multi-gate: build ordered list of (pin_frozenset, comp) per symbol name
    gate_units: Dict[str, List] = {}   # sn -> [(pins_frozenset, comp), ...]
    entries: Dict[str, Component] = {} # sn -> first comp (for single-gate)
    for comp in sheet.components:
        sn = comp.symbol_name
        if _is_hier_sheet(comp): continue           # sheet placements → no lib entry
        sym = sheet.symbols.get(sn)
        if sym is None: continue
        if is_port_sym(sym): continue              # port symbols → no lib entry
        if sn in multi_gate:
            pin_nums = frozenset(ca.value for ca in _valid_c_pin_attrs(sym, comp))
            existing = [pn for pn, _ in gate_units.get(sn, [])]
            if pin_nums not in existing:
                gate_units.setdefault(sn, []).append((pin_nums, comp))
        elif sn in named_stub_variant_syms:
            pass   # handled separately below — one lib entry per instance
        else:
            if sn not in entries:
                entries[sn] = comp

    # Sort each symbol's gate list by trailing alpha suffix of REFDES (A<B<C...)
    def _gate_sort_key(pf_comp):
        _, comp = pf_comp
        rd = comp.refdes or ''
        suffix = rd[-1] if rd and rd[-1].isupper() and rd[-1].isalpha() else ''
        return suffix
    for sn in gate_units:
        gate_units[sn].sort(key=_gate_sort_key)

    # Store unit assignments for _comp_sexp (sn -> {pin_frozenset: unit_number})
    sheet._gate_unit_map = {}
    for sn, units in gate_units.items():
        sheet._gate_unit_map[sn] = {pf: i+1 for i, (pf, _) in enumerate(units)}

    # Store ALL pin numbers across all gates (for placed symbol pin list)
    sheet._gate_all_pins = {}
    for sn, units in gate_units.items():
        all_pins = set()
        for pf, comp in units:
            all_pins |= set(pf)
        sheet._gate_all_pins[sn] = sorted(all_pins, key=lambda x: (0, int(x)) if x.isdigit() else (1, x))

    # Store base-refdes map (strip trailing alpha suffix for multi-gate)
    # IC3A -> 'IC3', IC3B -> 'IC3' (KiCad adds A/B automatically via unit number)
    sheet._gate_base_refdes = {}
    for sn in multi_gate:
        comps = [c for c in sheet.components if c.symbol_name == sn]
        for comp in comps:
            if comp.refdes and comp.refdes[-1].isalpha() and comp.refdes[-1].isupper():
                base = comp.refdes[:-1]
            else:
                base = comp.refdes
            sheet._gate_base_refdes[comp.refdes] = base

    # Build the LIB_ID-safe name map once for this sheet, and store it so
    # _comp_sexp can reuse it when building (lib_id ...) for placed instances.
    sheet._lib_name_map = _build_lib_name_map(sheet.symbols.keys())

    lines = ['  (lib_symbols']

    # ── Single-gate symbols (including power symbols) ───────────────────────
    for key, comp in sorted(entries.items()):
        sn  = comp.symbol_name
        sym = sheet.symbols.get(sn)
        if sym is None:
            continue
        is_pwr = is_pwr_symbol(sym)
        pin_data = _resolve_pins(sym, comp)
        safe_key = sheet._lib_name_map.get(key, key)
        lib_id   = f'Ulticap:{_esc(safe_key)}'
        tr_rel   = _RelTransform(half_w=sym.width/2, half_h=sym.height/2)

        lines += _lib_sym_header(lib_id, key, sym, comp, pin_data, is_power=is_pwr)
        lines += _lib_sym_geometry(safe_key, sym, tr_rel, sheet.is_less_than_v500)   # _0_1
        _plines = _lib_sym_pins(safe_key, '_1_1', sym, comp, pin_data, tr_rel)
        # Signal (hidden power) pins only for non-power symbols
        if not is_pwr:
            _slines = _lib_sym_signal_pins(sym)
            if _slines:
                lines += _plines[:-1] + _slines + [_plines[-1]]
            else:
                lines += _plines
        else:
            lines += _plines
        lines.append('    )')

    # ── Named-stub variant symbols (e.g. GND_LINK) ────────────────────────────
    # One lib entry per instance, each with its own resolved pin name baked in.
    # _comp_sexp consults sheet._lib_id_override (keyed by id(comp)) to place
    # each instance against its matching entry instead of a shared one.
    sheet._lib_id_override = {}
    for sn in sorted(named_stub_variant_syms):
        sym = sheet.symbols.get(sn)
        if sym is None:
            continue
        instances = [c for c in sheet.components if c.symbol_name == sn]
        for comp in instances:
            pin_data = _resolve_pins(sym, comp)
            safe_key = _lib_key(comp)
            lib_id   = f'Ulticap:{_esc(safe_key)}'
            tr_rel   = _RelTransform(half_w=sym.width/2, half_h=sym.height/2)

            sheet._lib_id_override[id(comp)] = safe_key

            lines += _lib_sym_header(lib_id, safe_key, sym, comp, pin_data, is_power=False)
            lines += _lib_sym_geometry(safe_key, sym, tr_rel, sheet.is_less_than_v500)
            lines += _lib_sym_pins(safe_key, '_1_1', sym, comp, pin_data, tr_rel)
            lines.append('    )')

    # ── Multi-gate symbols ────────────────────────────────────────────────────
    for sn in sorted(multi_gate):
        sym = sheet.symbols.get(sn)
        if sym is None:
            continue
        units      = gate_units.get(sn, [])
        first_comp = units[0][1] if units else None
        if first_comp is None:
            continue
        first_pin_data = _resolve_pins(sym, first_comp)
        safe_sn    = sheet._lib_name_map.get(sn, sn)
        lib_id     = f'Ulticap:{_esc(safe_sn)}'
        tr_rel     = _RelTransform(half_w=sym.width/2, half_h=sym.height/2)

        lines += _lib_sym_header(lib_id, sn, sym, first_comp, first_pin_data)
        lines += _lib_sym_geometry(safe_sn, sym, tr_rel, sheet.is_less_than_v500)    # _0_1: shared shape

        # Every unit the *S PARTS attribute declares must appear here, not
        # just the ones with a placed *C instance -- a design may legitimately
        # use only some gates of a multi-gate IC (see _synth_gate_pin_data).
        # Units with no placed instance use a blank stand-in Component (no
        # comp_attrs) so _lib_sym_pins falls back purely to *S-declared
        # visibility/etc. for them, rather than incorrectly inheriting
        # another unit's *C-level overrides.
        _total_units = int(sym.attributes.get('PARTS', '1'))
        _placed_by_unit = {i + 1: comp for i, (_, comp) in enumerate(units)}
        _blank_comp = Component(x=0, y=0, rotation=0, symbol_name=sn)
        for unit_num in range(1, _total_units + 1):
            comp = _placed_by_unit.get(unit_num)
            if comp is not None:
                pin_data = _resolve_pins(sym, comp)
            else:
                pin_data = _synth_gate_pin_data(sym, unit_num, _total_units)
                comp = _blank_comp
            _plines = _lib_sym_pins(safe_sn, f'_{unit_num}_1', sym, comp, pin_data, tr_rel)
            if unit_num == 1:
                _slines = _lib_sym_signal_pins(sym)
                if _slines:
                    lines += _plines[:-1] + _slines + [_plines[-1]]
                else:
                    lines += _plines
            else:
                lines += _plines

        lines.append('    )')


    lines.append('  )')           # close lib_symbols
    return lines


# ── component placement ────────────────────────────────────────────────────────


def _uc_text_anchor_shift(rotation: int, visibility: int, size: int):
    """World-frame (shift_dx, shift_dy) anchor shift, in Ulticap units, for
    a text attribute given its component's rotation, the attribute's own
    visibility byte, and its size.

    CRITICAL — this must be ADDED to the already-rotated base anchor,
    NOT combined into (dx, dy) before rot_transform:

        ox, oy = _rot_transform(a.dx, a.dy, comp.rotation)   # unchanged
        sx, sy = _uc_text_anchor_shift(comp.rotation, a.visibility, a.size)
        ox, oy = ox + sx, oy + sy

    An earlier version of this function returned a LOCAL-frame shift meant
    to be combined into (dx, dy) before a single rot_transform(...,
    comp.rotation) call. Since rot_transform is linear, that's
    mathematically equivalent to rotating the shift by the FULL
    comp.rotation transform — but that's not what real rendering does.
    kiuc_viewer.py's _text_display_angle computes the text's actual
    display rotation as (local_angle + (rotation & 3) * 90) % 180 — the
    % 180 is deliberate: Ulticap/KiCad's text-readability convention
    means a 180-degree-different placement is handled by a semantic
    left/right or top/bottom SWAP (already captured by ulticap_translate,
    below), not a literal upside-down rotation. So the shift only ever
    needs a 0-or-90-degree rotation, never the full 8-value comp.rotation
    transform (which includes 180, 270, and 4 mirrored variants). Verified
    against a direct simulation of the viewer's box model across all 144
    rotation x alignment combinations in the R0-R7_NS dataset — the old
    (combine-then-rotate) structure mismatched on 54 of 72 is_v=False
    cases alone (every rotation except 0 and 1, which is exactly why
    testing only at those two rotations never revealed it).

    hjust/vjust/is_v are derived via ulticap_translate(rotation,
    visibility) — matching kiuc_viewer.py's _comp_attr_entries exactly —
    not from the raw visibility byte alone.

    Every text anchor this writer emits — REFDES/VALUE, DEVICE, extra
    component properties, standalone LABEL/WIRELABEL net labels,
    power-symbol VALUE (via WIRELABEL/LABEL), and sheet-box
    labels/properties — needs this same correction (see
    ULTICAP_TEXT_MODEL.md §7, uc_anchor_shift_u, uc_anchor_shift_v, and
    the Step 5 writer/viewer commonality work). Single shared helper so
    the fix can't be missed at one of the several call sites again.
    """
    if not size:
        return 0.0, 0.0
    u = ulticap_translate(rotation, visibility)
    hjust, vjust, is_v, _angle = u_to_justify(u)
    local_angle = 90 if is_v else 0
    ang = (local_angle + (rotation & 3) * 90) % 180
    shift_x = uc_anchor_shift_u(hjust, size)
    shift_y = uc_anchor_shift_v(vjust, size)
    if ang == 90:
        shift_x, shift_y = _rot_transform(shift_x, shift_y, 1)
    return shift_x, shift_y


def _comp_sexp(comp: Component, sheet: Sheet, tr: _Transform,
               _tb_counter: dict = None) -> List[str]:
    """Emit one placed (symbol ...) S-expression for a component instance.

    Design rules (from full analysis of Ulticap *S/*C format):

    1. POWER SYMBOLS: exactly one pin with pin_type==PWR.
       - lib_id = Ulticap:PWR_<net_name>
       - Reference = #PWRnn (always hidden — KiCad bookkeeping only)
       - Value = net name (from WIRELABEL or LABEL tag, *C first then *S)
       - Value position/size/vis = from WIRELABEL/LABEL tag (*C first then *S)

    2. REGULAR COMPONENTS:
       - lib_id = Ulticap:<symbol_name>
       - Reference = REFDES value
       - Value = VALUE tag value (*C first, then *S default, then '-')
       - All positions/sizes/vis from *C first, then *S fallback

    3. VISIBILITY: every show/hide uses vis bit 7 (128) of the item's tag.
       No hardcoded decisions based on tag name or symbol type, with two
       legitimate exceptions: #PWRnn Reference always hidden (KiCad
       bookkeeping); Footprint/Datasheet/Description always hidden (no
       Ulticap equivalents).

    4. *S FALLBACK: when *C has no entry for a tag, *S provides position,
       size, and visibility. The merged effective entry is used for output.

    5. LABELn IN *C: LABEL/LABELn tags in *C whose value does not appear
       as a LABEL default_value in *S are net labels attached to pin POEs.
       These are emitted as KiCad (label ...) records after the symbol block.
    """
    sn = comp.symbol_name
    sym = sheet.symbols.get(sn)

    # ── Hierarchical sheet placement → (sheet ...) ───────────────────────────
    if _is_hier_sheet(comp):
        _subsheet_map  = getattr(sheet, '_subsheet_map',  {})
        _sheet_elem_uuid = getattr(sheet, '_sheet_elem_uuids', {}).get(id(comp), _uid())
        _sh_lines, _stub_lines = _sheet_sexp(comp, sheet, tr, _subsheet_map, _sheet_elem_uuid)
        return _sh_lines + _stub_lines

    # ── Port symbol → (hierarchical_label ...) ───────────────────────────────
    if is_port_sym(sym):
        # Net name: first non-placeholder LABEL or WIRELABEL in *C, deduped by full tuple.
        _seen_port = set()
        _net_name  = None
        _label_size = 35
        for ca in comp.comp_attrs:
            if ca.tag not in ('LABEL', 'WIRELABEL'):
                continue
            _key = (ca.tag, ca.value, ca.dx, ca.dy,
                    ca.dx_poe, ca.dy_poe, ca.size, ca.colour, ca.visibility)
            if _key in _seen_port:
                continue
            _seen_port.add(_key)
            if ca.value and ca.value not in ('?', '-', ''):
                _net_name   = ca.value
                _label_size = ca.size
                break
        if not _net_name:
            _net_name = '?'

        # KiCad shape from PORT= (V4) or DEVICE= name (V5)
        _port_val = sym.attributes.get('PORT', '')
        if not _port_val:
            # V5: derive from DEVICE= name, strip 'PORT_BUS_' or 'PORT_' prefix
            _dev = sym.attributes.get('DEVICE', '').upper()
            for _pfx in ('PORT_BUS_', 'PORT_'):
                if _dev.startswith(_pfx):
                    _port_val = _dev[len(_pfx):]
                    break
        _shape = _PORT_SHAPE.get(_port_val.upper(), 'passive')

        # Wire connection point: pin conn point in *S, rotated to schematic coords
        _pin       = sym.pins[0]
        _conn_s    = _pin_conn_point(_pin, sym)
        _rdx, _rdy = _rot_transform(_conn_s[0], _conn_s[1], comp.rotation)
        _wx = _f(tr.x(comp.x + _rdx))
        _wy = _f(tr.y(comp.y + _rdy))

        # KiCad angle and justify
        _hang  = _HIER_LABEL_ANGLE.get(comp.rotation & 7, 0)
        _hjust = _HIER_LABEL_JUSTIFY.get(_hang, 'left')

        # Text size
        _lsz = _f(_text_size_mm(_label_size)) if _label_size else _DEFAULT_TEXT_SIZE_MM

        # The hierlabel name must exactly match the bus label text so KiCad
        # connects the bus across the sheet boundary without conflict.
        # _port_label_text (set in write_schematic pre-pass) provides the
        # correct form: PREFIX[M..N] verbatim, or {NAME} for group buses.
        # Fall back to _sanitize_port_name for single-sheet conversions where
        # the pre-pass doesn't run (no _port_label_text on sheet).
        _plt = getattr(sheet, '_port_label_text', {})
        _hl_name = _plt.get(_net_name) or _sanitize_port_name(
            _net_name, getattr(sheet, '_port_name_map', None))

        return [
            f'  (hierarchical_label "{_esc(_hl_name)}"',
            f'    (shape {_shape})',
            f'    (at {_wx} {_wy} {_hang})',
            f'    (effects (font (size {_lsz} {_lsz})) (justify {_hjust}))',
            f'    (uuid "{_uid()}")',
            '  )',
        ]

    # ── Power symbol detection ────────────────────────────────────────────────
    _is_pwr = is_pwr_symbol(sym)

    # ── Rotation / mirror ─────────────────────────────────────────────────────
    angle    = (comp.rotation & 3) * 90
    mirror_y = bool(comp.rotation & 4)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _find_attr(tag: str):
        """Return (entry, is_comp) for a tag: *C first, then *S fallback."""
        for ca in comp.comp_attrs:
            if ca.tag == tag:
                return ca, True
        if sym:
            for sa in sym.sym_attrs:
                if sa.tag == tag:
                    return sa, False
        return None, False

    def _entry_value(entry) -> str:
        """Get value string from a comp_attr or sym_attr entry."""
        return entry.value if hasattr(entry, 'value') else entry.default_value

    def _prop_pos(tag: str, fallback_dy: float = 0.0):
        """Schematic position (kx, ky) for a tag, *C first then *S fallback.

        The anchor is shifted per Ulticap's own box-model margins, on
        both axes, so that KiCad's direct-anchor (justify ...) renders at
        the same position Ulticap's own virtual-box model would put it —
        see ULTICAP_TEXT_MODEL.md §7, _uc_text_anchor_shift's docstring
        (the corrected shift-then-add structure), and the Step 5
        writer/viewer commonality work. Without this, right/center-
        justified properties render at KiCad's raw anchor position,
        systematically displaced from authentic Ulticap placement.
        """
        a, _ = _find_attr(tag)
        if a is not None:
            ox, oy = _rot_transform(a.dx, a.dy, comp.rotation)
            shift_dx, shift_dy = _uc_text_anchor_shift(comp.rotation, a.visibility, a.size)
            ox, oy = ox + shift_dx, oy + shift_dy
            return tr.x(comp.x + ox), tr.y(comp.y + oy)
        _cx, _cy = _comp_centre(comp, sym)
        return tr.x(_cx), tr.y(_cy) + fallback_dy

    def _prop_params(tag: str):
        """Return (angle, justify_str, hide, sz_mm) from vis/size of tag."""
        a, _ = _find_attr(tag)
        if a is not None:
            vis = a.visibility
            hjust, vjust, _, txt_ang = _vis_justify(comp.rotation, vis)
            return (txt_ang, _justify_str(hjust, vjust),
                    (vis & 128) == 0,
                    _f(_text_size_mm(a.size)) if a.size else _DEFAULT_TEXT_SIZE_MM)
        return 0, '', True, _DEFAULT_TEXT_SIZE_MM   # tag absent → hidden by default

    # ── Reference ─────────────────────────────────────────────────────────────
    if _is_pwr:
        refdes   = getattr(sheet, '_pwr_refs', {}).get(id(comp), '#PWR')
        ref_x, ref_y = _prop_pos('REFDES', _REFDES_DY_MM)
        ref_hide = True   # #PWRnn always hidden — KiCad internal bookkeeping
        ref_just = ''
        ref_ang  = 0
        ref_sz   = _DEFAULT_TEXT_SIZE_MM
    else:
        refdes = comp.refdes or comp.attributes.get('REFDES', '?')
        _base_map = getattr(sheet, '_gate_base_refdes', {})
        if refdes in _base_map:
            refdes = _base_map[refdes]
        # ── Title-block symbols: assign a unique hidden reference ──────────
        # TITLE / TITLE_REV have no meaningful REFDES in Ulticap; without an
        # explicit override they would be emitted as '?' (unannotated) and
        # trigger duplicate_reference ERC errors on multi-sheet designs.
        # A '#TB_<stem><n>' reference (hidden, like #PWRnn) is collision-free
        # across sheets and invisible on the canvas.
        if sn in ('TITLE', 'TITLE_REV'):
            if _tb_counter is None:
                _tb_counter = {}
            _stem = Path(sheet.name).stem if sheet.name else 'SCH'
            _tb_n = _tb_counter.get(_stem, 0) + 1
            _tb_counter[_stem] = _tb_n
            refdes   = f'#TB_{_stem}{_tb_n}'
            ref_hide = True
            ref_x, ref_y = _prop_pos('REFDES', _REFDES_DY_MM)
            ref_ang, ref_just, ref_sz = 0, '', _DEFAULT_TEXT_SIZE_MM
        else:
            ref_x, ref_y    = _prop_pos('REFDES', _REFDES_DY_MM)
            ref_ang, ref_just, ref_hide, ref_sz = _prop_params('REFDES')

    # ── Value ─────────────────────────────────────────────────────────────────
    if _is_pwr:
        # Net name: WIRELABEL or LABEL, *C first then *S, skip placeholder values
        _net_name = None
        for _tag in ('WIRELABEL', 'LABEL'):
            for _pool in (comp.comp_attrs,
                          sym.sym_attrs if sym else ()):
                for _e in _pool:
                    _v = _entry_value(_e)
                    if _e.tag == _tag and _v not in ('', '-', '?'):
                        _net_name = _v
                        break
                if _net_name:
                    break
            if _net_name:
                break
        if not _net_name:
            _net_name = sn
        value  = _net_name
        lib_id = f'Ulticap:{_esc(sheet._lib_name_map.get(sn, sn))}'

        # Position/vis/size from WIRELABEL or LABEL tag
        _wl_a = None
        for _tag in ('WIRELABEL', 'LABEL'):
            _wl_a, _ = _find_attr(_tag)
            if _wl_a is not None:
                break
        if _wl_a is not None:
            ox, oy   = _rot_transform(_wl_a.dx, _wl_a.dy, comp.rotation)
            _wl_sdx, _wl_sdy = _uc_text_anchor_shift(comp.rotation, _wl_a.visibility, _wl_a.size)
            ox, oy   = ox + _wl_sdx, oy + _wl_sdy
            val_x    = tr.x(comp.x + ox)
            val_y    = tr.y(comp.y + oy)
            vis      = _wl_a.visibility
            val_hide = (vis & 128) == 0
            val_sz   = _f(_text_size_mm(_wl_a.size)) if _wl_a.size else _DEFAULT_TEXT_SIZE_MM
            _wh, _wv, _, _wa = _vis_justify(comp.rotation, vis)
            val_just = _justify_str(_wh, _wv)
            val_ang  = _wa
        else:
            val_x, val_y = _prop_pos('VALUE', _VALUE_DY_MM)
            val_ang, val_just, val_hide, val_sz = _prop_params('VALUE')
    else:
        _lib_override = sheet._lib_id_override.get(id(comp))
        if _lib_override is not None:
            lib_id = f'Ulticap:{_esc(_lib_override)}'
        else:
            lib_id = f'Ulticap:{_esc(sheet._lib_name_map.get(sn, sn))}'
        # Value: *C VALUE → *S VALUE default → '-'
        value = (_entry_value(_find_attr('VALUE')[0])
                 if _find_attr('VALUE')[0] is not None
                 else (sym.attributes.get('VALUE') if sym else None) or '-')
        val_x, val_y = _prop_pos('VALUE', _VALUE_DY_MM)
        val_ang, val_just, val_hide, val_sz = _prop_params('VALUE')

    # ── Fixed-position properties ─────────────────────────────────────────────
    footprint    = _entry_value(_find_attr('PKG_TYPE')[0]) if _find_attr('PKG_TYPE')[0] else ''
    _cx, _cy     = _comp_centre(comp, sym)
    _cxmm, _cymm = _f(tr.x(_cx)), _f(tr.y(_cy))

    # ── Unit number ───────────────────────────────────────────────────────────
    _gate_map = getattr(sheet, '_gate_unit_map', {})
    if sn in _gate_map and comp.refdes:
        rd = comp.refdes
        if rd and rd[-1].isalpha() and rd[-1].isupper():
            _unit_num = ord(rd[-1]) - ord('A') + 1
        else:
            _pin_fset = frozenset(ca.value for ca in comp.comp_attrs
                                  if ca.tag.startswith('#'))
            _unit_num = _gate_map[sn].get(_pin_fset, 1)
    else:
        _unit_num = 1

    # ── Symbol block ─────────────────────────────────────────────────────────
    lines = [
        '  (symbol',
        f'    (lib_id "{lib_id}")',
        f'    (at {_f(tr.x(_cx))} {_f(tr.y(_cy))} {angle})',
        *(('    (mirror y)',) if mirror_y else ()),
        f'    (unit {_unit_num})',
        '    (in_bom yes)',
        '    (on_board yes)',
        '    (dnp no)',
        '    (exclude_from_sim no)',
        f'    (uuid "{_uid()}")',
        f'    (property "Reference" "{_esc(refdes)}"',
        f'      (at {_f(ref_x)} {_f(ref_y)} {ref_ang})',
        f'      (effects (font (size {ref_sz} {ref_sz})){ref_just}'
        + (' (hide yes)' if ref_hide else '') + ')',
        '    )',
        f'    (property "Value" "{_esc(value)}"',
        f'      (at {_f(val_x)} {_f(val_y)} {val_ang})',
        f'      (effects (font (size {val_sz} {val_sz})){val_just}'
        + (' (hide yes)' if val_hide else '') + ')',
        '    )',
        f'    (property "Footprint" "{_esc(footprint)}"',
        f'      (at {_cxmm} {_cymm} 0)',
        f'      (effects (font (size {_DEFAULT_TEXT_SIZE_MM} {_DEFAULT_TEXT_SIZE_MM})) (hide yes))',
        '    )',
        '    (property "Datasheet" ""',
        f'      (at {_cxmm} {_cymm} 0)',
        f'      (effects (font (size {_DEFAULT_TEXT_SIZE_MM} {_DEFAULT_TEXT_SIZE_MM})) (hide yes))',
        '    )',
        '    (property "Description" ""',
        f'      (at {_cxmm} {_cymm} 0)',
        f'      (effects (font (size {_DEFAULT_TEXT_SIZE_MM} {_DEFAULT_TEXT_SIZE_MM})))',
        '    )',
    ]

    # ── Additional properties (non-power only) ────────────────────────────────
    if not _is_pwr:
        # DEVICE: emit using its own vis/position
        _dev_a, _ = _find_attr('DEVICE')
        if _dev_a is not None and (_dev_a.visibility & 128):
            _dh, _dv, _, _da = _vis_justify(comp.rotation, _dev_a.visibility)
            _dsz  = _f(_text_size_mm(_dev_a.size)) if _dev_a.size else val_sz
            _dox, _doy = _rot_transform(_dev_a.dx, _dev_a.dy, comp.rotation)
            _dsdx, _dsdy = _uc_text_anchor_shift(comp.rotation, _dev_a.visibility, _dev_a.size)
            _dox, _doy = _dox + _dsdx, _doy + _dsdy
            lines += [
                f'    (property "Device" "{_esc(_entry_value(_dev_a))}"',
                f'      (at {_f(tr.x(comp.x + _dox))} {_f(tr.y(comp.y + _doy))} {_da})',
                f'      (effects (font (size {_dsz} {_dsz})){_justify_str(_dh, _dv)})',
                '    )',
            ]

        # User-defined and text-overlay properties.
        # Covers TEXT=, TXT=, TEXT1=, TXT10=, and any user-defined tag not in
        # the reserved set. For each tag found in *S or *C:
        #   - *C entry takes priority (value, position, size, visibility)
        #   - *S entry is used when no *C override exists
        #   - Tags with vis=0 (neither value nor tag shown) are skipped
        _STANDARD_TAGS = frozenset({
            'REFDES', 'VALUE', 'DEVICE', 'PKG_TYPE', 'WIRELABEL', 'LABEL',
            'PINTYPE', 'SIGNAL', 'TRANSX', 'TRANSY',
            'PARTS', 'LABEL1', 'SPICE#', 'PORT', 'PINSWAP',
        })

        # Collect ordered list of non-standard tags, *S order as baseline
        _seen_tags = []
        _seen_set  = set()
        if sym:
            for sa in sym.sym_attrs:
                if sa.tag.startswith('#'): continue
                if sa.tag in _STANDARD_TAGS: continue
                if sa.tag not in _seen_set:
                    _seen_tags.append(sa.tag)
                    _seen_set.add(sa.tag)
        # Also include *C-only tags (not in *S at all)
        for ca in comp.comp_attrs:
            if ca.tag.startswith('#'): continue
            if ca.tag in _STANDARD_TAGS: continue
            if ca.tag not in _seen_set:
                _seen_tags.append(ca.tag)
                _seen_set.add(ca.tag)

        # Build *C lookup for quick access
        _c_by_tag: dict = {}
        for ca in comp.comp_attrs:
            if ca.tag not in _c_by_tag:   # first occurrence wins
                _c_by_tag[ca.tag] = []
            _c_by_tag[ca.tag].append(ca)

        _extra_idx = 0
        for _etag in _seen_tags:
            # Entries: list of *C attrs with this tag, or [*S attr] as fallback
            if _etag in _c_by_tag:
                _entries = _c_by_tag[_etag]   # *C overrides (may be multiple)
                _use_c   = True
            elif sym:
                # TXT with no *C override is exactly the *S default value that
                # _lib_sym_pins already drew as fixed graphic text inside the
                # shared lib symbol body (every placed instance shows it via
                # that shared graphic). Re-emitting it here as a per-instance
                # property would draw the same string a second time. Only a
                # genuine *C-level override (handled above, _use_c=True) needs
                # its own instance property, since the shared lib graphic
                # can't vary per instance.
                if _etag == 'TXT':
                    continue
                _entries = [sa for sa in sym.sym_attrs if sa.tag == _etag]
                _use_c   = False
            else:
                continue

            for _e in _entries:
                _vis   = _e.visibility
                _val   = _e.value if _use_c else _e.default_value
                # Emit all tags (including invisible ones) so the property
                # exists in the KiCad file; vis=0 entries get (hide yes).
                # Only skip if the entry has no value at all.
                if _val == '' and not (_vis & 192): continue
                _extra_idx += 1
                _eh, _ev, _, _ea = _vis_justify(comp.rotation, _vis)
                _esz   = _f(_text_size_mm(
                    _e.size, tune=(PIN_NAME_HEIGHT_TUNE if _etag == 'TXT' else None)
                )) if _e.size else _DEFAULT_TEXT_SIZE_MM
                _eox, _eoy = _rot_transform(_e.dx, _e.dy, comp.rotation)
                _esdx, _esdy = _uc_text_anchor_shift(comp.rotation, _vis, _e.size)
                _eox, _eoy = _eox + _esdx, _eoy + _esdy
                # Hide when neither value (bit 7) nor tag (bit 6) is shown
                _ehide = '' if (_vis & 128) else ' (hide yes)'
                # Property name: use tag directly, append index for duplicates
                _prop_name = f'{_esc(_etag)}_{_extra_idx}'
                # For TITLE only: substitute KiCad text variables for the four
                # named fields and PAGE/OF so the placed symbol stays live-linked
                # to Page Settings.  TITLE_REV and all other symbols use literal
                # values — TITLE_REV may carry different revision-specific data.
                _emit_val = (_TITLE_VAR_MAP.get(_etag.upper(), _val)
                             if sn == 'TITLE' else _val)
                lines += [
                    f'    (property "{_prop_name}" "{_esc(_emit_val)}"',
                    f'      (at {_f(tr.x(comp.x + _eox))} {_f(tr.y(comp.y + _eoy))} {_ea})',
                    f'      (effects (font (size {_esz} {_esz})){_justify_str(_eh, _ev)}{_ehide})',
                    '    )',
                ]

    # ── Pin uuid list ─────────────────────────────────────────────────────────
    if sym:
        _all_pins = getattr(sheet, '_gate_all_pins', {}).get(sn)
        if _all_pins:
            for _pnum in _all_pins:
                lines.append(f'    (pin "{_esc(_pnum)}" (uuid "{_uid()}"))')
        else:
            for _pin, _pnum, _pname, _pktype in _resolve_pins(sym, comp):
                lines.append(f'    (pin "{_esc(_pnum)}" (uuid "{_uid()}"))')
        for _sig_net, _sig_nums in getattr(sym, 'signal_pins', []):
            for _sig_pnum in _sig_nums:
                lines.append(f'    (pin "{_esc(_sig_pnum)}" (uuid "{_uid()}"))')

    # ── (instances) canonical reference ──────────────────────────────────────
    _inst_path = getattr(sheet, '_inst_path',
                         f'/{getattr(sheet, "_sch_uuid", "00000000-0000-0000-0000-000000000000")}')
    _proj = getattr(sheet, '_proj_name', 'schematic')
    lines += [
        '    (instances',
        f'      (project "{_esc(_proj)}"',
        f'        (path "{_inst_path}"',
        f'          (reference "{_esc(refdes)}")',
        f'          (unit {_unit_num})',
        '        )',
        '      )',
        '    )',
        '  )',
    ]

    # ── LABELn in *C → KiCad net labels at pin connection points ─────────────
    # LABEL/LABELn/WIRELABEL in *C whose value does not match any *S LABEL/WIRELABEL
    # default is a net label at a pin POE. Emit as KiCad (label ...).
    if sym and not _is_pwr:
        _s_label_vals = {sa.default_value for sa in sym.sym_attrs
                         if sa.tag in ('LABEL', 'WIRELABEL')}
        # Build set of known pin POEs from *S.  A *C LABEL/WIRELABEL at a pin POE
        # is a pin-name override handled by _resolve_pins — skip it here so it is
        # not also emitted as a standalone net label.
        _s_pin_poes = {(_pin_conn_point(p, sym)) for p in sym.pins}
        for ca in comp.comp_attrs:
            if not (ca.tag in ('LABEL', 'WIRELABEL') or
                    (ca.tag.startswith('LABEL') and ca.tag[5:].isdigit())):
                continue
            if ca.value in _s_label_vals:
                continue
            if not ca.value or ca.value in ('-', '?'):
                continue
            # Skip if (dx_poe, dy_poe) matches a *S pin POE: the value is a
            # pin-name override handled by _resolve_pins, not a standalone net label.
            if (ca.dx_poe, ca.dy_poe) in _s_pin_poes:
                continue
            # Position: label's own dx/dy (text anchor), not the pin POE
            _lox, _loy = _rot_transform(ca.dx, ca.dy, comp.rotation)
            _lsdx, _lsdy = _uc_text_anchor_shift(comp.rotation, ca.visibility, ca.size)
            _lox, _loy = _lox + _lsdx, _loy + _lsdy
            _lx  = _f(tr.x(comp.x + _lox))
            _ly  = _f(tr.y(comp.y + _loy))
            # Angle and justification: from the label's own vis byte
            _lh, _lv, _, _lang = _vis_justify(comp.rotation, ca.visibility)
            _ljust = _justify_str(_lh, _lv)
            _lsz   = _f(_text_size_mm(ca.size)) if ca.size else _DEFAULT_TEXT_SIZE_MM
            _lhide = ' (hide yes)' if (ca.visibility & 128) == 0 else ''
            lines += [
                f'  (label "{_esc(ca.value)}"',
                f'    (at {_lx} {_ly} {_lang})',
                f'    (effects (font (size {_lsz} {_lsz})){_ljust}{_lhide})',
                f'    (uuid "{_uid()}")',
                '  )',
            ]

    return lines


# ── title block extraction ─────────────────────────────────────────────────────
# _TITLE_FIELD_MAP / _TITLE_VAR_MAP — see the format-decode table block near
# the top of this file.


def _extract_title_block(sheet: Sheet) -> Dict[str, str]:
    """Scan the TITLE component and extract the four named KiCad title fields.

    Returns a dict with keys: title, rev, date, company (whichever are found).
    TITLE_REV is not scanned — its fields may carry revision-specific data that
    differs from the primary title and must not overwrite it.
    Both symbols are still rendered normally as placed symbols — this function
    is only responsible for populating the hidden KiCad (title_block ...) record.

    Field values are read from comp_attrs first (per-instance *C values), falling
    back to the sym_attr default_value.  Tags not in _TITLE_FIELD_MAP are skipped.
    Tag matching is case-insensitive to handle variants like 'Initial Date' vs
    'INITIAL_DATE' — the converter renders these as plain text without complaint;
    only exact-case matches populate the KiCad title block.
    """
    result: Dict[str, str] = {}

    def _collect(comp: Component) -> None:
        sym = sheet.symbols.get(comp.symbol_name)
        # Build a tag→value lookup from sym_attrs defaults first, then
        # override with comp_attrs per-instance values.
        merged: Dict[str, str] = {}
        if sym:
            for sa in sym.sym_attrs:
                tag_up = sa.tag.upper()
                if tag_up not in _TITLE_FIELD_MAP:
                    continue
                if not (sa.visibility & 0x7F) and not (sa.visibility & 0x80):
                    continue  # fully invisible — skip
                if sa.default_value and sa.default_value not in ('-', '?', '*', ''):
                    merged[tag_up] = sa.default_value
        for ca in comp.comp_attrs:
            tag_up = ca.tag.upper()
            if tag_up not in _TITLE_FIELD_MAP:
                continue
            if not (ca.visibility & 0x7F) and not (ca.visibility & 0x80):
                continue  # fully invisible — skip
            if ca.value and ca.value not in ('-', '?', '*', ''):
                merged[tag_up] = ca.value
        # Fill result, respecting TITLE-wins priority (don't overwrite)
        for tag_up, value in merged.items():
            kicad_key = _TITLE_FIELD_MAP[tag_up]
            if kicad_key not in result:
                result[kicad_key] = value

    # TITLE only — TITLE_REV is excluded (see _TITLE_FIELD_MAP rationale)
    for comp in sheet.components:
        if comp.symbol_name == 'TITLE':
            _collect(comp)
            break   # only one instance expected

    return result


# ── FILE= reference checker ────────────────────────────────────────────────────

def _build_subsheet_map(sheets) -> dict:
    """Build a lookup dict: normalised-uppercase-filename → Sheet.

    Used to resolve FILE= references when emitting (sheet ...) pin shapes.
    e.g.  'CHAP_9B.SCH' → <Sheet CHAP_9B>
    """
    result = {}
    for sh in sheets:
        if sh.name:
            result[Path(sh.name).name.upper()] = sh
    return result


def _sheet_sexp(comp: 'Component', sheet: 'Sheet', tr: '_Transform',
                subsheet_map: dict, sheet_elem_uuid: str) -> List[str]:
    """Emit a KiCad (sheet ...) block for a hierarchical sub-sheet placement.

    comp      : the *C instance with file_ref set
    sheet     : the parent (root) sheet
    tr        : coordinate transform for the parent sheet
    subsheet_map : normalised-filename → Sheet, for port shape lookup
    sheet_elem_uuid : pre-assigned uuid for this (sheet ...) element
    """
    sn  = comp.symbol_name
    sym = sheet.symbols.get(sn)

    # ── Box position and size ─────────────────────────────────────────────────
    # cx/cy is the bounding-box centre (same as the original code), so that the
    # KiCad pin stubs — which are computed relative to comp.x/comp.y — continue
    # to sit exactly on the box border.  Only the rendered size switches to the
    # actual polyline extents, which are always ≤ the bounding box and exclude
    # the label margins that caused overlap with adjacent symbol text.
    if sym:
        half_dx, half_dy = _rot_transform(sym.width / 2, sym.height / 2, comp.rotation)
        cx = comp.x + half_dx
        cy = comp.y + half_dy
        if sym.polylines:
            _all_px = [pt[0] for pl in sym.polylines for pt in pl.points]
            _all_py = [pt[1] for pl in sym.polylines for pt in pl.points]
            _box_w  = max(_all_px) - min(_all_px)   # units
            _box_h  = max(_all_py) - min(_all_py)   # units
        else:
            _box_w, _box_h = sym.width, sym.height  # no outline: use bbox
        w_mm = _f(_box_w * MM_PER_UNIT)
        h_mm = _f(_box_h * MM_PER_UNIT)
    else:
        cx, cy = comp.x, comp.y
        _box_w = _box_h = 200
        w_mm = h_mm = '10.16'   # fallback 200-unit box

    # ── FILE= attr: size and visibility ──────────────────────────────────────
    # *C first; fall back to *S when *C block has no attrs (e.g. bare *C line).
    _file_ca  = next((ca for ca in comp.comp_attrs if ca.tag == 'FILE'), None)
    _file_sa  = next((sa for sa in sym.sym_attrs  if sa.tag == 'FILE'), None) if sym else None
    _file_src = _file_ca if _file_ca is not None else _file_sa
    _fsz      = _f(_text_size_mm(_file_src.size)) if _file_src and _file_src.size else '0.889'
    _fvis     = _file_src.visibility if _file_src else 128
    _fhide    = ' (hide yes)' if not (_fvis & 128) else ''

    # ── Sheetname / Sheetfile values ──────────────────────────────────────────
    # file_ref is already normalised to have .SCH extension (kiuc_ascii.py)
    _file_ref  = comp.file_ref or ''
    _sheetname = Path(_file_ref).stem          # e.g. 'SUB1'
    _sheetfile = Path(_file_ref).with_suffix('.kicad_sch').name  # e.g. 'SUB1.kicad_sch'

    # ── Sheet top-left corner ────────────────────────────────────────────────
    # KiCad (sheet (at X Y)) expects the TOP-LEFT corner of the box.
    # Use polyline extents for size (excludes label margins); TL is the
    # polyline top-left in schematic coords so pin stubs land on the border.
    if sym and sym.polylines:
        _all_px2 = [pt[0] for pl in sym.polylines for pt in pl.points]
        _all_py2 = [pt[1] for pl in sym.polylines for pt in pl.points]
        _pl_xmin = min(_all_px2)
        _pl_ymax = max(_all_py2)
        _tl_ldx, _tl_ldy = _rot_transform(_pl_xmin, _pl_ymax, comp.rotation)
        _tl_x = _f(tr.x(comp.x + _tl_ldx))
        _tl_y = _f(tr.y(comp.y + _tl_ldy))
    else:
        _w_mm_f = _box_w * MM_PER_UNIT
        _h_mm_f = _box_h * MM_PER_UNIT
        _tl_x   = _f(tr.x(cx) - _w_mm_f / 2)
        _tl_y   = _f(tr.y(cy) - _h_mm_f / 2)

    # ── Label positions from Ulticap FILE= attribute (dx, dy) ────────────────
    # _file_src is already set above (*C first, *S fallback).
    # Sheetname uses the stored position and alignment exactly; Sheetfile shares
    # the same position and is always hidden (KiCad cross-reference only).
    if _file_src:
        _lbl_dx, _lbl_dy = _rot_transform(_file_src.dx, _file_src.dy, comp.rotation)
        _fsdx, _fsdy = _uc_text_anchor_shift(comp.rotation, _file_src.visibility, _file_src.size)
        _lbl_dx, _lbl_dy = _lbl_dx + _fsdx, _lbl_dy + _fsdy
        _name_x = _f(tr.x(comp.x + _lbl_dx))
        _name_y = _f(tr.y(comp.y + _lbl_dy))
        _lbl_h, _lbl_v, _, _ = _vis_justify(comp.rotation, _file_src.visibility)
    else:
        # No FILE= attr at all: fall back to box centre, no justify
        _name_x = _f(tr.x(cx))
        _name_y = _f(tr.y(cy))
        _lbl_h, _lbl_v = None, None
    _file_x = _name_x
    _file_y = _name_y

    # ── Sub-sheet port shape lookup: net_name → KiCad shape ──────────────────
    _sub_shapes: dict = {}
    _sub_sheet = subsheet_map.get(_file_ref.upper()) if subsheet_map else None
    if _sub_sheet:
        for sc in _sub_sheet.components:
            ssym = _sub_sheet.symbols.get(sc.symbol_name)
            if not is_port_sym(ssym):
                continue
            _lbl = next((ca.value for ca in sc.comp_attrs
                         if ca.tag in ('LABEL', 'WIRELABEL')
                         and ca.value not in ('?', '-', '')), None)
            if not _lbl:
                continue
            _pv = ssym.attributes.get('PORT', '')
            if not _pv:
                _dev = ssym.attributes.get('DEVICE', '').upper()
                for _pfx in ('PORT_BUS_', 'PORT_'):
                    if _dev.startswith(_pfx):
                        _pv = _dev[len(_pfx):]
                        break
            _sub_shapes[_lbl] = _PORT_SHAPE.get(_pv.upper(), 'passive')

    # ── Build pin map: conn_point → (net_name, pin_rotation) ─────────────────
    # Base from *S pins, overridden by *C LABEL/WIRELABEL via dx_poe/dy_poe match.
    _pin_map: dict = {}   # (conn_x, conn_y) → (net_name, pin_rotation)
    if sym:
        for _p in sym.pins:
            _conn = _pin_conn_point(_p, sym)
            _pin_map[_conn] = (_p.name, _p.pin_rotation)

    _seen_ca = set()
    for ca in comp.comp_attrs:
        if ca.tag not in ('LABEL', 'WIRELABEL'):
            continue
        if ca.value in ('?', '-', ''):
            continue
        _key = (ca.tag, ca.value, ca.dx, ca.dy,
                ca.dx_poe, ca.dy_poe, ca.size, ca.colour, ca.visibility)
        if _key in _seen_ca:
            continue
        _seen_ca.add(_key)
        _poe = (ca.dx_poe, ca.dy_poe)
        if _poe in _pin_map:
            _, _prot = _pin_map[_poe]
            _pin_map[_poe] = (ca.value, _prot)

    # ── Instances block ───────────────────────────────────────────────────────
    # Path is the PARENT sheet's full instance path — /{root_uuid} for root's
    # direct children, /{root_uuid}/{parent_elem_uuid} for grandchildren etc.
    # _inst_path is set on the sheet by _build_sch from the sheet_path parameter.
    # Page is the CHILD sheet's page number, pre-stamped by write_schematic.
    _inst_path = getattr(sheet, '_inst_path', f'/{getattr(sheet, "_sch_uuid", "00000000-0000-0000-0000-000000000000")}')
    _proj      = getattr(sheet, '_proj_name', 'schematic')
    _child_sh  = (subsheet_map or {}).get((comp.file_ref or '').upper())
    _page      = str(getattr(_child_sh, '_child_page_num',
                             getattr(sheet, '_page_num', 1)))

    # Border colour: from the first *S polyline group header (same colour field
    # as all other symbol body graphics).  Fall back to palette[5] if the
    # symbol has no polylines.
    _border_colour = sym.polylines[0].colour if (sym and sym.polylines) else 5
    _border_stroke = _colour_stroke(_border_colour, '0')

    # ── Assemble (sheet ...) block ────────────────────────────────────────────
    lines = [
        '  (sheet',
        f'    (at {_tl_x} {_tl_y})',
        f'    (size {w_mm} {h_mm})',
        f'    {_border_stroke}',
        f'    (property "Sheetname" "{_esc(_sheetname)}"',
        f'      (at {_name_x} {_name_y} 0)',
        f'      (effects (font (size {_fsz} {_fsz})){_justify_str(_lbl_h, _lbl_v)}{_fhide})',
        '    )',
        f'    (property "Sheetfile" "{_esc(_sheetfile)}"',
        f'      (at {_file_x} {_file_y} 0)',
        f'      (effects (font (size {_fsz} {_fsz})) (hide yes))',
        '    )',
    ]

    # ── Extra *S properties (e.g. TEXT=) not in *C and not FILE ──────────────
    # Any *S sym_attr that is not FILE, not a standard tag, and not already
    # overridden by a *C attr is emitted as an additional (property ...) on
    # the sheet box.  *C attrs take priority; *S provides the fallback.
    _SHEET_SKIP = frozenset({'FILE', 'REFDES', 'VALUE', 'DEVICE', 'PKG_TYPE',
                             'WIRELABEL', 'LABEL', 'PINTYPE', 'SIGNAL',
                             'TRANSX', 'TRANSY', 'PORT', 'PINSWAP'})
    _sheet_c_tags = {ca.tag for ca in comp.comp_attrs}
    _sheet_extra_idx = 0
    # *C non-FILE non-standard attrs first (preserving *C order)
    for _ca in comp.comp_attrs:
        if _ca.tag in _SHEET_SKIP: continue
        _sheet_extra_idx += 1
        _vis = _ca.visibility
        _eox, _eoy = _rot_transform(_ca.dx, _ca.dy, comp.rotation)
        _csdx, _csdy = _uc_text_anchor_shift(comp.rotation, _vis, _ca.size)
        _eox, _eoy = _eox + _csdx, _eoy + _csdy
        _eh, _ev, _, _ea = _vis_justify(comp.rotation, _vis)
        _esz  = _f(_text_size_mm(_ca.size)) if _ca.size else _fsz
        _ehide = '' if (_vis & 128) else ' (hide yes)'
        _pname = f'{_esc(_ca.tag)}_{_sheet_extra_idx}'
        lines += [
            f'    (property "{_pname}" "{_esc(_ca.value)}"',
            f'      (at {_f(tr.x(comp.x + _eox))} {_f(tr.y(comp.y + _eoy))} {_ea})',
            f'      (effects (font (size {_esz} {_esz})){_justify_str(_eh, _ev)}{_ehide})',
            '    )',
        ]
    # *S attrs not already covered by *C
    if sym:
        for _sa in sym.sym_attrs:
            if _sa.tag in _SHEET_SKIP: continue
            if _sa.tag in _sheet_c_tags: continue   # *C already emitted it
            if _sa.default_value == '' and not (_sa.visibility & 192): continue
            _sheet_extra_idx += 1
            _vis = _sa.visibility
            _eox, _eoy = _rot_transform(_sa.dx, _sa.dy, comp.rotation)
            _ssdx, _ssdy = _uc_text_anchor_shift(comp.rotation, _vis, _sa.size)
            _eox, _eoy = _eox + _ssdx, _eoy + _ssdy
            _eh, _ev, _, _ea = _vis_justify(comp.rotation, _vis)
            _esz  = _f(_text_size_mm(_sa.size)) if _sa.size else _fsz
            _ehide = '' if (_vis & 128) else ' (hide yes)'
            _pname = f'{_esc(_sa.tag)}_{_sheet_extra_idx}'
            lines += [
                f'    (property "{_pname}" "{_esc(_sa.default_value)}"',
                f'      (at {_f(tr.x(comp.x + _eox))} {_f(tr.y(comp.y + _eoy))} {_ea})',
                f'      (effects (font (size {_esz} {_esz})){_justify_str(_eh, _ev)}{_ehide})',
                '    )',
            ]

    # ── Pins ──────────────────────────────────────────────────────────────────
    for (_conn_x, _conn_y), (_net_name, _pin_rot) in _pin_map.items():
        if not _net_name or _net_name in ('?', '-', ''):
            continue
        # Wire connection point in schematic coords
        _rdx, _rdy = _rot_transform(_conn_x, _conn_y, comp.rotation)
        _px = _f(tr.x(comp.x + _rdx))
        _py = _f(tr.y(comp.y + _rdy))
        # KiCad pin angle: direction stub exits the box
        _dir = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}[_pin_rot & 3]
        _rdx2, _rdy2 = _rot_transform(_dir[0], _dir[1], comp.rotation)
        _pang = {(1, 0): 0, (0, 1): 90, (-1, 0): 180, (0, -1): 270}.get((_rdx2, _rdy2), 0)
        _pshape = _sub_shapes.get(_net_name, 'passive')
        # Sheet-pin name must exactly match the hierlabel and bus label text.
        _plt = getattr(sheet, '_port_label_text', {})
        _safe_pin_name = _plt.get(_net_name) or _sanitize_port_name(
            _net_name, getattr(sheet, '_port_name_map', None))
        lines += [
            f'    (pin "{_esc(_safe_pin_name)}" {_pshape}',
            f'      (at {_px} {_py} {_pang})',
            f'      (uuid "{_uid()}")',
            '    )',
        ]

    lines += [
        f'    (uuid "{sheet_elem_uuid}")',
        '    (instances',
        f'      (project "{_esc(_proj)}"',
        f'        (path "{_inst_path}"',
        f'          (page "{_page}")',
        '        )',
        '      )',
        '    )',
        '  )',
    ]

    # ── Stub wires/buses bridging bbox edge → polyline edge ───────────────────
    # Ulticap draws pin stubs as separate lines from the bbox edge (wire
    # connection point) to the polyline edge.  KiCad has no such concept;
    # we emit explicit (wire) or (bus) segments to restore the connection.
    stub_lines: List[str] = []
    if sym:
        for _p in sym.pins:
            _plen = _pin_length(_p, sym)
            if _plen == 0:
                continue   # pin sits exactly on polyline edge; no stub needed
            # Connection point (bbox edge) in schematic coords
            _cx0, _cy0 = _pin_conn_point(_p, sym)
            _rdx0, _rdy0 = _rot_transform(_cx0, _cy0, comp.rotation)
            _wx1 = _f(tr.x(comp.x + _rdx0))
            _wy1 = _f(tr.y(comp.y + _rdy0))
            # Stub far end (polyline edge) in schematic coords
            # stub points AWAY from body: move _plen units in pin_rotation direction
            _dirs = {0: (1, 0), 1: (0, 1), 2: (-1, 0), 3: (0, -1)}
            _sd = _dirs[_p.pin_rotation & 3]
            _ex = _cx0 - _sd[0] * _plen   # move toward bbox edge
            _ey = _cy0 - _sd[1] * _plen
            _rdx1, _rdy1 = _rot_transform(_ex, _ey, comp.rotation)
            _wx2 = _f(tr.x(comp.x + _rdx1))
            _wy2 = _f(tr.y(comp.y + _rdy1))
            # Wire or bus based on pin_format
            _seg_type = 'bus' if _p.pin_format == 9 else 'wire'
            _stub_col = _KICAD_COL_BUS if _p.pin_format == 9 else _KICAD_COL_WIRE
            _stub_stroke = (f'(stroke (width 0) (type default) {_col(_stub_col)})'
                           if not USE_KICAD_DEFAULT_COLORS
                           else '(stroke (width 0) (type default))')
            stub_lines += [
                f'  ({_seg_type}',
                f'    (pts (xy {_wx1} {_wy1}) (xy {_wx2} {_wy2}))',
                f'    {_stub_stroke}',
                f'    (uuid "{_uid()}")',
                '  )',
            ]

    return lines, stub_lines


def check_missing_sheets(sheets: List[Sheet]) -> List[str]:
    """
    Return list of warning strings for FILE= references not covered by
    the provided sheet list.
    """
    provided = {Path(s.name).name.upper() for s in sheets}
    warnings = []
    for sheet in sheets:
        for comp in sheet.components:
            ref = comp.file_ref
            if ref and Path(ref).name.upper() not in provided:
                rd = comp.refdes or comp.symbol_name
                warnings.append(
                    f"Sub-sheet '{ref}' referenced by {rd} in {sheet.name} "
                    f"was not provided — hierarchical design may be incomplete."
                )
    return warnings


def check_pkg_type_consistency(sheets: List[Sheet]) -> List[str]:
    """Return warnings for multi-gate components whose gates have inconsistent
    PKG_TYPE (footprint) values — a common source-schematic typo that KiCad
    flags as 'different_unit_footprint' after conversion.

    Groups all placed components by their base refdes (the refdes with any
    trailing gate letter A–Z stripped, e.g. 'U3C' → 'U3').  If two or more
    gates of the same component have non-empty, differing PKG_TYPE values,
    a warning is returned listing the conflict.
    """
    import re as _re
    _GATE_RE = _re.compile(r'^(.*?)([A-Z])$')

    # base_refdes → dict of gate_refdes → (pkg_type, sheet_name)
    by_base: dict = {}
    for sheet in sheets:
        for comp in sheet.components:
            rd = comp.refdes
            if not rd:
                continue
            pkg = comp.attributes.get('PKG_TYPE', '').strip()
            if not pkg:
                continue
            m = _GATE_RE.match(rd)
            base = m.group(1) if (m and m.group(1)) else rd
            by_base.setdefault(base, {})[rd] = (pkg, sheet.name)

    warnings = []
    for base, gates in sorted(by_base.items()):
        pkgs = {g: pkg for g, (pkg, _) in gates.items()}
        unique = set(pkgs.values())
        if len(unique) > 1:
            detail = ', '.join(
                f'{g}={p!r}' for g, p in sorted(pkgs.items()))
            warnings.append(
                f"PKG_TYPE mismatch for {base}: {detail} "
                f"— possible typo in source schematic"
            )
    return warnings


# ── wire / junction / text helpers ────────────────────────────────────────────

def _wire_sexp(w: Wire, tr: _Transform,
               ov_x1: float = None, ov_y1: float = None,
               ov_x2: float = None, ov_y2: float = None) -> List[str]:
    """Emit a (wire) or (bus) element.

    ov_x1/ov_y1/ov_x2/ov_y2 optionally override one or both endpoints with
    Ulticap-unit coordinates (may be fractional) different from w.x1/y1/x2/y2.
    Used to shorten a wire by a perpendicular-bus-entry stub length without
    ever mutating the underlying Wire model object — every other piece of
    code that reads sheet.wires (bus net-name collection, bus network
    union-find, etc.) must keep seeing the original, untouched coordinates.
    """
    wire_type = 'bus' if w.is_bus else 'wire'
    x1u = w.x1 if ov_x1 is None else ov_x1
    y1u = w.y1 if ov_y1 is None else ov_y1
    x2u = w.x2 if ov_x2 is None else ov_x2
    y2u = w.y2 if ov_y2 is None else ov_y2
    _wcol = _KICAD_COL_BUS if w.is_bus else _KICAD_COL_WIRE
    if not USE_KICAD_DEFAULT_COLORS:
        _stroke = f'(stroke (width 0) (type default) {_col(_wcol)})'
    else:
        _stroke = '(stroke (width 0) (type default))'
    return [
        f'  ({wire_type}',
        f'    (pts {tr.xy(x1u, y1u)} {tr.xy(x2u, y2u)})',
        f'    {_stroke}',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


def _interior_junction_points(w: Wire, junction_pts) -> List[Tuple[int, int]]:
    """Junction coordinates lying strictly on the interior of wire w -- same
    line, strictly between its endpoints, excluding the endpoints themselves.

    Splitting a wire at these points mirrors what KiCad's own editor produces
    when a wire is drawn as one continuous run and a junction is added
    partway along it: the run becomes several separate (wire) objects, one
    per junction-bounded piece. Without this, a label whose POE sits on the
    interior of an otherwise-unsplit straight wire that also carries an
    unrelated junction elsewhere along its own length triggers KiCad's
    'label_multiple_wires' ERC warning as a false positive -- confirmed
    empirically (see TT87W-2 5VSW/VBUS investigation) not to occur once the
    wire is pre-split this way, and not to occur at all for ordinary corners
    (two wires of different orientation meeting at a bend, no junction
    needed to disambiguate those).

    Returned points are sorted by distance from (w.x1, w.y1) so callers can
    walk the chain start -> split points -> end in order.
    """
    x1, y1, x2, y2 = w.x1, w.y1, w.x2, w.y2
    pts = []
    for (jx, jy) in junction_pts:
        if (jx, jy) == (x1, y1) or (jx, jy) == (x2, y2):
            continue
        cross = (jx - x1) * (y2 - y1) - (jy - y1) * (x2 - x1)
        if cross != 0:
            continue
        if min(x1, x2) <= jx <= max(x1, x2) and min(y1, y2) <= jy <= max(y1, y2):
            pts.append((jx, jy))
    pts.sort(key=lambda p: (p[0] - x1) ** 2 + (p[1] - y1) ** 2)
    return pts


def _junction_sexp(j: Junction, tr: _Transform) -> List[str]:
    # Ulticap junction sizes (1 unit = 0.0508 mm):
    #   wire-to-wire:      radius 6  -> diameter 12 units = 0.6096 mm
    #   wire-to-bus / bus: radius 8  -> diameter 16 units = 0.8128 mm
    _diam = _f(16 * MM_PER_UNIT if j.is_bus_entry else 12 * MM_PER_UNIT)
    _color = (f'    {_col(_KICAD_COL_JUNCTION)}' if not USE_KICAD_DEFAULT_COLORS
             else '    (color 0 0 0 0)')
    return [
        '  (junction',
        f'    (at {_f(tr.x(j.x))} {_f(tr.y(j.y))})',
        f'    (diameter {_diam})',
        _color,
        f'    (uuid "{_uid()}")',
        '  )',
    ]


def _point_on_ortho_segment(px: int, py: int, x1: int, y1: int,
                            x2: int, y2: int) -> bool:
    """True if integer point (px,py) lies exactly on the axis-aligned segment
    (x1,y1)→(x2,y2).  Diagonal segments always return False.
    """
    if x1 == x2:   # vertical
        return px == x1 and min(y1, y2) <= py <= max(y1, y2)
    elif y1 == y2: # horizontal
        return py == y1 and min(x1, x2) <= px <= max(x1, x2)
    return False


def _bus_entry_sexp(j: Junction, tr: _Transform,
                    sx: float = 0.0, sy: float = 0.0) -> List[str]:
    """Emit a KiCad (bus_entry) for a junction that connects a wire to a bus.

    (at x y) is the bus-side point of the entry.
    (size sx sy) = KiCad(wire_pt) - KiCad(bus_pt):
      • (±sx, 0) or (0, ±sy) — single-axis stub mimicking a perpendicular
        Ulticap wire-to-bus connection (no diagonal in the source data).
        The matching wire is shortened by the same amount at emission time
        so the two endpoints stay exactly coincident.  Undocumented in the
        KiCad GUI (which only ever draws 45° entries) but spec-legal — size
        is just a free 2D vector — and verified against both the generated
        netlist and ERC in KiCad 9.0.7.
      • (±sx, ±sy) with |sx|==|sy| — diagonal stub from a slanted Ulticap
        wire-to-bus segment.
      • (0, 0) — last-resort fallback when neither of the above could be
        resolved (e.g. the matching wire was too short to shorten safely,
        or the shortened point would land on unrelated wire/bus geometry).
        KiCad treats this as electrically coincident but ERC flags it as
        "Unconnected wire to bus entry" / "Unconnected wire endpoint" —
        confirmed in KiCad 9.0.7. Kept only as a graceful-degradation path.
    """
    return [
        '  (bus_entry',
        f'    (at {_f(tr.x(j.x))} {_f(tr.y(j.y))})',
        f'    (size {_f(sx)} {_f(sy)})',
        '    (stroke',
        '      (width 0)',
        '      (type default)',
        f'      {_col(_KICAD_COL_STUB)}',
        '    )',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


# ── bus alias / label helpers ──────────────────────────────────────────────────

import re as _re
_VECTOR_RE = _re.compile(r'^(.*?)(\d+)$')
# Matches KiCad/Ulticap vector-bus syntax: PREFIX[M..N], e.g. 'RC[0..2]',
# 'P1.[0..7]'. Used to recognise a hierarchical port name that is already in
# vector-range form (as opposed to a non-range group name like 'RA[125]').
_VECTOR_BRACKET_RE = _re.compile(r'^(.+)\[(\d+)\.\.(\d+)\]$')
# Matches Ulticap's concatenated-digit group-bus syntax: PREFIX[ddd...], e.g.
# 'RA[125]' for the group {RA1, RA2, RA5}. Each character inside the brackets
# is one decimal digit of a member index -- this is only a valid encoding of
# *names* (handled in _group_bracket_indices below), not yet validated against
# any particular name set.
_GROUP_BRACKET_RE = _re.compile(r'^(.+)\[(\d+)\]$')


def _group_bracket_indices(port_name: str, local_names: List[str]) -> bool:
    """True if *port_name* is Ulticap concatenated-digit group syntax
    (PREFIX[ddd...]) whose digits, as a set, exactly match the numeric
    suffixes of *local_names* under the same PREFIX.

    e.g. port_name='RA[125]', local_names=['RA1','RA2','RA5']
         -> digits {1,2,5} == suffixes {1,2,5} -> True

    Returns False if port_name isn't in PREFIX[ddd...] form, or if
    *local_names* is non-empty and its digit set doesn't match port_indices
    (e.g. a name like 'DI_HF[1&2]' contains a non-digit character and never
    matches this form at all).

    If local_names is empty (an orphan network with no locally-resolvable
    signal names), there is nothing to contradict the port name, so it is
    accepted as-is -- this is exactly the case (e.g. root's 'RA[125]'
    network) that needs the port name to supply a label at all.
    """
    m = _GROUP_BRACKET_RE.match(port_name)
    if not m:
        return False
    prefix, digits = m.group(1), m.group(2)
    port_indices = {int(d) for d in digits}

    if not local_names:
        return True

    local_indices = set()
    for name in local_names:
        vm = _VECTOR_RE.match(name)
        if not vm or vm.group(1) != prefix:
            return False   # a local name doesn't match PREFIX<digit>
        local_indices.add(int(vm.group(2)))

    return port_indices == local_indices


# Regex detecting any bracket-containing name that is NOT yet PREFIX[M..N].
# Used to decide whether _sanitize_port_name() must rewrite it.
_INVALID_BRACKET_RE = _re.compile(r'\[|\]')


def _sanitize_port_name(name: str, used_names: set = None) -> str:
    """Return a KiCad-legal plain-name form of a hierarchical port/pin name.

    KiCad forbids '[' and ']' in plain signal names (net labels, port names,
    hierarchical labels) unless the whole name is valid vector-bus syntax
    PREFIX[M..N].  Ulticap uses bracket notation for group buses too, e.g.
    'RA[125]' (digits = member indices) or 'DI_HF[1&2]' (non-digit separator).

    Rules applied in order:
      1. No brackets → return as-is.
      2. Already valid PREFIX[M..N] → return as-is.
      3. PREFIX[<digits only>] (Ulticap concatenated-index form, e.g. 'RA[125]')
         → convert to PREFIX[min..max] using the individual digit characters as
         indices:  RA[125] → indices {1,2,5} → RA[1..5].
      4. Anything else with brackets (e.g. 'DI_HF[1&2]') → strip brackets and
         any non-word-non-dot character inside them, keep just the surrounding
         text plus a sanitized inner part converted to [M..N] where possible,
         falling back to replacing '[...]' with '_' if no numeric range can be
         derived.

    If *used_names* is provided and the sanitized name already appears in it
    (as a genuinely DIFFERENT original name), a numeric suffix '_2', '_3', ...
    is appended until a free name is found.
    """
    if not _INVALID_BRACKET_RE.search(name):
        return name                        # rule 1: no brackets → fine as-is

    if _VECTOR_BRACKET_RE.match(name):
        return name                        # rule 2: already PREFIX[M..N]

    # Try rule 3: PREFIX[<all-digits>]
    m = _GROUP_BRACKET_RE.match(name)
    if m:
        prefix, digits = m.group(1), m.group(2)
        indices = sorted({int(d) for d in digits})
        candidate = f'{prefix}[{indices[0]}..{indices[-1]}]'
    else:
        # Rule 4: extract numeric digits from inside brackets, fall back to '_'
        inner = _re.search(r'\[([^\]]*)\]', name)
        nums = _re.findall(r'\d+', inner.group(1)) if inner else []
        prefix = _re.sub(r'\[.*?\]', '', name)       # strip all [...]
        if len(nums) >= 2:
            candidate = f'{prefix}[{min(int(n) for n in nums)}..{max(int(n) for n in nums)}]'
        elif len(nums) == 1:
            candidate = f'{prefix}[{nums[0]}..{nums[0]}]'
        else:
            candidate = prefix.rstrip('_') + '_'     # last resort

    # Collision check: if used_names supplied and name already taken by a
    # *different* original name, append numeric suffix.
    if used_names is not None:
        base = candidate
        n = 2
        while candidate in used_names and used_names[candidate] != name:
            candidate = f'{base}_{n}'
            n += 1
        # Register this mapping so subsequent calls can detect further conflicts
        used_names[candidate] = name

    return candidate


def _wire_networks(sheet: 'Sheet') -> List[List[Tuple[int, int, int, int]]]:
    """Group the sheet's plain (non-bus) wire segments into connected networks.

    Same connectivity rule as _bus_networks (shared endpoints, or a junction
    mediating a mid-segment T-junction) applied to plain wires instead of bus
    segments. Bus segments never participate here -- a label whose POE is on
    a bus, or at a bus-entry junction, is handled by its own dedicated branch
    before wire-network lookup is ever attempted, and mixing the two pools
    would risk snapping a wire label onto an unrelated bus or vice versa.

    Returns a list of segment-lists, one per connected network. Order is
    not significant but is deterministic for a given input.
    """
    wire_segs = [(w.x1, w.y1, w.x2, w.y2) for w in sheet.wires if not w.is_bus]
    n = len(wire_segs)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    endpoint_map: dict = {}
    for i, (x1, y1, x2, y2) in enumerate(wire_segs):
        for pt in ((x1, y1), (x2, y2)):
            endpoint_map.setdefault(pt, []).append(i)
    for idxs in endpoint_map.values():
        for i in idxs[1:]:
            union(idxs[0], i)

    for j in sheet.junctions:
        touching = [i for i, s in enumerate(wire_segs)
                    if _point_on_ortho_segment(j.x, j.y, *s)]
        for i in touching[1:]:
            union(touching[0], i)

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(wire_segs[i])
    return list(groups.values())


def _nearest_point_on_ortho_segment(px: int, py: int,
                                     x1: int, y1: int, x2: int, y2: int
                                     ) -> Tuple[int, int, float]:
    """Nearest point to (px, py) on an orthogonal segment, clamped to its span.

    Returns (nx, ny, distance). For a horizontal segment (y1==y2), nx is px
    clamped to the segment's X span and ny is the segment's own Y; symmetric
    for a vertical segment. Works whether the nearest point lands on the
    segment's interior (a true perpendicular foot) or is clamped to one of
    its endpoints (a corner/junction) -- either way the result is a valid
    point on the wire, and the returned distance is the plain Euclidean
    distance to it, needing no special-casing between the two.
    """
    if y1 == y2:                      # horizontal segment
        lo, hi = (x1, x2) if x1 <= x2 else (x2, x1)
        nx = min(max(px, lo), hi)
        ny = y1
    else:                              # vertical segment
        lo, hi = (y1, y2) if y1 <= y2 else (y2, y1)
        nx = x1
        ny = min(max(py, lo), hi)
    dist = math.hypot(px - nx, py - ny)
    return nx, ny, dist


def _bus_networks(sheet: 'Sheet') -> List[List[Tuple[int, int, int, int]]]:
    """Group the sheet's bus wire segments into connected networks.

    Two segments belong to the same network if they:
      - share an endpoint, or
      - both pass through a common junction point (mid-segment T-junctions,
        where a bus segment's endpoint lands partway along another bus
        segment — common at bus corners).

    Segments that merely cross at a point with NO junction recorded there
    are NOT merged — a crossing without a junction does not connect buses.

    Returns a list of segment-lists, one per connected network. Order is
    not significant but is deterministic for a given input.
    """
    bus_segs = [(w.x1, w.y1, w.x2, w.y2) for w in sheet.wires if w.is_bus]
    n = len(bus_segs)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Endpoint-sharing
    endpoint_map: dict = {}
    for i, (x1, y1, x2, y2) in enumerate(bus_segs):
        for pt in ((x1, y1), (x2, y2)):
            endpoint_map.setdefault(pt, []).append(i)
    for idxs in endpoint_map.values():
        for i in idxs[1:]:
            union(idxs[0], i)

    # Junction-mediated mid-segment connections
    for j in sheet.junctions:
        touching = [i for i, s in enumerate(bus_segs)
                    if _point_on_ortho_segment(j.x, j.y, *s)]
        for i in touching[1:]:
            union(touching[0], i)

    groups: dict = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(bus_segs[i])
    return list(groups.values())


def _bus_network_port_name(sheet: 'Sheet',
                            net_segs: List[Tuple[int, int, int, int]]) -> Optional[str]:
    """Return the authoritative bus name for *net_segs* from a connected
    hierarchical port, or None if the network doesn't terminate at one.

    A bus network that crosses a sheet boundary is identified on each side
    by a named port:
      - On the sheet containing the placement (e.g. root), the port is a pin
        on a hierarchical sheet-symbol (Symbol.pins of a placed *C with
        file_ref set), and the authoritative name is pin.name.
      - On the subsheet itself, the port is a PORT_BUS_* component, and the
        authoritative name is its LABEL attribute.

    In both cases the bus-side connection point of the port's pin is computed
    the same way _sheet_sexp computes stub endpoints (_pin_conn_point +
    _rot_transform). If that point coincides with an endpoint of any segment
    in net_segs, the port's name is authoritative for this network — it MUST
    match exactly on both sides of the connection for KiCad to join the bus.

    Only bus-type pins (pin_format == 9) are considered, since wire-type
    stubs are unrelated to bus connectivity.
    """
    net_endpoints = set()
    for x1, y1, x2, y2 in net_segs:
        net_endpoints.add((x1, y1))
        net_endpoints.add((x2, y2))

    for comp in sheet.components:
        sym = sheet.symbols.get(comp.symbol_name)
        if sym is None:
            continue

        if _is_hier_sheet(comp):
            # Hierarchical sheet-symbol placed on this sheet: its pins are
            # the sheet's ports as seen from this side.
            #
            # A *C-level LABEL or WIRELABEL attribute on this specific placed
            # instance renames the port for this instance only -- the same
            # override mechanism _resolve_pins already applies for ordinary
            # symbol pins, matched by (dx_poe, dy_poe) against the pin's own
            # connection point. This MUST take priority over the *S-default
            # pin.name: otherwise a renamed port on this sheet and its
            # PORT_BUS_* counterpart on the subsheet (which already resolves
            # its own *C/*S name correctly via comp.attributes below) end up
            # with two different authoritative names for what is meant to be
            # a single bus crossing the sheet boundary -- both get written
            # into the shared, cross-sheet _port_label_text table under
            # different keys, producing two different bus_alias/label texts
            # for the same net instead of one.
            _c_name_by_poe = {
                (ca.dx_poe, ca.dy_poe): ca.value
                for ca in comp.comp_attrs
                if ca.tag in ('LABEL', 'WIRELABEL')
            }
            for pin in sym.pins:
                if pin.pin_format != 9:
                    continue   # not a bus-type pin
                poe = _pin_conn_point(pin, sym)
                name = _c_name_by_poe.get(poe) or pin.name
                if not name:
                    continue
                rdx, rdy = _rot_transform(poe[0], poe[1], comp.rotation)
                pt = (comp.x + rdx, comp.y + rdy)
                if pt in net_endpoints:
                    return name
        else:
            # PORT_BUS_* component: this sheet's own hierarchical port,
            # named via its LABEL attribute.
            if 'PORT' not in comp.symbol_name.upper():
                continue
            label = comp.attributes.get('LABEL')
            if not label:
                continue
            for pin in sym.pins:
                if pin.pin_format != 9:
                    continue
                cx0, cy0 = _pin_conn_point(pin, sym)
                rdx, rdy = _rot_transform(cx0, cy0, comp.rotation)
                pt = (comp.x + rdx, comp.y + rdy)
                if pt in net_endpoints:
                    return label

    return None


def _bus_network_label_text(sheet: 'Sheet',
                             net_segs: List[Tuple[int, int, int, int]]) -> Optional[str]:
    """The exact text KiCad expects for *net_segs*' bus label, hierarchical
    label, and sheet pin -- the single source for the {NAME}-wrapping
    decision (PREFIX[M..N] verbatim for vector buses; '{NAME}' for
    everything else), read from the shared, cross-sheet _port_label_text
    table (see write_schematic).

    Every text-anchor call site that names this bus MUST go through this
    function rather than re-deriving its own wrapping locally: the two are
    then free to silently disagree (as happened when the annotation-driven
    path in write_kicad_sch's free-text-annotation loop was never wired up
    to it -- a group bus's *A LABEL= annotation rendered unwrapped 'CTRL'
    while its hierarchical port correctly rendered '{CTRL}', producing a
    bus/net connection KiCad's ERC rejects).

    Returns None if this network has no resolvable hierarchical port name
    (e.g. a purely local bus with no cross-sheet connection) -- callers fall
    back to their own local derivation in that case, same as before this
    function existed.
    """
    port_name = _bus_network_port_name(sheet, net_segs)
    if port_name is None:
        return None
    return getattr(sheet, '_port_label_text', {}).get(port_name)



def _collect_bus_net_names(sheet: 'Sheet',
                            net_segs: List[Tuple[int, int, int, int]]) -> List[str]:
    """Return sorted list of unique net names connected to bus *net_segs*.

    net_segs is one connected bus network as returned by _bus_networks();
    only wire-to-bus junctions that lie on one of these segments contribute
    a net name.
    """
    if not net_segs:
        return []

    # net_id → label text via mid-segment POE match
    label_anns = [a for a in sheet.annotations if a.tag == 'LABEL']
    net_label: dict = {}
    for a in label_anns:
        for w in sheet.wires:
            if not w.is_bus and _point_on_ortho_segment(
                    a.x_poe, a.y_poe, w.x1, w.y1, w.x2, w.y2):
                net_label[w.net_id] = a.text
                break

    # point → net_id
    point_net: dict = {}
    for w in sheet.wires:
        if not w.is_bus:
            for pt in ((w.x1, w.y1), (w.x2, w.y2)):
                point_net[pt] = w.net_id

    names = {net_label[point_net[pt]]
             for j in sheet.junctions
             if j.is_bus_entry
             and any(_point_on_ortho_segment(j.x, j.y, *s) for s in net_segs)
             for pt in ((j.x, j.y),)
             if pt in point_net and point_net[pt] in net_label}
    return sorted(names)


def _try_vector_bus(names: List[str]):
    """Test whether *names* form a VECTOR BUS.

    Returns (prefix, M, N) if all names match <prefix><k> with k in [M..N]
    (subset allowed, no name outside range permitted).
    Returns None if the set does not qualify.
    """
    if not names:
        return None
    parsed = []
    for name in names:
        m = _VECTOR_RE.match(name)
        if not m:
            return None          # name has no numeric suffix → GROUP BUS
        parsed.append((m.group(1), int(m.group(2))))

    prefixes = {p for p, _ in parsed}
    if len(prefixes) != 1:
        return None              # multiple prefixes → GROUP BUS

    prefix = prefixes.pop()
    indices = [idx for _, idx in parsed]
    M, N = min(indices), max(indices)
    if M < 0:
        return None              # negative index → GROUP BUS (shouldn't occur)

    # Verify no name exists outside [M..N]  (guaranteed by definition here,
    # but validate that no duplicate prefix+index collision exists)
    if len(set(indices)) != len(indices):
        return None              # duplicate index → GROUP BUS

    return prefix, M, N


def _bus_label_position(sheet: 'Sheet',
                         net_segs: List[Tuple[int, int, int, int]]):
    """Return Ulticap (x, y) for placing the bus label for one network.

    Prefers: just right of the leftmost bus-to-bus junction on the topmost
    horizontal bus segment of *net_segs*.  Falls back to the start of the
    first segment in *net_segs*.

    Avoids positions where a non-bus-network wire or bus crosses the chosen
    segment perpendicularly at the label's grid position — such a crossing
    would cause KiCad to accidentally connect the label to that wire too.
    Steps through grid positions along the segment to find a clear spot.
    """
    if not net_segs:
        return 0, 0

    net_seg_set = set(net_segs)

    def _has_crossing_wire(px: int, py: int, is_horizontal: bool) -> bool:
        """True if any wire/bus NOT in this network crosses through (px,py)
        in the direction perpendicular to the bus."""
        for w in sheet.wires:
            seg = (w.x1, w.y1, w.x2, w.y2)
            if seg in net_seg_set:
                continue
            if is_horizontal:
                # Looking for vertical wires through px at py
                if w.x1 == w.x2 == px:
                    if min(w.y1, w.y2) <= py <= max(w.y1, w.y2):
                        return True
            else:
                # Looking for horizontal wires through py at px
                if w.y1 == w.y2 == py:
                    if min(w.x1, w.x2) <= px <= max(w.x1, w.x2):
                        return True
        return False

    def _first_free(candidates: List[int], fixed: int,
                    is_horizontal: bool,
                    net_segs_set: set) -> Optional[int]:
        """Return first candidate position with no crossing wire, or None."""
        for c in candidates:
            px, py = (c, fixed) if is_horizontal else (fixed, c)
            if not _has_crossing_wire(px, py, is_horizontal):
                return c
        return None

    def _safe_fallback(net_segs: List[Tuple[int,int,int,int]],
                       is_horizontal: bool) -> Tuple[int,int]:
        """Return a guaranteed-safe label position using the priority chain:
           1. POE point (bus_entry junction) on a horizontal/vertical segment
           2. Corner (endpoint shared by 2+ segments)
           3. Any segment endpoint
        All three are by definition electrical bus points — a crossing wire
        there would have generated a junction, making it a connected point
        rather than an unrelated crossing.
        """
        from collections import Counter

        # 1. POE points: bus_entry junctions lying on this network
        for j in sheet.junctions:
            if not j.is_bus_entry:
                continue
            for x1,y1,x2,y2 in net_segs:
                if not _point_on_ortho_segment(j.x, j.y, x1, y1, x2, y2):
                    continue
                # Prefer a POE on a segment matching our orientation
                seg_horiz = (y1 == y2)
                if seg_horiz == is_horizontal:
                    return (j.x, j.y)

        # POE on any segment orientation (secondary preference)
        for j in sheet.junctions:
            if not j.is_bus_entry:
                continue
            if any(_point_on_ortho_segment(j.x, j.y, *s) for s in net_segs):
                return (j.x, j.y)

        # 2. Corners: endpoints shared by 2+ segments
        ep_count: Counter = Counter()
        for x1,y1,x2,y2 in net_segs:
            ep_count[(x1,y1)] += 1
            ep_count[(x2,y2)] += 1
        corners = [pt for pt, cnt in ep_count.items() if cnt >= 2]
        if corners:
            return corners[0]

        # 3. Any endpoint
        x1,y1,x2,y2 = net_segs[0]
        return (x1, y1)

    # Find horizontal bus segments (y1 == y2)
    h_segs = [(x1, y1, x2, y2) for x1, y1, x2, y2 in net_segs if y1 == y2]
    if h_segs:
        # Topmost horizontal bus in KiCad coords = highest y in Ulticap coords
        seg = max(h_segs, key=lambda s: s[1])
        x1, y1, x2, y2 = seg
        bus_x_min = min(x1, x2)
        bus_x_max = max(x1, x2)
        bus_y = y1
        grid = sheet.grid or 50

        # Find bus-to-bus junctions on this segment
        bbjunctions = [j for j in sheet.junctions
                       if not j.is_bus_entry
                       and _point_on_ortho_segment(j.x, j.y, x1, y1, x2, y2)]
        if bbjunctions:
            leftmost = min(bbjunctions, key=lambda j: j.x)
            initial_lx = leftmost.x + grid
        else:
            initial_lx = bus_x_min + grid

        # Clamp to bus extent
        initial_lx = max(bus_x_min, min(initial_lx, bus_x_max))

        # Build candidate list: start from initial_lx stepping right,
        # wrapping to positions left of initial_lx if needed.
        candidates_right = list(range(initial_lx, bus_x_max + 1, grid))
        candidates_left  = list(range(initial_lx - grid, bus_x_min - 1, -grid))
        candidates = candidates_right + candidates_left
        if not candidates:
            candidates = [initial_lx]

        lx = _first_free(candidates, bus_y, True, net_seg_set)
        if lx is None:
            return _safe_fallback(net_segs, is_horizontal=True)
        return lx, bus_y
    else:
        # No horizontal bus segment — use leftmost/topmost point of first seg
        x1, y1, x2, y2 = net_segs[0]
        is_horizontal = (y1 == y2)
        grid = sheet.grid or 50
        if is_horizontal:
            bus_min, bus_max, fixed = min(x1,x2), max(x1,x2), y1
            initial = bus_min + grid
            candidates = list(range(initial, bus_max+1, grid)) + \
                         list(range(initial-grid, bus_min-1, -grid))
            if not candidates: candidates = [initial]
            lv = _first_free(candidates, fixed, True, net_seg_set)
            if lv is None:
                return _safe_fallback(net_segs, is_horizontal=True)
            return lv, fixed
        else:
            bus_min, bus_max, fixed = min(y1,y2), max(y1,y2), x1
            initial = bus_min + grid
            candidates = list(range(initial, bus_max+1, grid)) + \
                         list(range(initial-grid, bus_min-1, -grid))
            if not candidates: candidates = [initial]
            lv = _first_free(candidates, fixed, False, net_seg_set)
            if lv is None:
                return _safe_fallback(net_segs, is_horizontal=False)
            return fixed, lv


def _bus_alias_sexp(alias_name: str, members: List[str]) -> List[str]:
    """Emit a KiCad (bus_alias ...) block."""
    quoted = ' '.join(f'"{m}"' for m in members)
    return [
        f'  (bus_alias "{alias_name}"',
        f'    (members {quoted})',
        '  )',
    ]


def _bus_label_sexp(label_text: str, x_mm: float, y_mm: float,
                    size_mm: float = 0.889) -> List[str]:
    """Emit a KiCad (label ...) for a bus name, placed on the bus."""
    return [
        f'  (label "{label_text}"',
        f'    (at {_f(x_mm)} {_f(y_mm)} 0)',
        '    (effects',
        '      (font',
        f'        (size {_f(size_mm)} {_f(size_mm)})',
        '      )',
        '      (justify left bottom)',
        '    )',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


def _userline_sexp(ul: UserLine, tr: _Transform) -> List[str]:
    """Emit one (polyline ...) for a graphic-layer (*LV level=2) line.

    Always uses a normal stroked line (rounded caps/joins), even when
    thick and solid. The filled-rectangle flat-cap substitution is
    reserved for symbol-body lines (_sym_polyline) only: UserLine
    records are frequently joined end-to-end into multi-segment shapes
    (title-block-style borders, revision boxes, etc.), and applying the
    flat-cap trick per-segment there reintroduces gaps at outer corners.
    Rounded corners/caps on the graphic layer are visually acceptable.
    """
    _lt = {0: 'solid', 256: 'dash', 512: 'dash_dot', 768: 'dot'}
    lt = _lt.get(ul.linetype, 'solid')

    # thickness is stored in mils; convert to mm (1 mil = 0.0254 mm)
    thick = _f(ul.thickness * MM_PER_MIL) if ul.thickness else '0'
    stroke = _colour_stroke(ul.colour, thick, lt)
    return [
        '  (polyline',
        f'    (pts {tr.xy(ul.x1, ul.y1)} {tr.xy(ul.x2, ul.y2)})',
        f'    {stroke}',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


HEIGHT_TUNE = 0.5     # Fine-tuning multiplier for rendered text height
                      # (reference, value, labels, plain text outside symbols,
                      # and all other text except pin numbers/pin names and
                      # plain text embedded in a symbol, which use their own
                      # multipliers below).
PIN_NUM_HEIGHT_TUNE  = 0.5  # Fine-tuning multiplier for pin number text height,
                            # independent of HEIGHT_TUNE.
PIN_NAME_HEIGHT_TUNE = 0.5  # Fine-tuning multiplier for pin name text height and
                            # for plain text embedded in the symbol itself (its
                            # TXT graphics), independent of HEIGHT_TUNE.
OFFSET_PNAME = 0.005  # Pin name offset from pin endpoint (mm). KiCad default is 1.016.
                      # Small value minimises pin name overlap on dense ICs.

# Ulticap's own ULTIC.SET "thin"/"thick" line-width settings, in Ulticap's own
# raw units (as displayed in Ulticap — not real mils). ULTIC.SET isn't always
# available to KIUC and can be changed by the user at any time, so these are
# exposed as editable defaults rather than assumed fixed. Empirically measured:
# a "thick" (raw 30) line is actually 15 mil wide in Ulticap, i.e. Ulticap's
# raw setting is always double the real mil value — see _sym_line_width_mm.
# Used only for symbol body graphics (polylines/circles); KiCad wires/buses
# are left to KiCad's own default line rendering rules.
THIN_LINE_WIDTH  = 6.0
THICK_LINE_WIDTH = 30.0

# Label alignment-adjustment constant.
# When a *A LABEL= annotation's POE is on a plain wire and its anchor is
# offset from that wire, the anchor is projected onto the nearest point of
# POE's own wire network (see _wire_networks / _nearest_point_on_ortho_segment)
# and used as the label's position, keeping its originally-authored along-wire
# justification untouched and flipping only the perpendicular-axis justify
# when needed (see _annot_label_sexp).
# ANCHOR_WIRE_MAX_DIST: how far (Ulticap units) a label's anchor may sit from
# its own POE's wire network before we give up trying to place the label at
# a projected on-wire point and fall back to the POE itself. Ulticap's DOS
# editor lets a label's text be dragged anywhere while its POE stays fixed to
# the wire it names, so a real (sometimes large, entirely legitimate) gap
# between anchor and POE is normal; this limit exists only to catch the case
# where the anchor has been moved somewhere with no nearby wire in its own
# net at all (a different part of the schematic), not to second-guess a
# label that is genuinely, if distantly, positioned along its own wire.
ANCHOR_WIRE_MAX_DIST = 100.0   # units; distance to nearest point on POE's own
                               # wire network above which we fall back to POE

# ALONG_WIRE_CENTRE_LO / ALONG_WIRE_CENTRE_HI: when a label falls back to
# POE specifically because the nearest safe point would coincide with an
# unrelated net at an unmarked crossing (see _project_label_onto_network),
# the along-wire justify may need to flip so the text doesn't keep extending
# past POE into whatever lies beyond it -- but only if the along-wire offset
# is large enough, relative to the label's own real text width (from
# _uc_box_metrics), to actually risk that overlap. This mirrors the ratio
# rule the old code used to apply everywhere (below LO: keep, LO..HI:
# centre only if already centre, above HI: swap outright) -- narrowed here
# to only the one case that still needs it, and now measured against the
# label's real per-character width instead of a flat per-character guess.
ALONG_WIRE_CENTRE_LO = 0.25   # |offset|/text_width below this  -> keep as-is
ALONG_WIRE_CENTRE_HI = 0.75   # |offset|/text_width above this -> flip outright



def _sym_line_width_mm(is_thick: bool) -> float:
    """Real stroke width (mm) for symbol-body graphics (polylines/circles).

    THIN_LINE_WIDTH / THICK_LINE_WIDTH are Ulticap's own ULTIC.SET numbers.
    Ulticap's raw setting is always double the real mil width (empirically
    measured — see the comment above THIN_LINE_WIDTH), so it's halved here
    before the mil→mm conversion.
    """
    raw_mil = THICK_LINE_WIDTH if is_thick else THIN_LINE_WIDTH
    return (raw_mil / 2) * MM_PER_MIL

# ── Fine-tuning parameter registry ──────────────────────────────────────────
# Single source of truth for every user-adjustable constant that affects
# KiCad output geometry/rendering. Each entry is
#   (name, default, min, max, description, category)
# category is 'empirical' for constants tuned by eye against KiCad's visual
# output, or 'ulticap' for constants that mirror an actual Ulticap/ULTIC.SET
# value (editable in case the user's own Ulticap install was customized).
# The GUI's tuning pop-up and the CLI's ini loader both walk this table, so
# adding a new tunable later only requires adding one entry here -- no new
# dialog layout code, no new CLI plumbing.
TUNING_SPEC = [
    ('HEIGHT_TUNE', 0.5, 0.1, 2.0,
     "Multiplier applied to rendered text height (vs. the raw Ulticap size) "
     "for reference, value, labels, plain text, and all other text except "
     "pin numbers and pin names, which use their own multipliers below.",
     'empirical'),
    ('PIN_NUM_HEIGHT_TUNE', 0.5, 0.1, 2.0,
     "Multiplier applied to pin number text height, independent of "
     "HEIGHT_TUNE (which covers all other text).",
     'empirical'),
    ('PIN_NAME_HEIGHT_TUNE', 0.5, 0.1, 2.0,
     "Multiplier applied to pin name text height and to plain text embedded "
     "in the symbol itself (its TXT graphics), independent of HEIGHT_TUNE.",
     'empirical'),
    ('OFFSET_PNAME', 0.005, 0.0, 2.0,
     "Pin name offset from pin endpoint, mm. KiCad default is 1.016; a "
     "small value minimises pin name overlap on dense ICs.",
     'empirical'),
    ('ANCHOR_WIRE_MAX_DIST', 100.0, 0.0, 2000.0,
     "Max distance (Ulticap units) a label's anchor may sit from its own "
     "POE's wire network before giving up on projecting it onto the wire "
     "and falling back to the POE's own position instead.",
     'empirical'),
    ('ALONG_WIRE_CENTRE_LO', 0.25, 0.0, 1.0,
     "When a label falls back to POE due to an unsafe net-crossing, "
     "|along-wire offset|/text-width ratio below which the along-wire "
     "justify is kept as-is (no overlap risk worth flipping for).",
     'empirical'),
    ('ALONG_WIRE_CENTRE_HI', 0.75, 0.0, 1.0,
     "Same ratio, above which the along-wire justify is flipped outright "
     "rather than centred.",
     'empirical'),

    ('THIN_LINE_WIDTH', 6.0, 1.0, 50.0,
     "Ulticap's ULTIC.SET \"thin\" line-width setting, in Ulticap's own "
     "raw units (as shown in Ulticap, not real mils — real mil = this ÷ 2). "
     "Affects symbol body graphics only, not wires/buses.",
     'ulticap'),
    ('THICK_LINE_WIDTH', 30.0, 1.0, 100.0,
     "Ulticap's ULTIC.SET \"thick\" line-width setting, in Ulticap's own "
     "raw units (as shown in Ulticap, not real mils — real mil = this ÷ 2). "
     "Affects symbol body graphics only, not wires/buses.",
     'ulticap'),
]


def get_tuning() -> dict:
    """Return the current value of every fine-tuning constant, by name."""
    return {name: globals()[name] for name, *_rest in TUNING_SPEC}


def set_tuning(values: dict) -> None:
    """Update fine-tuning constants from a {name: value} dict. Unknown
    keys are ignored; keys absent from `values` are left unchanged."""
    valid = {name for name, *_rest in TUNING_SPEC}
    for name, value in values.items():
        if name in valid:
            globals()[name] = float(value)


def _text_size_mm(size_units: int, tune: Optional[float] = None) -> float:
    """Convert Ulticap text size to KiCad font height in mm.

    Ulticap size unit = 2 mils.  Real text height = size * 2 * 0.0254 mm.
    HEIGHT_TUNE allows fine-tuning without changing the formula; pass an
    explicit `tune` (e.g. PIN_NUM_HEIGHT_TUNE / PIN_NAME_HEIGHT_TUNE) for
    callers that need a category-specific multiplier instead of the shared
    HEIGHT_TUNE used by every other text category.
    Note: HEIGHT_FACTOR (0.0754) is used only for bounding-box positioning,
    not for the actual rendered font size.
    """
    return round(size_units * 0.0508 * (HEIGHT_TUNE if tune is None else tune), 4)


def _vis_justify(r_code: int, visibility: int):
    """Map (R, V) to (hjust, vjust, is_vertical, angle) for writing to .kicad_sch.

    KiCad applies an automatic flip transform when rendering component text.
    The value written to the file must be the inverse of that transform applied
    to the desired display value (Col2 = ulticap_translate result).
    Since all transforms are self-inverse, Col3 = kicad_flip(Col2).

    KiCad flip table (empirically verified, 144 ground-truth combinations):
      R0 H+V : none
      R1 H   : none          R1 V : 180° (both B↔T and L↔R)
      R2 H+V : 180°
      R3 H   : 180°          R3 V : none
      R4 H   : L↔R           R4 V : T↔B
      R5 H+V : T↔B
      R6 H   : T↔B           R6 V : L↔R
      R7 H+V : L↔R
    """
    u     = ulticap_translate(r_code, visibility)
    hjust, vjust, is_v, angle = u_to_justify(u)

    # Apply KiCad's flip (self-inverse pre-compensation)
    _FLIP_H = {'left': 'right', 'right': 'left', 'center': 'center'}
    _FLIP_V = {'bottom': 'top',  'top': 'bottom', 'center': 'center'}

    # flip_row (T↔B), flip_col (L↔R) per (r_code, is_v)
    _FLIPS = {
        #        (flip_row, flip_col)
        (0,'H'): (False, False),  (0,'V'): (False, False),
        (1,'H'): (False, False),  (1,'V'): (True,  True),
        (2,'H'): (True,  True),   (2,'V'): (True,  True),
        (3,'H'): (True,  True),   (3,'V'): (False, False),
        (4,'H'): (False, True),   (4,'V'): (True,  False),
        (5,'H'): (True,  False),  (5,'V'): (True,  False),
        (6,'H'): (True,  False),  (6,'V'): (False, True),
        (7,'H'): (False, True),   (7,'V'): (False, True),
    }
    key = (r_code & 7, 'V' if is_v else 'H')
    fr, fc = _FLIPS[key]
    if fr: vjust = _FLIP_V[vjust]
    if fc: hjust = _FLIP_H[hjust]

    return hjust, vjust, is_v, angle


def _justify_str(hjust: str, vjust: str) -> str:
    """Build KiCad (justify ...) string, omitting defaults (center/center).

    KiCad supports (justify bottom/top) without an explicit hjust token,
    so we can express all non-center vjust values even when hjust is center.
    """
    _hj = '' if hjust == 'center' else hjust
    _vj = '' if vjust == 'center' else vjust
    jt = ' '.join(x for x in [_hj, _vj] if x)
    return f' (justify {jt})' if jt else ''


def _project_label_onto_network(ann: Annotation, is_v: bool,
                                 hjust: str, vjust: str, orig_angle: int,
                                 networks: list, all_networks: list = None):
    """Project ann's anchor onto the nearest point of whichever network in
    `networks` contains ann's POE, applying the justify-flip rules below.
    Shared by both the wire-POE and bus-POE cases in _annot_label_sexp -- a
    label's anchor-vs-POE authoring convention (and the same 'pull towards
    the wire, never away' flip rule) is exactly the same whether what it's
    naming is a wire or a bus; only the candidate pool differs (wire
    networks vs bus networks), and the two pools are never mixed when
    searching for a projection candidate.

    `all_networks` should be every wire and bus network on the sheet (the
    combined pool); it is used only for the crossing-safety check below,
    against every network except whichever one turns out to be this label's
    own (never as a source of candidate points itself).

    Three outcomes, each with its own justify treatment:

    1. SAFE PROJECTION -- the nearest point (preferring a segment whose
       orientation matches the label's own text orientation, to avoid
       snapping onto the wrong wire/bus at an inner corner) is within
       ANCHOR_WIRE_MAX_DIST and does not coincide with any other network.
       Position = the anchor's own along-wire coordinate at the network's
       perpendicular coordinate. The along-wire justify is untouched (the
       anchor's own position there is exactly where the user placed the
       text, so its original alignment already reads correctly). Only the
       PERPENDICULAR-axis justify may flip, and only when it was originally
       'center' and the offset clears the scaled top margin from
       _uc_vertical_metrics -- a smaller offset barely straddles the
       wire/bus even before flipping, not worth disturbing.

    2. TOO FAR -- the nearest point exceeds ANCHOR_WIRE_MAX_DIST (anchor
       moved somewhere with no nearby wire/bus in its own net at all).
       Position = POE directly, alignment fully unchanged on both axes --
       there is no reliable geometric signal left to adjust anything from.

    3. CROSSING-UNSAFE -- the nearest point would coincide with an unrelated
       network (Ulticap wires, or a wire and a bus, may cross at a point
       with no junction dot, meaning they are NOT electrically connected
       there even though the crossing point satisfies the on-segment test
       for both; placing a KiCad label exactly there would make KiCad treat
       it as touching both, bridging two nets that were never meant to
       connect). Position = POE directly. Here the text is no longer
       sitting at the anchor's own along-wire coordinate, so unlike case 1
       the ALONG-WIRE justify may also need to flip so the text doesn't
       keep extending past POE into whatever lies beyond it -- but, mirroring
       the ratio rule the old code applied everywhere (just narrowed here to
       only this one case, and measured against the label's real width from
       _uc_box_metrics instead of a flat per-character guess):
         |along-wire offset| / text_width < ALONG_WIRE_CENTRE_LO
             -> keep as-is (offset too small to risk any overlap)
         ALONG_WIRE_CENTRE_LO..HI -> centre, but only if the original
             along-wire justify was already 'center' (a label already
             justified to one side already reads correctly at that size of
             offset without centring)
         > ALONG_WIRE_CENTRE_HI -> flip outright, regardless of the
             original justify, pulling the text back towards where the
             anchor was.
       The perpendicular-axis justify still only flips under the same
       center-and-clears-the-margin condition as case 1, using the real
       anchor-to-POE offset and POE's own segment orientation.

    In all cases, the KiCad angle only needs recomputing when the flipped
    axis is also this label's own text-reading axis (hjust for horizontal
    text, vjust for vertical text).

    Returns (lx, ly, hjust_adj, vjust_adj, angle), or None only in the
    defensive, shouldn't-happen case where POE isn't found in any network at
    all -- the caller falls back to the anchor, alignment fully unchanged.
    """
    _net = None
    if networks:
        for _segs in networks:
            if any(_point_on_ortho_segment(ann.x_poe, ann.y_poe, *s) for s in _segs):
                _net = _segs
                break
    if not _net:
        return None

    def _seg_is_horizontal(s):
        return s[1] == s[3]
    _preferred = [s for s in _net if _seg_is_horizontal(s) == (not is_v)]
    _candidates = _preferred if _preferred else _net

    _best = None
    for s in _candidates:
        nx, ny, d = _nearest_point_on_ortho_segment(ann.x, ann.y, *s)
        if _best is None or d < _best[2]:
            _best = (nx, ny, d, s)

    _too_far = _best is None or _best[2] > ANCHOR_WIRE_MAX_DIST
    _crossing_unsafe = False
    if not _too_far:
        nx, ny, _d, s = _best
        _seg_horizontal = (s[1] == s[3])
        if all_networks and any(_point_on_ortho_segment(nx, ny, *os)
                                 for _on in all_networks if _on is not _net
                                 for os in _on):
            _crossing_unsafe = True

    if _too_far or _crossing_unsafe:
        # POE fallback (cases 2/3): find POE's own segment for orientation,
        # preferring one matching the label's text orientation in case POE
        # sits at a genuine same-network T-junction with more than one.
        nx, ny = ann.x_poe, ann.y_poe
        _poe_segs = [seg for seg in _net
                     if _point_on_ortho_segment(ann.x_poe, ann.y_poe, *seg)]
        _poe_preferred = [seg for seg in _poe_segs if _seg_is_horizontal(seg) == (not is_v)]
        _fallback_seg = (_poe_preferred or _poe_segs or ([_best[3]] if _best else []))
        if not _fallback_seg:
            return None   # defensive: POE not actually on any segment of its own net
        _seg_horizontal = _seg_is_horizontal(_fallback_seg[0])

    hjust_adj, vjust_adj, angle = hjust, vjust, orig_angle

    def _along_wire_zone(_offset: float) -> str:
        """Zone-based along-wire decision (case 3 only): 'keep', 'center',
        or 'flip', based on |offset| vs the label's real text width."""
        _rendered = _re.sub(r'~\{([^}]*)\}', r'\1', ann.text)
        _, _content_w, _, _ = _uc_box_metrics(_rendered, ann.size)
        if _content_w <= 0 or _offset == 0:
            return 'keep'
        _ratio = abs(_offset) / _content_w
        if _ratio < ALONG_WIRE_CENTRE_LO:
            return 'keep'
        elif _ratio <= ALONG_WIRE_CENTRE_HI:
            return 'center'
        else:
            return 'flip'

    if _seg_horizontal:
        # Perpendicular axis = Y = vjust (all cases)
        _perp_offset = ann.y - ny
        if vjust == 'center':
            top_margin, _bottom_margin = _uc_vertical_metrics(ann.size)
            if abs(_perp_offset) >= top_margin:
                vjust_adj = 'bottom' if _perp_offset > 0 else 'top'
        if is_v and vjust_adj != vjust:
            angle = 270 if vjust_adj == 'bottom' else 90
        if _crossing_unsafe:
            # Along-wire axis = X = hjust
            _along_offset = ann.x - nx
            _zone = _along_wire_zone(_along_offset)
            if _zone == 'center':
                hjust_adj = 'center' if hjust == 'center' else hjust
            elif _zone == 'flip':
                hjust_adj = 'right' if _along_offset < 0 else 'left'
            if not is_v and hjust_adj != hjust:
                angle = 180 if hjust_adj == 'right' else 0
    else:
        # Perpendicular axis = X = hjust (all cases)
        _perp_offset = ann.x - nx
        if hjust == 'center':
            top_margin, _bottom_margin = _uc_vertical_metrics(ann.size)
            if abs(_perp_offset) >= top_margin:
                hjust_adj = 'right' if _perp_offset < 0 else 'left'
        if not is_v and hjust_adj != hjust:
            angle = 180 if hjust_adj == 'right' else 0
        if _crossing_unsafe:
            # Along-wire axis = Y = vjust
            _along_offset = ann.y - ny
            _zone = _along_wire_zone(_along_offset)
            if _zone == 'center':
                vjust_adj = 'center' if vjust == 'center' else vjust
            elif _zone == 'flip':
                vjust_adj = 'bottom' if _along_offset > 0 else 'top'
            if is_v and vjust_adj != vjust:
                angle = 270 if vjust_adj == 'bottom' else 90

    return nx, ny, hjust_adj, vjust_adj, angle


def _annot_label_sexp(ann: Annotation, tr: _Transform,
                      wire_segments: list = None,
                      bus_segments: list = None,
                      bus_entry_poes: set = None,
                      wire_networks: list = None,
                      bus_networks: list = None) -> List[str]:
    """Emit KiCad (label ...) for a *A LABEL= annotation.

    Position:
      If POE is a bus-entry junction → use anchor, keep original alignment.
                                        The junction point coincides with the bus;
                                        placing a KiCad net label there would name
                                        the entire bus rather than just the wire.
      If POE is free-floating         → use anchor, keep original alignment.
                                        Ulticap's DOS editor allows a label whose
                                        POE was never actually attached to any
                                        wire; there is nothing to project onto,
                                        and any position the writer might pick
                                        (e.g. the POE itself, often near a sheet
                                        edge) risks overlapping other symbols or
                                        landing outside the sheet -- the anchor,
                                        wherever the user put it, is the only
                                        reasonable choice.
      If POE lies on a plain wire, or on a bus (and is not a bus-entry
      junction) → find POE's own network (see _wire_networks / _bus_networks
                                        and _project_label_onto_network) and
                                        project the anchor onto the nearest
                                        point of that network, preferring
                                        segments whose orientation matches the
                                        label's own text orientation (a
                                        horizontal label should snap onto a
                                        horizontal wire/bus even if a nearer
                                        vertical segment happens to sit at the
                                        same inner corner); falls back to
                                        any-orientation nearest only if the
                                        network has none matching. If that
                                        nearest distance exceeds
                                        ANCHOR_WIRE_MAX_DIST, the anchor has
                                        been moved somewhere with no nearby
                                        wire/bus in its own net at all: use
                                        POE directly instead, original
                                        alignment unchanged. Wire and bus
                                        candidates are never mixed within a
                                        single search -- a label whose POE is
                                        on a bus only ever considers that
                                        bus's own network, never a nearby
                                        unrelated wire, and vice versa.

    Alignment adjustment (only when projecting onto a wire or bus, above):
      See _project_label_onto_network's own docstring for the full detail.
      In short: when the label's own along-wire anchor position is used
      (the safe, common case), sliding along the wire/bus never needs any
      justify change, and only the axis PERPENDICULAR to it may flip, and
      only when the original justify there was already 'center' and the
      offset clears the scaled top margin from _uc_vertical_metrics. When
      the label instead falls back to sitting at POE itself (anchor too far
      from its own net, or the nearest point would coincide with an
      unrelated net at an unmarked crossing), the ALONG-WIRE justify also
      flips -- unconditionally -- so the text is pulled back towards where
      the anchor was rather than left extending further away from POE into
      whatever lies beyond it.
      hjust_adj/vjust_adj are carried as separate variables; the model is
      never mutated. The KiCad angle only needs recomputing when the
      flipped axis is also this label's own text-reading axis (hjust for
      horizontal text, vjust for vertical text).
    """
    sz  = _f(_text_size_mm(ann.size))
    hjust, vjust, is_v, txt_angle = _vis_justify(0, ann.visibility)

    # Original angle from vis byte (kept unless the rare mismatched-
    # orientation fallback below needs to recompute it)
    _a  = ann.visibility & 31
    _n  = (_a - 16) if _a >= 16 else _a
    _col = _n % 3
    if _a < 16:
        orig_angle = 180 if _col == 2 else 0
    else:
        orig_angle = 270 if _col == 2 else 90

    hjust_adj = hjust
    vjust_adj = vjust
    angle     = orig_angle

    # ── Position decision ─────────────────────────────────────────────────────
    _at_bus_entry = bool(bus_entry_poes and (ann.x_poe, ann.y_poe) in bus_entry_poes)
    _on_bus = bool(bus_segments and
                   any(_point_on_ortho_segment(ann.x_poe, ann.y_poe, *s) for s in bus_segments))
    _on_wire = bool(wire_segments and
                     any(_point_on_ortho_segment(ann.x_poe, ann.y_poe, *s) for s in wire_segments))

    _lx, _ly = ann.x, ann.y   # default: anchor, original alignment (bus-entry / free-floating)

    _result = None
    _all_networks = (wire_networks or []) + (bus_networks or [])
    if _at_bus_entry:
        pass   # anchor, unchanged -- see docstring
    elif _on_wire:
        _result = _project_label_onto_network(ann, is_v, hjust, vjust, orig_angle,
                                               wire_networks, _all_networks)
    elif _on_bus:
        _result = _project_label_onto_network(ann, is_v, hjust, vjust, orig_angle,
                                               bus_networks, _all_networks)

    if _result is not None:
        _lx, _ly, hjust_adj, vjust_adj, angle = _result
    # else: defensive/shouldn't-happen case (POE confirmed on wire/bus but no
    # network actually contains it) -- keep the anchor, original alignment,
    # same as the bus-entry/free-floating branches above.

    # NOTE: no box-model anchor-shift correction is applied here, unlike
    # every other text-anchor call site in this writer. That shift is only
    # safe for PROPERTY text (REFDES/VALUE/DEVICE/etc.), where position is
    # purely cosmetic in KiCad -- it has no electrical meaning. This
    # function emits actual (label ...) elements, where position IS the
    # electrical connection: a label only attaches to a net if its
    # coordinate sits exactly on a wire/bus. Applying the shift here would
    # move the label's carefully-projected, on-wire coordinate (see
    # _project_label_onto_network above) back OFF that wire, breaking
    # connectivity entirely -- confirmed empirically (every label in a real
    # file lost its wire connection once this was tried). Left un-applied
    # deliberately; see ULTICAP_TEXT_MODEL.md §7 and NAMED_STUB.md.

    just_str = _justify_str(hjust_adj, vjust_adj)
    effects  = _colour_effects(ann.colour, sz, just_str)
    return [
        f'  (label "{_esc(ann.text)}"',
        f'    (at {_f(tr.x(_lx))} {_f(tr.y(_ly))} {angle})',
        f'    {effects}',
        f'    (uuid "{_uid()}")',
        '  )',
    ]



def _text_sexp(ann: Annotation, tr: _Transform) -> List[str]:
    """Emit KiCad (text ...) for a free-text annotation (*A TEXT=/UT_NETNAME or *X record).

    Annotations have no R code — placed as-is (R=0, identity).
    Schematic (text ...) angles use normal degrees (not tenths).

    Visibility: bit 7 = value shown, bit 6 = tag shown.
    When both are set (e.g. vis=196), display text is 'TAG=value'.
    When only bit 7 is set, display just the value.
    """
    sz = _f(_text_size_mm(ann.size))
    hjust, vjust, is_v, angle = _vis_justify(0, ann.visibility)
    just_str = _justify_str(hjust, vjust)
    effects = _colour_effects(ann.colour, sz, just_str)
    # Compose display text from vis bits
    show_val = bool(ann.visibility & 128)
    show_tag = bool(ann.visibility & 64)
    if show_tag and ann.tag and ann.tag not in ('LABEL', 'TEXT', 'TXT'):
        display_text = f'{ann.tag}={ann.text}' if show_val else ann.tag
    else:
        display_text = ann.text if show_val else ''
    ann = type(ann)(
        x=ann.x, y=ann.y, x_poe=ann.x_poe, y_poe=ann.y_poe,
        text=display_text, size=ann.size, colour=ann.colour,
        visibility=ann.visibility, tag=ann.tag,
    )
    return [
        f'  (text "{_esc(ann.text)}"',
        f'    (at {_f(tr.x(ann.x))} {_f(tr.y(ann.y))} {angle})',
        f'    {effects}',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


def _x_text_sexp(lbl: Label, tr: _Transform) -> List[str]:
    """Emit KiCad (text ...) for a free-text *X record.

    *X is plain freely-placed text — the older, more limited form of *A TEXT=.
    rotation: 0=horizontal, 5760=vertical (90°); all other values clamped to 0.
    align:    0-8 {0:BL,1:BC,2:BR,3:CL,4:CC,5:CR,6:TL,7:TC,8:TR}; always visible.
    """
    sz  = _f(_text_size_mm(lbl.size))
    ang = 90 if lbl.rotation == 5760 else 0
    hjust, vjust, is_v, _ang = _vis_justify(0, lbl.align)
    just_str = _justify_str(hjust, vjust)
    effects = _colour_effects(lbl.colour, sz, just_str)
    return [
        f'  (text "{_esc(lbl.text)}"',
        f'    (at {_f(tr.x(lbl.x))} {_f(tr.y(lbl.y))} {ang})',
        f'    {effects}',
        f'    (uuid "{_uid()}")',
        '  )',
    ]


# ── dump / inspect ─────────────────────────────────────────────────────────────

def dump_schematic(schematic: 'Schematic', write) -> None:
    """Write a human-readable summary of a schematic to *write*.

    Parameters
    ----------
    schematic : Schematic
        Parsed schematic (one or more sheets).
    write : callable(text, tag='')
        Output sink.  *text* is the line to emit.  *tag* is an optional
        colour hint for GUI callers ('info', 'warn', 'err'); plain-text
        callers (CLI) ignore it.  The callable must NOT add a trailing
        newline — the function passes one line per call.
    """
    for sh in schematic.sheets:
        vx, vy = vrp(sh)
        write(f'\n=== Sheet: {sh.name} ===', 'info')
        write(f'  Bounds  : ({sh.xmin},{sh.ymin}) → ({sh.xmax},{sh.ymax})'
              f'  ({sh.width_mm:.1f} × {sh.height_mm:.1f} mm)')
        write(f'  Grid    : {sh.grid or 25} mils')
        write(f'  VRP     : ({vx},{vy}) units = ({vx*MM_PER_UNIT:.3f},{vy*MM_PER_UNIT:.3f}) mm')
        write(f'  Root    : {sh.root_sheet or "(none)"}')
        write(f'  V5.x arc fix: '
              f'{"no (DOS version)" if sh.is_less_than_v500 else "yes (Windows version)"}')
        write(f'  Symbols   : {len(sh.symbols)}')
        write(f'  Components: {len(sh.components)}')
        write(f'  Wires     : {len(sh.wires)}'
              f'  (buses: {sum(1 for w in sh.wires if w.is_bus)})')
        write(f'  User lines: {len(sh.user_lines)}')
        write(f'  Junctions : {len(sh.junctions)}')
        write(f'  Annotations: {len(sh.annotations)}')
        write(f'  Net labels: {len(sh.labels)}')
        write('')
        write(f'  {"Reference":<12}{"Symbol name":<22}Value', 'info')
        write(f'  {"----------":<12}{"--------------------":<22}--------------------', 'info')
        for comp in sh.components:
            rd  = comp.refdes     or '?'
            dev = comp.device     or comp.symbol_name
            val = comp.value      or '-'
            write(f'  {rd:10s}  {dev:20s}  {val}')


def dump_symbols(schematic: 'Schematic', write) -> None:
    """Write a symbol-level summary of a schematic to *write*.

    Parameters
    ----------
    schematic : Schematic
        Parsed schematic (one or more sheets).
    write : callable(text, tag='')
        Same contract as for dump_schematic.
    """
    for sh in schematic.sheets:
        write(f'\n=== Symbols in {sh.name} ===', 'info')
        for name, sym in sorted(sh.symbols.items()):
            write(f'\n  *S{name}  ({sym.width}×{sym.height})', 'info')
            write(f'    Pins: {len(sym.pins)}  '
                  f'Circles: {len(sym.circles)}  '
                  f'Polylines: {len(sym.polylines)}')
            for sa in sym.sym_attrs:
                write(f'    {sa.tag:15s} = {sa.default_value!r}')

        write('\n=== TITLE component ===', 'info')
        found = False
        for comp in sh.components:
            if comp.symbol_name == 'TITLE':
                found = True
                for tag, val in comp.attributes.items():
                    write(f'  {tag:15s} = {val!r}')
        if not found:
            write('  (no TITLE component found)', 'warn')


# ── main builder ───────────────────────────────────────────────────────────────

def _build_sch(sheet: Sheet,
               subsheet_map: dict = None,
               sheet_path: str = '/',
               page_num: int = 1,
               sch_uuid: str = None,
               proj_name: str = None,
               suppress_title: bool = False) -> List[str]:
    # Paper size: fit tight content bbox (true coordinate scan) + margin.
    # _TB_GAP adds clearance on all four sides so content is never flush against
    # the paper edge; 50 units (~2.54 mm) is intentionally conservative.
    # The Ulticap title block is a placed symbol included in tight_bounds, so
    # no separate height reservation is needed.
    _TB_GAP = 50
    _tb = _tight_bounds_with_gap(sheet, _TB_GAP)
    if _tb:
        tb_xmin, tb_ymin, tb_xmax, tb_ymax = _tb
    else:
        tb_xmin, tb_ymin, tb_xmax, tb_ymax = (
            sheet.xmin, sheet.ymin, sheet.xmax, sheet.ymax)
    grid = sheet.grid or 25
    content_w_snap = math.ceil((tb_xmax - tb_xmin) / grid) * grid
    content_h_snap = math.ceil((tb_ymax - tb_ymin) / grid) * grid
    content_w_mm = content_w_snap * MM_PER_UNIT
    content_h_mm = content_h_snap * MM_PER_UNIT
    paper_name, paper_w, paper_h = _nearest_paper(content_w_mm, content_h_mm)
    # Standard sizes use (paper "NAME"); a None name signals the custom-size
    # fallback from _nearest_paper, written as (paper "User" W H).
    if paper_name is None:
        _paper_token = f'"User" {paper_w:g} {paper_h:g}'
    else:
        _paper_token = f'"{paper_name}"'
    tr = _Transform(sheet, paper_w, paper_h,
                    xmin=tb_xmin, ymin=tb_ymin, xmax=tb_xmax, ymax=tb_ymax)

    # Title block
    tb = _extract_title_block(sheet)

    # Schematic UUID: use provided value (from write_schematic) or generate one
    if sch_uuid is None:
        sch_uuid = _uid()
    sheet._sch_uuid  = sch_uuid
    sheet._proj_name = (proj_name if proj_name is not None
                        else (Path(sheet.name).stem
                              if getattr(sheet, 'name', None) else 'schematic'))
    sheet._page_num  = page_num

    # Component instance path for (instances (path "...")) entries.
    # For root sheet:  sheet_path="/"  → inst_path = "/{root_sch_uuid}"
    # For subsheet:    sheet_path="/{root_uuid}/{sheet_elem_uuid}"
    #                           → inst_path = "/{root_uuid}/{sheet_elem_uuid}"
    # i.e. for subsheets sheet_path IS the inst_path; for root we build it from sch_uuid.
    if sheet_path == '/':
        sheet._inst_path = f'/{sch_uuid}'
    else:
        sheet._inst_path = sheet_path

    # Store subsheet_map on sheet so _comp_sexp → _sheet_sexp can access it
    sheet._subsheet_map = subsheet_map or {}

    # Pre-assign (sheet ...) element uuids for all hier-sheet placements so that
    # write_schematic can build subsheet paths using the same uuids.
    _sheet_elem_uuids: dict = {}   # id(comp) → uuid_str
    for _hc in sheet.components:
        if _is_hier_sheet(_hc):
            _sheet_elem_uuids[id(_hc)] = _uid()
    sheet._sheet_elem_uuids = _sheet_elem_uuids

    lines: List[str] = [
        '(kicad_sch',
        '  (version 20250114)',
        '  (generator kiuc)',
        f'  (uuid "{sch_uuid}")',
        '',
        f'  (paper {_paper_token})',
        '',
        '  (title_block',
    ]
    if tb.get('title'):
        lines.append(f'    (title "{_esc(tb["title"])}")')
    if tb.get('rev'):
        lines.append(f'    (rev "{_esc(tb["rev"])}")')
    if tb.get('date'):
        lines.append(f'    (date "{_esc(tb["date"])}")')
    if tb.get('company'):
        lines.append(f'    (company "{_esc(tb["company"])}")')
    lines += ['  )', '']

    # lib_symbols (embedded geometry)
    lines += _build_lib_symbols(sheet)
    lines.append('')

    # Build wire/bus segment lists for label POE connectivity check.
    # Bus segments are included so the annotation-label emission can distinguish
    # "POE on bus only" (decorative bus label → use anchor) from "POE on wire
    # that terminates at a bus-entry junction" (wirelabel → must also use anchor,
    # since the junction point coincides with the bus, and placing a KiCad net
    # label there names the entire bus rather than just the wire).
    _wire_segments: list = [(w.x1, w.y1, w.x2, w.y2)
                            for w in sheet.wires if not w.is_bus]
    _bus_segments:  list = [(w.x1, w.y1, w.x2, w.y2)
                            for w in sheet.wires if w.is_bus]
    _bus_entry_poes: set = {(j.x, j.y) for j in sheet.junctions if j.is_bus_entry}
    _wire_networks_list: list = _wire_networks(sheet)
    _bus_networks_list: list = _bus_networks(sheet)

    # ── Diagonal wire → bus_entry size resolution ─────────────────────────────
    # Diagonal wires (x1≠x2 and y1≠y2) that have one endpoint on a bus are
    # Ulticap's way of drawing a slanted wire-to-bus connection.  In KiCad
    # these become (bus_entry (size sx sy)) rather than (wire).
    # The matching bus_entry *V junction sits at the bus endpoint of the diagonal.
    # We build a dict keyed on the bus-endpoint Ulticap coords so we can look up
    # the size when emitting that junction's bus_entry.

    _bus_segs: list = [(w.x1, w.y1, w.x2, w.y2)
                       for w in sheet.wires if w.is_bus]

    # Map bus-entry point (Ulticap ints) → list of (sx_mm, sy_mm) from diagonal
    # wires.  A single bus point can be the entry for MULTIPLE diagonal wires
    # (e.g. four signal wires converging on one bus corner from different
    # directions), each needing its own (bus_entry (size ...)) element.
    _bus_entry_size: dict = {}      # (x, y) → [(sx, sy), ...]
    _diagonal_wire_ids: set = set() # id(w) for diagonal wires to suppress

    for w in sheet.wires:
        if w.is_bus:
            continue
        if w.x1 == w.x2 or w.y1 == w.y2:
            continue                # orthogonal wire — keep as-is
        # Diagonal wire: determine which endpoint is on a bus
        p1_on = any(_point_on_ortho_segment(w.x1, w.y1, *b) for b in _bus_segs)
        p2_on = any(_point_on_ortho_segment(w.x2, w.y2, *b) for b in _bus_segs)
        if not (p1_on or p2_on):
            continue                # diagonal not connected to bus — keep as-is
        bus_u  = (w.x1, w.y1) if p1_on else (w.x2, w.y2)
        wire_u = (w.x2, w.y2) if p1_on else (w.x1, w.y1)
        # KiCad size = KiCad(wire_pt) - KiCad(bus_pt)
        sx = tr.x(wire_u[0]) - tr.x(bus_u[0])
        sy = tr.y(wire_u[1]) - tr.y(bus_u[1])
        _bus_entry_size.setdefault(bus_u, []).append((sx, sy))
        _diagonal_wire_ids.add(id(w))

    # ── Perpendicular (no-diagonal) wire-to-bus stub resolution ───────────────
    # Some Ulticap wire-to-bus junctions have no diagonal stub at all — a
    # plain orthogonal wire just terminates exactly on the bus.  KiCad's GUI
    # never draws this (its bus-entry tool only places 45° diagonal stubs),
    # but a single-axis (size sx 0) / (size 0 sy) entry is spec-legal and
    # was verified against both the generated netlist and ERC in KiCad
    # 9.0.7: it resolves cleanly (only a benign off-grid ERC note), whereas
    # the (0,0) fallback this used to default to is electrically coincident
    # but ERC-flagged as "Unconnected wire to bus entry" / "Unconnected
    # wire endpoint" in every case tested. We mimic the perpendicular entry
    # by shortening the wire's bus-side endpoint by half this sheet's grid
    # spacing and emitting a matching single-axis bus_entry across the gap.
    #
    # This only ever changes what gets WRITTEN for the affected wire's
    # endpoint — the underlying Wire model object is never mutated, so
    # every other consumer of sheet.wires (bus net-name collection, bus
    # network union-find, label POE matching, etc.) keeps seeing the
    # original coordinates and is unaffected by this resolution.
    #
    # Half the sheet's own grid is used (rather than a fixed mm constant)
    # so the shortened point can never land exactly on a position where
    # genuinely-placed Ulticap content could exist — real content always
    # sits on whole-grid multiples of *this* file's grid, whatever that is.
    _STUB_U = (sheet.grid or 25) / 2.0   # Ulticap units; commonly fractional

    # id(wire) -> {'x1': (new_x_u, new_y_u), 'x2': (new_x_u, new_y_u)}
    # A dict-of-dicts so a wire running directly between two bus points
    # (both ends are bus_entry junctions) can have each end shortened
    # independently without one overwriting the other.
    _wire_endpoint_override: dict = {}

    def _segment_blocks(px_u: float, py_u: float, skip_id: int) -> bool:
        """True if some other wire/bus occupies Ulticap point (px_u,py_u)."""
        for w2 in sheet.wires:
            if id(w2) == skip_id:
                continue
            if w2.x1 == w2.x2 or w2.y1 == w2.y2:
                if _point_on_ortho_segment(px_u, py_u, w2.x1, w2.y1, w2.x2, w2.y2):
                    return True
            elif (px_u, py_u) == (w2.x1, w2.y1) or (px_u, py_u) == (w2.x2, w2.y2):
                return True   # diagonal wire endpoint coincidence
        return False

    for j in sheet.junctions:
        if not j.is_bus_entry:
            continue
        # NOTE: deliberately not skipping bus-to-bus corners (n_bus_segs >= 2)
        # here. A corner and a wire-to-bus entry are not mutually exclusive —
        # an ordinary signal wire can terminate exactly at the point where two
        # bus segments meet, needing both the corner's own (junction) AND a
        # (bus_entry) for that wire. This mirrors the diagonal-wire detection
        # pass above, which has never excluded corners for the same reason.
        # Collision safety at corners is unaffected: _segment_blocks already
        # checks against ALL wires (bus and non-bus alike), so a candidate
        # stub point that would land on either of the corner's two bus
        # segments is rejected the same way a stub colliding with any other
        # wire would be.

        # Orthogonal, non-bus wires terminating exactly at this bus point.
        candidates = [w for w in sheet.wires
                      if not w.is_bus and (w.x1 == w.x2 or w.y1 == w.y2)
                      and ((w.x1, w.y1) == (j.x, j.y) or (w.x2, w.y2) == (j.x, j.y))]
        new_sizes = []
        for w in candidates:
            at_p1 = (w.x1, w.y1) == (j.x, j.y)
            bx_u, by_u = (w.x1, w.y1) if at_p1 else (w.x2, w.y2)
            fx_u, fy_u = (w.x2, w.y2) if at_p1 else (w.x1, w.y1)
            horizontal = (by_u == fy_u)
            length_u = abs(fx_u - bx_u) if horizontal else abs(fy_u - by_u)
            # Require enough slack for both ends of this same wire to be
            # shortened independently without inverting or overlapping.
            if length_u <= 4 * _STUB_U:
                continue   # too short to shorten safely — leave on (0,0)
            delta = (fx_u - bx_u) if horizontal else (fy_u - by_u)
            sign = 1 if delta > 0 else -1
            if horizontal:
                new_x_u, new_y_u = bx_u + sign * _STUB_U, by_u
            else:
                new_x_u, new_y_u = bx_u, by_u + sign * _STUB_U
            if _segment_blocks(new_x_u, new_y_u, id(w)):
                continue   # would coincide with unrelated geometry — leave on (0,0)
            sx = tr.x(new_x_u) - tr.x(bx_u)
            sy = tr.y(new_y_u) - tr.y(by_u)
            new_sizes.append((sx, sy))
            ov = _wire_endpoint_override.setdefault(id(w), {})
            ov['x1' if at_p1 else 'x2'] = (new_x_u, new_y_u)

        if new_sizes:
            _bus_entry_size.setdefault((j.x, j.y), []).extend(new_sizes)

    # ── Wires and buses ────────────────────────────────────────────────────────
    # Suppress diagonal wires that are replaced by bus_entry elements; apply
    # any perpendicular-stub endpoint overrides to the rest. Wires that carry
    # a junction strictly on their interior are pre-split at each such point
    # (mirroring KiCad's own editor behaviour for a run with a mid-wire tap)
    # to avoid a label_multiple_wires false positive -- see
    # _interior_junction_points().
    _all_junction_pts = {(j.x, j.y) for j in sheet.junctions}
    for w in sheet.wires:
        if id(w) in _diagonal_wire_ids:
            continue
        ov = _wire_endpoint_override.get(id(w))
        ox1 = oy1 = ox2 = oy2 = None
        if ov:
            ox1, oy1 = ov.get('x1', (None, None))
            ox2, oy2 = ov.get('x2', (None, None))

        split_pts = _interior_junction_points(w, _all_junction_pts)
        if not split_pts:
            lines += _wire_sexp(w, tr, ox1, oy1, ox2, oy2)
            continue

        chain = [(w.x1 if ox1 is None else ox1, w.y1 if oy1 is None else oy1)]
        chain += split_pts
        chain.append((w.x2 if ox2 is None else ox2, w.y2 if oy2 is None else oy2))
        for i in range(len(chain) - 1):
            (px1, py1), (px2, py2) = chain[i], chain[i + 1]
            lines += _wire_sexp(w, tr, px1, py1, px2, py2)

    # User/graphic lines
    for ul in sheet.user_lines:
        lines += _userline_sexp(ul, tr)

    # ── Junctions and bus entries ──────────────────────────────────────────────
    # is_bus_entry == True, on exactly 1 bus seg → wire-to-bus → (bus_entry)
    #   size comes from the matching diagonal wire(s) if any, else (0,0).
    # is_bus_entry == True, on 2+ bus segs → bus-to-bus corner/intersection.
    #   This ALSO emits a (junction) for the corner itself.  If the point is
    #   ADDITIONALLY the entry point for one or more diagonal OR perpendicular
    #   signal wires (recorded in _bus_entry_size by the two detection passes
    #   above), each of those wires still needs its own (bus_entry) — a bus
    #   corner and a wire-to-bus entry are not mutually exclusive; both can
    #   occupy the same point.
    # is_bus_entry == False → wire-to-wire junction → (junction)
    #
    # Note: Ulticap sets junction_type=1 for ALL junctions involving a bus,
    # including bus-to-bus intersections, so the n_bus_segs check is required
    # to distinguish the two cases.
    for j in sheet.junctions:
        if j.is_bus_entry:
            n_bus = sum(1 for s in _bus_segs
                        if _point_on_ortho_segment(j.x, j.y, *s))
            sizes = _bus_entry_size.get((j.x, j.y))
            if n_bus >= 2:
                lines += _junction_sexp(j, tr)   # bus-to-bus corner/intersection
                for sx, sy in (sizes or []):
                    lines += _bus_entry_sexp(j, tr, sx, sy)
            else:
                for sx, sy in (sizes or [(0.0, 0.0)]):
                    lines += _bus_entry_sexp(j, tr, sx, sy)
        else:
            lines += _junction_sexp(j, tr)

    # ── Bus aliases and bus labels ─────────────────────────────────────────────
    # One alias + label per connected bus network. Networks are determined by
    # _bus_networks() via endpoint-sharing and junction-mediated mid-segment
    # connections; segments that merely cross without a junction stay separate.
    #
    # _bus_vector_labels records, for each network that resolves to a VECTOR
    # BUS (local or project-wide port), the segments belonging to that network
    # plus its true, fully-derived label text — i.e. the name that actually
    # reflects every wire-to-bus junction found above, not whatever text a
    # stale *A LABEL= annotation happens to claim. Used below to detect and
    # suppress *A LABEL annotations that name only a SUBSET of a bus whose
    # real range is wider (e.g. a leftover "D[0..7]" annotation on a bus
    # later extended to D8..D15 via plain wirelabels, where the bus's true
    # label is now "ISA_D[0..15]" — note the prefix itself can differ from
    # the stale annotation's, so this matches on POE-on-network + vector-style
    # text + non-equality to the network's real label, not on prefix).
    # KiCad does not support two differently-ranged bus labels coexisting
    # cleanly on one bus, and the narrower/stale one is the wrong one since
    # it doesn't reflect the bus's actual current connections.
    _bus_vector_labels: list = []   # [(net_segs, label_text), ...]

    if _bus_segs:
        _bus_counter = getattr(sheet, '_bus_counter', 1)

        for _net_segs in _bus_networks(sheet):
            _bus_names = _collect_bus_net_names(sheet, _net_segs)
            _port_name = _bus_network_port_name(sheet, _net_segs)
            _local_vector = _try_vector_bus(_bus_names)

            if _port_name is None and not _bus_names:
                continue   # no labeled net and no hierarchical port — skip

            # _bus_network_label_text (single-sourced from the shared,
            # cross-sheet _port_label_text table) gives the exact text to
            # use for bus label, hierlabel and sheet pin so all three match.
            # PORT[M..N] → verbatim; anything else → {NAME}. For single-sheet
            # conversions it returns None (no cross-sheet port); fall back to
            # local derivation below.
            label_text = _bus_network_label_text(sheet, _net_segs)
            _vec_m = _VECTOR_BRACKET_RE.match(label_text) if label_text else None

            if label_text is not None:
                if not _vec_m:
                    # Group-bus: emit alias with project-wide union members
                    _members = getattr(sheet, '_port_alias_members', {}).get(
                        _port_name, _bus_names)
                    if _members:
                        lines += _bus_alias_sexp(_port_name, _members)
                else:
                    _bus_vector_labels.append((_net_segs, label_text))
            elif _local_vector:
                prefix, M, N = _local_vector
                label_text = f'{prefix}[{M}..{N}]'
                _bus_vector_labels.append((_net_segs, label_text))
                # VECTOR BUS: label only, no bus_alias needed
            else:
                alias_name = f'BUS{_bus_counter}'
                label_text = '{' + alias_name + '}'
                _bus_counter += 1
                lines += _bus_alias_sexp(alias_name, _bus_names)

            # Place bus label on this network — but only if no *A annotation
            # already provides an identical label whose POE lands on this network.
            # Ulticap places *A LABEL= annotations directly on buses to name them;
            # emitting a second auto-generated label on the same bus is redundant.
            lx_u, ly_u = _bus_label_position(sheet, _net_segs)
            _net_endpoints = set()
            for _s in _net_segs:
                _net_endpoints.add((_s[0], _s[1]))
                _net_endpoints.add((_s[2], _s[3]))

            def _poe_on_this_net(px, py):
                if any(_point_on_ortho_segment(px, py, *_s) for _s in _net_segs):
                    return True
                # Also check bus connectivity (segments joined at endpoints)
                return any(_point_on_ortho_segment(px, py, *_s)
                           for _s in _bus_segs
                           if (_s[0], _s[1]) in _net_endpoints or
                              (_s[2], _s[3]) in _net_endpoints)

            # label_text for a group ("named") bus is wrapped as '{NAME}',
            # but the *A annotation's own stored text is never wrapped --
            # Ulticap's raw LABEL=NAME has no braces; wrapping is a KiCad-
            # notation step applied only to the auto-generated side above.
            # An exact-string comparison against label_text therefore never
            # matches for group buses, so also compare against the name with
            # the wrapping stripped. Vector-bus names are never wrapped, so
            # this is a no-op for them (the two forms are identical).
            _label_text_unwrapped = (
                label_text[1:-1]
                if label_text.startswith('{') and label_text.endswith('}')
                else label_text)
            _ann_covers = any(
                ann.tag == 'LABEL' and ann.text in (label_text, _label_text_unwrapped)
                and _poe_on_this_net(ann.x_poe, ann.y_poe)
                for ann in sheet.annotations
            )
            if not _ann_covers:
                lines += _bus_label_sexp(label_text,
                                         tr.x(lx_u), tr.y(ly_u))

        sheet._bus_counter = _bus_counter

    # Pre-assign #PWRnn references — one unique number per placed power instance.
    # Power symbol = exactly one pin with PINTYPE=PWR.
    # Net connectivity is determined by the Value property (net name), not by
    # the reference, so unique #PWRnn per instance does not break net grouping.
    if not getattr(sheet, '_pwr_refs', None):
        _pwr_counter = 0
        _pwr_refs_map: dict = {}
        for _pc in sheet.components:
            _s = sheet.symbols.get(_pc.symbol_name)
            if (_s is not None and
                    len(_s.pins) == 1 and
                    _s.pins[0].pin_type.upper() == 'PWR'):
                _pwr_counter += 1
                _pwr_refs_map[id(_pc)] = f'#PWR{_pwr_counter:02d}'
        sheet._pwr_refs = _pwr_refs_map

    # Per-sheet collision tracker for _sanitize_port_name: maps sanitized
    # port name → original name (first user).  Initialized fresh each time
    # _build_sch runs so it covers both hierarchical_label (via _comp_sexp)
    # and sheet-pin (via _sheet_sexp) emissions on this sheet.
    sheet._port_name_map = {}

    # Component placements
    _tb_counter: dict = {}   # tracks TITLE/TITLE_REV reference sequencing
    for comp in sheet.components:
        if suppress_title and comp.symbol_name in ('TITLE', 'TITLE_REV'):
            continue  # block mode: omit title block symbols
        lines += _comp_sexp(comp, sheet, tr, _tb_counter)

    # ── Stale bus-label detection ───────────────────────────────────────────────
    # A *A LABEL= annotation placed directly on a bus (POE on a bus segment)
    # whose own text parses as PREFIX[m..n] is meant to name that bus. If the
    # network it sits on actually has a DIFFERENT true label (per
    # _bus_vector_labels, derived from real wire-to-bus junctions above), the
    # annotation is stale leftover data — e.g. a "D[0..7]" label kept on a
    # bus that was later extended to D8..D15 via plain wirelabels, where the
    # bus's real label is now "ISA_D[0..15]" (note the prefix itself differs
    # from the stale annotation's — this can't be detected by comparing
    # numeric ranges under a shared prefix, only by the fact that the bus's
    # actual derived label text doesn't match what the annotation claims).
    # KiCad does not support two differently-named bus labels coexisting
    # cleanly on one bus, and the stale one is the wrong one — it doesn't
    # reflect the bus's actual current connections — so it is dropped here
    # rather than emitted.
    #
    # An EXACT match is deliberately NOT touched: that is the normal case
    # where the annotation already IS the bus's correct, complete label, and
    # the auto-generated alias/label loop above already suppresses its own
    # duplicate emission for that case (_ann_covers) — only this annotation's
    # own label is meant to render.
    _stale_bus_label_ids: set = set()
    if _bus_vector_labels:
        for ann in sheet.annotations:
            if ann.tag != 'LABEL':
                continue
            if not _VECTOR_BRACKET_RE.match(ann.text):
                continue   # not a vector-style bus label name — leave alone
            for _segs, _true_label in _bus_vector_labels:
                if not any(_point_on_ortho_segment(ann.x_poe, ann.y_poe, *_s) for _s in _segs):
                    continue   # POE not on this network
                if ann.text != _true_label:
                    _stale_bus_label_ids.add(id(ann))
                break   # POE can only be on one network

    # Free text annotations
    for ann in sheet.annotations:
        if ann.tag == 'LABEL':
            if id(ann) in _stale_bus_label_ids:
                continue   # superseded by the bus's actual (wider) auto-generated label
            _ann_render = ann
            # A LABEL= annotation sitting on a bus must render the same
            # {NAME}-wrapped text _bus_network_label_text would derive for
            # that network (see its docstring) -- this annotation is the one
            # actually rendering the bus's label when present, since
            # auto-generation is suppressed above (_ann_covers) once an
            # annotation already covers the network.
            if _bus_segments and any(
                    _point_on_ortho_segment(ann.x_poe, ann.y_poe, *s) for s in _bus_segments):
                for _net_segs in _bus_networks_list:
                    if not any(_point_on_ortho_segment(ann.x_poe, ann.y_poe, *s)
                               for s in _net_segs):
                        continue
                    _wrapped = _bus_network_label_text(sheet, _net_segs)
                    if _wrapped and _wrapped != ann.text:
                        _ann_render = type(ann)(
                            x=ann.x, y=ann.y, x_poe=ann.x_poe, y_poe=ann.y_poe,
                            text=_wrapped, size=ann.size, colour=ann.colour,
                            visibility=ann.visibility, tag=ann.tag)
                    break   # POE can only be on one network
            lines += _annot_label_sexp(_ann_render, tr, _wire_segments, _bus_segments,
                                       _bus_entry_poes, _wire_networks_list,
                                       _bus_networks_list)
        else:
            lines += _text_sexp(ann, tr)

    # Net labels
    for lbl in sheet.labels:
        lines += _x_text_sexp(lbl, tr)

    # ── Sheet instances ───────────────────────────────────────────────────────
    # Only the root sheet (sheet_path == '/') carries sheet_instances.
    # For multi-sheet designs, write_schematic patches the root file afterward
    # with the full table.  Subsheets have no sheet_instances block.
    if sheet_path == '/':
        lines += [
            '',
            '  (sheet_instances',
            '    (path "/"',
            f'      (page "{page_num}")',
            '    )',
            '  )',
        ]

    lines.append(')')
    return lines


# ── public API ─────────────────────────────────────────────────────────────────

def write_kicad_sch(sheet: Sheet, out_path,
                    warnings: bool = True,
                    subsheet_map: dict = None,
                    sheet_path: str = '/',
                    page_num: int = 1,
                    sch_uuid: str = None,
                    proj_name: str = None,
                    suppress_title: bool = False) -> List[str]:
    """Write one Sheet as a KiCad 9 .kicad_sch file.
    Returns list of warning strings (empty if none).
    """
    out_path = Path(out_path)
    lines = _build_sch(sheet,
                       subsheet_map=subsheet_map,
                       sheet_path=sheet_path,
                       page_num=page_num,
                       sch_uuid=sch_uuid,
                       proj_name=proj_name,
                       suppress_title=suppress_title)
    out_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return []




def _dfs_order(root_sheet, subsheet_map: dict):
    """Return all sheets in DFS pre-order starting from root_sheet.

    Pre-order guarantees every parent is written before its children, which is
    required because write_kicad_sch populates _sheet_elem_uuids on a sheet as
    a side-effect — children need those UUIDs to build their instance paths.

    Children at each level are sorted alphabetically by filename so that page
    numbers are assigned in a predictable order within each subtree while
    keeping each subtree contiguous.

    Cycles (a sheet referencing itself or an ancestor) are silently skipped via
    the visited set.
    """
    order = []
    visited = set()

    def _visit(sh):
        key = id(sh)
        if key in visited:
            return
        visited.add(key)
        order.append(sh)
        children = []
        for hc in sh.components:
            if not _is_hier_sheet(hc):
                continue
            ref = (hc.file_ref or '').upper()
            child = subsheet_map.get(ref)
            if child is not None:
                children.append((ref, child))
        children.sort(key=lambda rc: rc[0])
        for _, child in children:
            _visit(child)

    _visit(root_sheet)
    return order


# ── ERC rule severities ──────────────────────────────────────────────────────
# Deliberately NOT KiCad 9.0.7's stock defaults — confirmed by diffing against
# a freshly-created project's actual erc.rule_severities. Differs at 8 keys
# (footprint_link_issues, lib_symbol_issues, missing_unit, pin_not_connected,
# pin_not_driven, pin_to_pin, power_pin_not_driven, unconnected_wire_endpoint),
# tuned specifically for schematics produced by this converter — e.g. softening
# pin_not_connected/unconnected_wire_endpoint to account for legitimate
# converter artifacts (hidden hidden-pin power_in connections, off-grid stub
# geometry) that would otherwise read as false ERC failures under the stock
# defaults. Always force-overwritten on every (re-)conversion rather than
# merged with whatever is already on disk: there is no reliable way for this
# converter to tell whether an existing .kicad_pro's ERC settings were
# hand-tuned by the user afterward or are simply stale from a prior run, and
# detecting whether the *source* Ulticap schematic changed between runs is out
# of scope — so the converter's own recommended baseline always wins.
_ERC_RULE_SEVERITIES: dict = {
    "bus_definition_conflict": "error",
    "bus_entry_needed": "error",
    "bus_to_bus_conflict": "error",
    "bus_to_net_conflict": "error",
    "different_unit_footprint": "error",
    "different_unit_net": "error",
    "duplicate_reference": "error",
    "duplicate_sheet_names": "error",
    "endpoint_off_grid": "ignore",
    "extra_units": "error",
    "footprint_filter": "ignore",
    "footprint_link_issues": "ignore",
    "four_way_junction": "ignore",
    "global_label_dangling": "warning",
    "hier_label_mismatch": "error",
    "label_dangling": "error",
    "label_multiple_wires": "warning",
    "lib_symbol_issues": "ignore",
    "lib_symbol_mismatch": "ignore",
    "missing_bidi_pin": "warning",
    "missing_input_pin": "warning",
    "missing_power_pin": "error",
    "missing_unit": "ignore",
    "multiple_net_names": "warning",
    "net_not_bus_member": "warning",
    "no_connect_connected": "warning",
    "no_connect_dangling": "warning",
    "pin_not_connected": "ignore",
    "pin_not_driven": "warning",
    "pin_to_pin": "error",
    "power_pin_not_driven": "warning",
    "same_local_global_label": "warning",
    "similar_label_and_power": "warning",
    "similar_labels": "warning",
    "similar_power": "warning",
    "simulation_model_issue": "ignore",
    "single_global_label": "ignore",
    "unannotated": "error",
    "unconnected_wire_endpoint": "ignore",
    "undefined_netclass": "error",
    "unit_value_mismatch": "error",
    "unresolved_variable": "error",
    "wire_dangling": "error",
}


# ── border worksheet ──────────────────────────────────────────────────────────
# A .kicad_wks file that draws a plain sheet border with column (1, 2, 3 …)
# and row (A, B, C …) reference markers, matching the style produced by
# KiCad's pl_editor.  No title-block text fields are included: the Ulticap
# TITLE/TITLE_REV symbols are rendered as ordinary placed components at their
# original Ulticap positions, so the worksheet must not duplicate them.
#
# Margins are 10 mm on all sides (matching the uploaded border.kicad_wks).
# The repeat/incrx/incry mechanism in KiCad's page-layout engine fills as
# many tick marks and labels as the paper size requires.
_BORDER_WKS: str = """\
(kicad_wks (version 20231118) (generator kiuc)
\t(setup
\t\t(textsize 1.5 1.5)
\t\t(linewidth 0.15)
\t\t(textlinewidth 0.15)
\t\t(left_margin 10)
\t\t(right_margin 10)
\t\t(top_margin 10)
\t\t(bottom_margin 10)
\t)
\t(rect
\t\t(name "")
\t\t(start 0 0 ltcorner)
\t\t(end 0 0)
\t\t(repeat 2)
\t\t(incrx 2)
\t\t(incry 2)
\t)
\t(line
\t\t(name "")
\t\t(start 50 2 ltcorner)
\t\t(end 50 0 ltcorner)
\t\t(repeat 30)
\t\t(incrx 50)
\t)
\t(tbtext "1"
\t\t(name "")
\t\t(pos 25 1 ltcorner)
\t\t(font
\t\t\t(size 1.3 1.3)
\t\t)
\t\t(repeat 100)
\t\t(incrx 50)
\t)
\t(line
\t\t(name "")
\t\t(start 50 2 lbcorner)
\t\t(end 50 0 lbcorner)
\t\t(repeat 30)
\t\t(incrx 50)
\t)
\t(tbtext "1"
\t\t(name "")
\t\t(pos 25 1 lbcorner)
\t\t(font
\t\t\t(size 1.3 1.3)
\t\t)
\t\t(repeat 100)
\t\t(incrx 50)
\t)
\t(line
\t\t(name "")
\t\t(start 0 50 ltcorner)
\t\t(end 2 50 ltcorner)
\t\t(repeat 30)
\t\t(incry 50)
\t)
\t(tbtext "A"
\t\t(name "")
\t\t(pos 1 25 ltcorner)
\t\t(font
\t\t\t(size 1.3 1.3)
\t\t)
\t\t(justify center)
\t\t(repeat 100)
\t\t(incry 50)
\t)
\t(line
\t\t(name "")
\t\t(start 0 50 rtcorner)
\t\t(end 2 50 rtcorner)
\t\t(repeat 30)
\t\t(incry 50)
\t)
\t(tbtext "A"
\t\t(name "")
\t\t(pos 1 25 rtcorner)
\t\t(font
\t\t\t(size 1.3 1.3)
\t\t)
\t\t(justify center)
\t\t(repeat 100)
\t\t(incry 50)
\t)
)
"""


def _write_border_wks(out_dir: Path, project_name: str) -> str:
    """Write <project_name>.kicad_wks with a sheet border and reference markers.

    The border suppresses KiCad's built-in title block (no title-block fields
    are included) while adding a plain engineering border with column (1, 2 …)
    and row (A, B …) markers.  The Ulticap TITLE/TITLE_REV symbols are
    rendered as ordinary placed components at their original positions and are
    unaffected by the worksheet.
    Returns the filename (not full path) for use in .kicad_pro references.
    """
    wks_filename = f'{project_name}.kicad_wks'
    wks_path = out_dir / wks_filename
    wks_path.write_text(_BORDER_WKS, encoding='utf-8')
    return wks_filename


def write_kicad_pro(out_dir, project_name: str, root_sheet: Sheet,
                    wks_name: str = '') -> None:
    """Create or update <project_name>.kicad_pro alongside the .kicad_sch(es).

    One project file per OUTPUT project: called once per independent design
    (including each independently-written sheet in a multi-unrelated-files
    GUI selection) and once for a hierarchical design's root sheet only —
    never per sub-sheet, matching KiCad's one-project-per-root-hierarchy model.

    Two cases:

    1. No .kicad_pro at this path yet: a brand-new file is written, using
       _KICAD_PRO_TEMPLATE_JSON (a verbatim freshly-created-by-KiCad-9.0.7
       project) as the literal base, so every required top-level section
       (board design rules, net classes, pcbnew paths, etc.) is present and
       structurally valid from the start. Only meta.filename,
       erc.rule_severities, and schematic.connection_grid_size are set.

    2. A .kicad_pro already exists (most commonly because KIUB created one
       first, often carrying KIUB's own custom PCB/board settings from a DDF
       conversion run): loaded and surgically updated in place. ONLY
       meta.filename, erc.rule_severities, schematic.connection_grid_size,
       and the top-level "sheets" array are touched; every other key —
       board, net_settings, pcbnew, libraries, every other top-level meta
       field, boards, cvpcb, text_variables, and every other schematic.*
       key — is left exactly as found.

       meta.filename is corrected to this project's own name rather than
       left as whatever a copied-from-elsewhere file originally had — this
       mirrors what KiCad itself does the moment the user opens the matching
       .kicad_sch and saves, just proactively rather than waiting on that
       manual step.

       "sheets" is blanked to [] rather than left untouched: confirmed
       empirically that a fresh-from-KiCad project already has this as []
       even after a full open+save cycle, and that KiCad fully regenerates
       it from the actual .kicad_sch hierarchy on next save regardless of
       what was there before — including recovering correctly from a
       single-sheet project's array being reused for a hierarchical design.
       So there's no reason to risk carrying over stale per-sheet UUID/name
       entries from an unrelated prior design; starting from [] is at least
       as safe as anything else and avoids exactly the kind of cross-design
       leftover data meta.filename also needed correcting for.

    connection_grid_size is written in mils (KiCad's unit for this field,
    confirmed against the reference project: default 50.0 = the documented
    50 mil schematic grid). 1 Ulticap unit = 2 mils, so the value is simply
    root_sheet.grid * 2.0 — taken from the ROOT sheet specifically, since one
    .kicad_pro corresponds to one project regardless of how many sheets it
    contains, and sub-sheets are not expected to use a different grid than
    their own project's root.
    """
    out_dir = Path(out_dir)
    pro_path = out_dir / f'{project_name}.kicad_pro'
    grid_mils = (root_sheet.grid or 25) * 2.0

    if pro_path.exists():
        try:
            with open(pro_path, 'r', encoding='utf-8') as f:
                data = _json.load(f)
        except (_json.JSONDecodeError, OSError):
            # Corrupt or unreadable existing file — do not attempt to patch
            # something we can't parse; fall back to the clean template
            # rather than risk silently destroying user/KIUB data further.
            data = _json.loads(_KICAD_PRO_TEMPLATE_JSON)
            data['meta']['filename'] = f'{project_name}.kicad_pro'

        data.setdefault('erc', _json.loads(_KICAD_PRO_TEMPLATE_JSON)['erc'])
        data['erc']['rule_severities'] = dict(_ERC_RULE_SEVERITIES)

        data.setdefault('schematic',
                        _json.loads(_KICAD_PRO_TEMPLATE_JSON)['schematic'])
        data['schematic']['connection_grid_size'] = grid_mils
        if wks_name:
            data['schematic']['page_layout_descr_file'] = wks_name

        data.setdefault('meta', {})
        data['meta']['filename'] = f'{project_name}.kicad_pro'

        data['sheets'] = []

        data.setdefault('pcbnew', _json.loads(_KICAD_PRO_TEMPLATE_JSON)['pcbnew'])
        if wks_name:
            data['pcbnew']['page_layout_descr_file'] = wks_name
    else:
        data = _json.loads(_KICAD_PRO_TEMPLATE_JSON)
        data['meta']['filename'] = f'{project_name}.kicad_pro'
        data['erc']['rule_severities'] = dict(_ERC_RULE_SEVERITIES)
        data['schematic']['connection_grid_size'] = grid_mils
        if wks_name:
            data['schematic']['page_layout_descr_file'] = wks_name
            data['pcbnew']['page_layout_descr_file'] = wks_name

    with open(pro_path, 'w', encoding='utf-8') as f:
        _json.dump(data, f, indent=2)
        f.write('\n')


def write_schematic(schematic: Schematic, out_dir,
                    base_name: str = 'schematic') -> List[str]:
    """Write a Schematic (one or more sheets) to .kicad_sch files.
    Returns list of warning strings.

    Three cases:

    1. Single sheet — written as base_name.kicad_sch.

    2. Multiple sheets that form a hierarchy (every sheet is reachable via DFS
       from the root through *C FILE= references) — hierarchical write: parent
       UUIDs, instance paths, page numbers, and bus-alias unions are all
       computed project-wide.

    3. Multiple sheets with no shared hierarchy (e.g. the user selected several
       unrelated SCH files in the GUI) — each sheet is written independently as
       stem.kicad_sch, exactly as if each had been converted on its own.  This
       is detected when _dfs_order from the identified root does not cover all
       provided sheets; the unreachable ones are silently written flat.  The
       user sees one 'Written: …' log line per file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    warns = check_missing_sheets(schematic.sheets)

    # Single-sheet: no hierarchy, simple path
    if len(schematic.sheets) == 1:
        wks_name = _write_border_wks(out_dir, base_name)
        write_kicad_sch(schematic.sheets[0],
                        out_dir / f'{base_name}.kicad_sch')
        write_kicad_pro(out_dir, base_name, schematic.sheets[0],
                        wks_name=wks_name)
        return warns

    # Multi-sheet: build subsheet_map (normalised-uppercase-filename → Sheet)
    subsheet_map = _build_subsheet_map(schematic.sheets)

    # Identify root sheet: the one whose root_sheet attr matches its own stem,
    # or the first sheet if not determinable.
    root_sheet = schematic.sheets[0]
    for sh in schematic.sheets:
        stem = Path(sh.name).stem.upper() if sh.name else ''
        rs   = sh.root_sheet.upper() if sh.root_sheet else ''
        if stem and rs and stem == rs:
            root_sheet = sh
            break

    # DFS from the root to find the hierarchically reachable subset.
    dfs_sheets = _dfs_order(root_sheet, subsheet_map)
    dfs_ids    = {id(sh) for sh in dfs_sheets}

    # Sheets not reachable from the root have no parent-child relationship with
    # the others — they were selected as independent files.  Write them flat and
    # exclude them from the hierarchical pass so they don't corrupt page numbers
    # or bus-alias unions for the real hierarchy.
    independent_sheets = [sh for sh in schematic.sheets if id(sh) not in dfs_ids]
    for sh in independent_sheets:
        stem = Path(sh.name).stem if sh.name else base_name
        _ind_wks = _write_border_wks(out_dir, stem)
        write_kicad_sch(sh, out_dir / f'{stem}.kicad_sch')
        write_kicad_pro(out_dir, stem, sh, wks_name=_ind_wks)

    # If ALL sheets were independent (no hierarchy at all), we're done.
    if not dfs_sheets:
        return warns

    # DFS pre-order: root first, then each subtree fully before the next sibling.
    # This is the natural KiCad navigator order and guarantees parents are
    # written (and their _sheet_elem_uuids populated) before children need them.
    # (dfs_sheets already computed above when detecting independent sheets.)

    # Assign page numbers in DFS order: root = 1, children before siblings.
    page_map: dict = {}
    for i, sh in enumerate(dfs_sheets, start=1):
        page_map[id(sh)] = i

    # Pre-stamp every sheet's page number so _sheet_sexp can read it for the
    # (instances (page "N")) block inside each (sheet ...) placement element.
    # Must happen before any write_kicad_sch call, including the root's.
    for sh in dfs_sheets:
        sh._child_page_num = page_map[id(sh)]

    # Pre-generate root sch_uuid so all subsheet paths can reference it.
    root_sch_uuid = _uid()
    root_proj_name = Path(root_sheet.name).stem if root_sheet.name else base_name

    # Pre-assign #PWRnn references globally in DFS order so numbers are
    # unique project-wide regardless of nesting depth or branching.
    _pwr_counter = 0
    for _sh in dfs_sheets:
        _pwr_refs_map: dict = {}
        for _pc in _sh.components:
            _s = _sh.symbols.get(_pc.symbol_name)
            if (_s is not None and
                    len(_s.pins) == 1 and
                    _s.pins[0].pin_type.upper() == 'PWR'):
                _pwr_counter += 1
                _pwr_refs_map[id(_pc)] = f'#PWR{_pwr_counter:02d}'
        _sh._pwr_refs = _pwr_refs_map

    # Pre-compute the union of bus alias members for every port-named group
    # bus across all sheets in DFS order.  When a port-named bus (e.g. 'SPI')
    # appears on multiple sheets, each sheet may only see a subset of the
    # individual signal wires (e.g. D sees MISO+SCLK but not MOSI).  KiCad
    # requires that every bus_alias with the same name has *identical* members
    # project-wide (ERCE_BUS_ALIAS_CONFLICT otherwise), so we use the union.
    # _port_label_text maps port_name → the exact text used for the bus label,
    # the hierarchical label, and the sheet pin — all three must be identical
    # for KiCad to connect the bus across the sheet boundary without conflict:
    #   PREFIX[M..N]  → use verbatim (valid everywhere as a vector-bus name)
    #   anything else → use {NAME} (group-bus syntax, valid for bus/hierlabel/pin)
    _port_members_union: dict = {}   # port_name → set of member names
    _port_label_text: dict = {}      # port_name → bus/hierlabel/pin text
    for _sh in dfs_sheets:
        for _net_segs in _bus_networks(_sh):
            _port = _bus_network_port_name(_sh, _net_segs)
            if _port is None:
                continue
            _vec_m_pre = _VECTOR_BRACKET_RE.match(_port)
            if _vec_m_pre:
                _port_label_text[_port] = _port          # PREFIX[M..N] as-is
            else:
                _port_label_text[_port] = '{' + _port + '}'  # {NAME} form
                # Only group-bus ports need member union
                _names = _collect_bus_net_names(_sh, _net_segs)
                if _names:
                    _port_members_union.setdefault(_port, set()).update(_names)

    for _sh in dfs_sheets:
        _sh._port_alias_members = {k: sorted(v)
                                    for k, v in _port_members_union.items()}
        _sh._port_label_text = _port_label_text   # shared read-only reference

    # Collect (inst_path, page_num) for every sheet during traversal so the
    # root sheet can emit the complete sheet_instances table.
    # Per KiCad spec, instance paths start with the root schematic file UUID:
    # root="/", child="/{root_uuid}/{elem_uuid}", grandchild adds another segment.
    _si_entries: list = []   # [(path_str, page_num), ...]

    def _write_recursive(sh, inst_path: str, sch_uuid: str):
        """Write sh, then recurse into every hierarchical child it contains.

        inst_path  : KiCad instance path for sh ('/' for root,
                     '/{root_uuid}/{elem_uuid}' for depth-1 child, etc.)
        sch_uuid   : UUID assigned to sh's .kicad_sch file.
        """
        _si_entries.append((inst_path, page_map[id(sh)]))
        stem = Path(sh.name).stem if sh.name else base_name
        write_kicad_sch(sh,
                        out_dir / f'{stem}.kicad_sch',
                        subsheet_map=subsheet_map,
                        sheet_path=inst_path,
                        page_num=page_map[id(sh)],
                        sch_uuid=sch_uuid,
                        proj_name=root_proj_name)

        # _sheet_elem_uuids is populated on sh by _build_sch.
        elem_uuids  = getattr(sh, '_sheet_elem_uuids', {})
        sh_sch_uuid = getattr(sh, '_sch_uuid', sch_uuid)

        for hc in sh.components:
            if not _is_hier_sheet(hc):
                continue
            ref = (hc.file_ref or '').upper()
            child = subsheet_map.get(ref)
            if child is None:
                continue
            elem_uuid = elem_uuids.get(id(hc))
            if elem_uuid is None:
                continue
            # Instance path: /{root_sch_uuid}/{elem_uuid} for depth-1,
            # /{root_sch_uuid}/{parent_elem}/{child_elem} for depth-2, etc.
            # The root file UUID is always the first segment per KiCad spec.
            if inst_path == '/':
                child_path = f'/{sh_sch_uuid}/{elem_uuid}'
            else:
                child_path = f'{inst_path}/{elem_uuid}'
            # Stamp child's page number so _sheet_sexp can read it for the
            # (instances (page "N")) block inside the (sheet ...) placement.
            child._child_page_num = page_map[id(child)]
            _write_recursive(child, child_path, _uid())

    # Kick off from root with path '/'.
    _write_recursive(root_sheet, '/', root_sch_uuid)

    # Now all elem UUIDs are known. Patch only the sheet_instances block in
    # the already-written root file — avoids regenerating all other UUIDs.
    if len(_si_entries) > 1:
        root_stem = Path(root_sheet.name).stem if root_sheet.name else base_name
        root_path = out_dir / f'{root_stem}.kicad_sch'
        content = root_path.read_text(encoding='utf-8')
        # Build replacement block
        si_lines = ['', '  (sheet_instances']
        for si_path, si_page in _si_entries:
            si_lines += [
                f'    (path "{si_path}"',
                f'      (page "{si_page}")',
                '    )',
            ]
        si_lines += ['  )', ')']
        new_tail = '\n'.join(si_lines) + '\n'
        # Replace from the last (sheet_instances marker to end of file
        marker = '\n  (sheet_instances'
        idx = content.rfind(marker)
        if idx != -1:
            root_path.write_text(content[:idx] + new_tail, encoding='utf-8')

    # One .kicad_pro for the whole hierarchy, named after the root — never
    # per sub-sheet, matching KiCad's one-project-per-root-hierarchy model.
    _hier_wks = _write_border_wks(out_dir, root_proj_name)
    write_kicad_pro(out_dir, root_proj_name, root_sheet, wks_name=_hier_wks)

    return warns


# ══════════════════════════════════════════════════════════════════════════════
# BLOCK LIBRARY OUTPUT
# ══════════════════════════════════════════════════════════════════════════════

def write_block_library(schematic, out_dir, lib_name: str = None) -> List[str]:
    """Write a Schematic as a KiCad block library (.kicad_blocks folder).

    Output structure:
        <lib_name>.kicad_blocks/
            <sheet_stem>.kicad_block/
                <sheet_stem>.kicad_sch
                <sheet_stem>.json

    Each sheet (including root for hierarchical designs) becomes one block.
    No .kicad_pro or .kicad_wks files are written.
    TITLE / TITLE_REV symbols are suppressed.

    lib_name defaults to the stem of the first input file.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    warns = check_missing_sheets(schematic.sheets)

    # Derive library name from first sheet filename if not provided
    if not lib_name:
        first_name = schematic.sheets[0].name if schematic.sheets else 'schematic'
        lib_name = Path(first_name).stem

    lib_dir = out_dir / f'{lib_name}.kicad_blocks'
    lib_dir.mkdir(exist_ok=True)

    def _write_block(sheet: Sheet, stem: str) -> None:
        """Write one .kicad_block subfolder for a single sheet."""
        block_dir = lib_dir / f'{stem}.kicad_block'
        block_dir.mkdir(exist_ok=True)

        # .kicad_sch — suppress title symbols
        write_kicad_sch(sheet,
                        block_dir / f'{stem}.kicad_sch',
                        suppress_title=True)

        # .json metadata — name, empty description/keywords/fields
        meta = {
            'description': stem,
            'keywords': '',
            'fields': {},
        }
        (block_dir / f'{stem}.json').write_text(
            _json.dumps(meta, indent=2), encoding='utf-8')

    if len(schematic.sheets) == 1:
        stem = Path(schematic.sheets[0].name).stem if schematic.sheets[0].name else lib_name
        _write_block(schematic.sheets[0], stem)
        return warns

    # Multi-sheet: write every sheet as its own block (root included)
    subsheet_map = _build_subsheet_map(schematic.sheets)
    root_sheet = schematic.sheets[0]
    for sh in schematic.sheets:
        s = Path(sh.name).stem.upper() if sh.name else ''
        rs = sh.root_sheet.upper() if sh.root_sheet else ''
        if s and rs and s == rs:
            root_sheet = sh
            break

    dfs_sheets = _dfs_order(root_sheet, subsheet_map)
    dfs_ids = {id(sh) for sh in dfs_sheets}

    # Independent (non-hierarchical) sheets: write each as its own block
    for sh in schematic.sheets:
        if id(sh) not in dfs_ids:
            stem = Path(sh.name).stem if sh.name else lib_name
            _write_block(sh, stem)

    # Hierarchical sheets in DFS order — no inter-sheet wiring needed for blocks
    for sh in dfs_sheets:
        stem = Path(sh.name).stem if sh.name else lib_name
        _write_block(sh, stem)

    return warns
