#!/usr/bin/env python3
"""
LBA-inspired VFR navlog generator.

Reads a Little Navmap .lnmpln plan plus an aircraft JSON config, applies
a single wind aloft and a magnetic variation, and writes an A4-landscape
PDF you can print and clip to your kneeboard.

Usage:
    python3 navlog.py \
        --plan "/path/to/plan.lnmpln" \
        --aircraft aircraft_sr22.json \
        --wind 270/15 \
        --magvar 2.5E \
        --output navlog.pdf
"""
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from fpdf import FPDF

# --- Phase 1 facade: names below now live in the vfr_navlog package. This file
#     re-exports them so `from navlog import X` and the code still in this module
#     keep working until the split completes. ---
from vfr_navlog.config import (  # noqa: E402,F401
    APT_REL,
    DEFAULT_XPLANE,
    FIX_REL,
    NAV_FALLBACK_REL,
    NAV_REL,
    NAVIGRAPH_LDB,
    SURFACE_NAMES,
    UNICOM_FREQ,
    VATSIM_METAR_URL,
    VATSIM_TAF_URL,
    VATSIM_UA,
    VATSIM_URL,
    _load_env,
    _smart_output,
)
from vfr_navlog.geo import apply_wind, great_circle  # noqa: E402,F401
from vfr_navlog.geo import haversine_m as _haversine_m  # noqa: E402,F401
from vfr_navlog.model import (  # noqa: E402,F401
    AirportInfo,
    FieldWx,
    IlsLoc,
    Leg,
    ParsedMetar,
    Plan,
    Runway,
    VatsimSnapshot,
    Waypoint,
    WeatherBriefing,
)
from vfr_navlog.legs import (  # noqa: E402,F401
    _effective_leg_alt,
    apply_hemispheric_rule,
    compute_legs,
    find_call_marker,
    hemispheric_alt,
)
from vfr_navlog.lnmpln import parse_lnmpln, parse_magvar, parse_wind  # noqa: E402,F401
from vfr_navlog.navigraph import (  # noqa: E402,F401
    _airport_position,
    _build_nav_index,
    _decode_dms,
    _navigraph_plan,
    read_navigraph_flight,
)
from vfr_navlog.xplane import (  # noqa: E402,F401
    load_destination_info,
    parse_airport,
    parse_ils_locs,
)


# ------------------------- fonts -------------------------

FONT_CANDIDATES = [
    ("/System/Library/Fonts/Supplemental/Arial.ttf", ""),
    ("/System/Library/Fonts/Supplemental/Arial Bold.ttf", "B"),
    ("/System/Library/Fonts/Supplemental/Arial Italic.ttf", "I"),
    ("/System/Library/Fonts/Supplemental/Arial Bold Italic.ttf", "BI"),
]


def install_fonts(pdf: FPDF) -> str:
    """Register a Unicode TTF if available; otherwise fall back to core Helvetica."""
    if all(Path(p).exists() for p, _ in FONT_CANDIDATES):
        for path, style in FONT_CANDIDATES:
            pdf.add_font("NavFont", style, path)
        return "NavFont"
    return "Helvetica"


# ------------------------- VATSIM -------------------------

def _normalize_freq(raw: str) -> str:
    """VATSIM returns frequencies as strings like '118.300' already; just clean."""
    if not raw:
        return ""
    return raw.strip()


def fetch_vatsim(icaos: list[str], timeout: float = 6.0) -> VatsimSnapshot | None:
    """Single GET against the VATSIM data feed; pick out GND/TWR/ATIS/DEL/APP for each ICAO."""
    req = urllib.request.Request(VATSIM_URL, headers={"User-Agent": VATSIM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        print(f"[vatsim] fetch failed: {e}", file=sys.stderr)
        return None

    update_time = payload.get("general", {}).get("update_timestamp", "")
    controllers = payload.get("controllers", []) or []
    atis_stations = payload.get("atis", []) or []

    role_for_suffix = {
        "GND": "ground",
        "TWR": "tower",
        "ATIS": "atis",
        "DEL": "delivery",
        "APP": "approach",
        "CTR": "radar",
    }

    out: dict[str, dict[str, str]] = {icao.upper(): {} for icao in icaos if icao}
    atis_out: dict[str, list[str]] = {icao.upper(): [] for icao in icaos if icao}
    for entry in controllers + atis_stations:
        callsign = (entry.get("callsign") or "").upper()
        if "_" not in callsign:
            continue
        icao, _, rest = callsign.partition("_")
        if icao not in out:
            continue
        # callsigns can be EDDG_TWR, EDDG_N_TWR (split sector), EDDG_1_GND, EDDG_ATIS
        suffix = rest.rsplit("_", 1)[-1]
        role = role_for_suffix.get(suffix)
        if not role:
            continue
        freq = _normalize_freq(entry.get("frequency", ""))
        if not freq:
            continue
        out[icao].setdefault(role, freq)
        if role == "atis":
            raw_atis = entry.get("text_atis") or []
            if isinstance(raw_atis, list):
                atis_out[icao] = [str(line).strip() for line in raw_atis if line]
            elif isinstance(raw_atis, str):
                atis_out[icao] = [raw_atis.strip()]

    return VatsimSnapshot(
        fetched_at=datetime.now(timezone.utc).strftime("%H:%MZ"),
        update_time=update_time,
        frequencies=out,
        atis_text=atis_out,
    )


# ------------------------- FIR / en-route radar helpers -------------------------

_FIR_NAMES: dict[str, str] = {
    "EDGG": "Langen Radar",
    "EDWW": "Bremen Radar",
    "EDMM": "München Radar",
    "EDYY": "Berlin Radar",
}


def _german_firs_for_route(waypoints: list[Waypoint]) -> list[str]:
    """Return VATSIM FIR ICAO prefix(es) for waypoints in German airspace.

    Uses a rough latitude split: ≥53.5° → Bremen, ≤48° → Munich, else Langen.
    """
    german_wps = [wp for wp in waypoints if (wp.ident or "").upper().startswith("ED")]
    if not german_wps:
        return []
    firs: set[str] = set()
    for wp in german_wps:
        if wp.lat >= 53.5:
            firs.add("EDWW")
        elif wp.lat <= 48.0:
            firs.add("EDMM")
        else:
            firs.add("EDGG")
    return sorted(firs)


def _find_radar_online(
    vatsim: "VatsimSnapshot | None",
    fir_icaos: list[str],
) -> tuple[str, str] | None:
    """Return (station_name, frequency) for the first online CTR station, else None."""
    if not vatsim or not fir_icaos:
        return None
    for fir in fir_icaos:
        freq = vatsim.frequencies.get(fir.upper(), {}).get("radar", "")
        if freq:
            return _FIR_NAMES.get(fir.upper(), f"{fir} Radar"), freq
    return None


# ------------------------- PDF -------------------------

class NavlogPDF(FPDF):
    def __init__(self):
        super().__init__(orientation="L", unit="mm", format="A4")
        self.set_auto_page_break(False)
        self.set_margins(8, 8, 8)
        self.add_page()


def hms(minutes: float) -> str:
    if minutes <= 0:
        return ""
    total_sec = int(round(minutes * 60))
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_int(x: float, width: int = 0) -> str:
    return f"{int(round(x)):>{width}d}"


def _append_dfs_charts(pdf: FPDF, icao: str) -> int:
    """
    Fetch VFR charts from the DFS AIP and append them as pages to *pdf*.
    Returns the number of pages added. Gracefully skips on any failure.
    """
    try:
        from dfs_charts import extract_png, find_chapter_url, list_charts
    except ImportError:
        print("[dfs] dfs_charts.py not found — skipping chart pages", file=sys.stderr)
        return 0

    from PIL import Image as PILImage

    print(f"[dfs] Fetching VFR charts for {icao}…")
    pngs: list[tuple[str, bytes]] = []

    try:
        vfr_url = find_chapter_url(icao, "vfr")
        if vfr_url:
            for title, page_url in list_charts(vfr_url, skip_junk=True):
                png = extract_png(page_url)
                if png:
                    pngs.append((title, png))
                    print(f"[dfs]   {title}")

        ifr_url = find_chapter_url(icao, "ifr")
        if ifr_url:
            for title, page_url in list_charts(ifr_url, vfr_filter=True):
                png = extract_png(page_url)
                if png:
                    pngs.append((title, png))
                    print(f"[dfs]   {title}")
    except Exception as exc:
        print(f"[dfs] Chart fetch failed: {exc}", file=sys.stderr)
        return 0

    for _title, png_bytes in pngs:
        img = PILImage.open(BytesIO(png_bytes))
        w_px, h_px = img.size
        pdf.add_page(orientation="L" if w_px > h_px else "P")
        pdf.image(BytesIO(png_bytes), x=0, y=0, w=pdf.w, h=pdf.h)

    if pngs:
        print(f"[dfs] Added {len(pngs)} chart page(s)")
    else:
        print(f"[dfs] No VFR charts found for {icao}")
    return len(pngs)


def render(plan: Plan, aircraft: dict, legs: list[Leg], wind: tuple[float, float], magvar: float, out: Path, vatsim: VatsimSnapshot | None = None, call_tower_nm: float = 10.0, dest_info: AirportInfo | None = None, source_note: str = "", fir_icaos: list[str] | None = None, weather: "WeatherBriefing | None" = None, dfs_charts: bool = False, field_wx: "dict[str, FieldWx] | None" = None) -> None:
    pdf = NavlogPDF()
    font = install_fonts(pdf)
    pw = pdf.w - pdf.l_margin - pdf.r_margin

    perf = aircraft.get("performance", {})
    departure = plan.waypoints[0]
    destination = plan.waypoints[-1]
    date_str = datetime.now().strftime("%Y-%m-%d")

    # Tower-call marker: which waypoint row to flag, plus optional VATSIM freq.
    call_leg_idx = find_call_marker(legs, call_tower_nm)
    dest_freqs_for_call = (
        vatsim.frequencies.get(destination.ident.upper(), {}) if vatsim else {}
    )
    call_freq = dest_freqs_for_call.get("tower") or dest_freqs_for_call.get("approach")
    call_freq_label = "TWR" if dest_freqs_for_call.get("tower") else ("APP" if dest_freqs_for_call.get("approach") else "TWR")
    if call_freq:
        call_text = f"→ {call_freq_label} {call_freq}"
    else:
        call_text = f"→ {destination.ident} TWR rufen"

    # ---------- header strip ----------
    pdf.set_font(font, "B", 11)
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.cell(60, 7, "Flugdurchführungsplan VFR", border=0)

    y = pdf.t_margin
    x = pdf.l_margin + 62
    fields = [
        ("Datum", date_str, 28),
        ("von", f"{departure.ident}  {departure.name}", 70),
        ("nach", f"{destination.ident}  {destination.name}", 70),
        ("LFZ-Muster", aircraft.get("type", ""), 26),
        ("LFZ-Kennz.", aircraft.get("registration", ""), 26),
    ]
    for label, value, w in fields:
        pdf.set_xy(x, y)
        pdf.set_font(font, "", 7)
        pdf.cell(w, 3, label, border="LTR")
        pdf.set_xy(x, y + 3)
        pdf.set_font(font, "B", 9)
        pdf.cell(w, 4, " " + value, border="LBR")
        x += w

    # ---------- info row (freigaben / frequencies / times) ----------
    y = pdf.t_margin + 8
    block_h = 25

    # Left: Freigaben / Wetter / Info (free text)
    fg_w = 110
    pdf.set_xy(pdf.l_margin, y)
    pdf.set_font(font, "B", 8)
    pdf.cell(fg_w, 4, " Freigaben / Wetter / Info", border="LTR")
    pdf.rect(pdf.l_margin, y + 4, fg_w, block_h - 4)

    # Middle: Frequencies (departure / destination)
    fr_x = pdf.l_margin + fg_w + 2
    fr_w = (pw - fg_w - 2) * 0.55
    pdf.set_xy(fr_x, y)
    pdf.set_font(font, "B", 8)
    if vatsim and not vatsim.empty():
        freq_title = f" Frequenzen   (VATSIM live, {vatsim.fetched_at})"
    elif vatsim:
        freq_title = " Frequenzen   (VATSIM: keine Stationen online)"
    else:
        freq_title = " Frequenzen"
    pdf.cell(fr_w, 4, freq_title, border="LTR")

    dep_freqs = vatsim.frequencies.get(departure.ident.upper(), {}) if vatsim else {}
    dest_freqs = vatsim.frequencies.get(destination.ident.upper(), {}) if vatsim else {}

    fr_rows = [
        ("Ground", "ground", "delivery"),
        ("Tower", "tower", "approach"),
        ("Info / ATIS", "atis", None),
    ]
    col1_w = 26
    col_rest = (fr_w - col1_w) / 2
    pdf.set_xy(fr_x, y + 4)
    pdf.set_font(font, "", 7)
    pdf.cell(col1_w, 4, "", border="LR")
    pdf.cell(col_rest, 4, f" {departure.ident} (Abflug)", border="R")
    pdf.cell(col_rest, 4, f" {destination.ident} (Ziel)", border="R")
    sub_rh = (block_h - 8) / 5  # Ground + Tower + ATIS + Radar/FIS + UNICOM
    for i, (label, primary, fallback) in enumerate(fr_rows):
        ry = y + 8 + i * sub_rh
        pdf.set_xy(fr_x, ry)
        pdf.set_font(font, "", 8)

        def pick(freqs: dict[str, str]) -> str:
            v = freqs.get(primary, "")
            if not v and fallback:
                v = freqs.get(fallback, "")
                if v:
                    v += f" ({fallback[:3].upper()})"
            return v

        dep_v = pick(dep_freqs)
        dest_v = pick(dest_freqs)
        pdf.cell(col1_w, sub_rh, " " + label, border=1)
        pdf.cell(col_rest, sub_rh, " " + dep_v if dep_v else "", border=1)
        pdf.cell(col_rest, sub_rh, " " + dest_v if dest_v else "", border=1)

    # Radar / FIS row — spans dep+dest columns, highlighted in pale blue when online.
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_y = y + 8 + 3 * sub_rh
    pdf.set_xy(fr_x, radar_y)
    pdf.set_font(font, "B" if radar_info else "", 8)
    if radar_info:
        radar_name, radar_freq = radar_info
        pdf.set_fill_color(225, 240, 255)
        pdf.cell(col1_w, sub_rh, f" {radar_name}", border=1, fill=True)
        pdf.cell(col_rest * 2, sub_rh, f" {radar_freq}", border=1, fill=True)
        pdf.set_fill_color(255, 255, 255)
    else:
        pdf.set_font(font, "", 8)
        pdf.cell(col1_w, sub_rh, " Radar / FIS", border=1)
        pdf.cell(col_rest * 2, sub_rh, "", border=1)

    # UNICOM row: merged across the full Frequenzen width, bold and centered.
    unicom_y = y + 8 + 4 * sub_rh
    pdf.set_xy(fr_x, unicom_y)
    pdf.set_fill_color(235, 235, 235)
    pdf.set_font(font, "B", 9)
    pdf.cell(fr_w, sub_rh,
             f"UNICOM (kein ATC):  {UNICOM_FREQ} MHz",
             border=1, align="C", fill=True)
    pdf.set_fill_color(255, 255, 255)

    # Right: Times block
    t_x = fr_x + fr_w + 2
    t_w = pw - (t_x - pdf.l_margin)
    pdf.set_xy(t_x, y)
    pdf.set_font(font, "B", 8)
    pdf.cell(t_w, 4, " Zeiten (UTC)", border="LTR")
    time_rows = [("ETD", "ATD"), ("ETA", "ATA"), ("SS", "")]
    rh = (block_h - 4) / 3
    half = t_w / 2
    for i, (a, b) in enumerate(time_rows):
        ry = y + 4 + i * rh
        pdf.set_xy(t_x, ry)
        pdf.set_font(font, "", 8)
        pdf.cell(t_w * 0.18, rh, f" {a}", border="LB" if i == 2 else "L")
        pdf.cell(half - t_w * 0.18, rh, "", border="B" if i == 2 else "")
        pdf.cell(t_w * 0.18, rh, f" {b}" if b else "", border="LB" if i == 2 else "L")
        pdf.cell(half - t_w * 0.18, rh, "", border="RB" if i == 2 else "R")

    # ---------- ATIS / Platzwetter strip: 3 rows (Abflug / Ziel / Ausweich) ----------
    y2 = y + block_h + 2
    fixed_w = 16 + 12 + 12 + 14 + 16 + 22 + 16 + 30 + 16 + 14
    atis_cols = [
        ("Platz", 16), ("Code", 12), ("RWY", 12), ("TL/FL", 14), ("Zeit UTC", 16),
        ("Wind °/kt", 22), ("Sicht", 16), ("Wolken", 30),
        ("T/Td °C", 16), ("QNH", 14),
        ("Tendenz / Bemerkungen", pw - fixed_w),
    ]
    atis_header_h = 4
    atis_row_h = 5
    pdf.set_xy(pdf.l_margin, y2)
    pdf.set_font(font, "B", 7)
    for label, w in atis_cols:
        pdf.cell(w, atis_header_h, " " + label, border="LTR")
    atis_rows = [departure.ident or "Abflug", destination.ident or "Ziel", "Ausweich"]
    # Vorbefüllung Abflug/Ziel aus VATSIM-ATIS bzw. echtem METAR; Ausweich bleibt leer.
    wx_by_row = [
        (field_wx or {}).get(departure.ident.upper()) if departure.ident else None,
        (field_wx or {}).get(destination.ident.upper()) if destination.ident else None,
        None,
    ]
    fetched_at = weather.fetched_at if weather else ""

    def _atis_cell_values(label: str, wx: "FieldWx | None") -> list[str]:
        # Reihenfolge: Platz, Code, RWY, TL/FL, Zeit UTC, Wind, Sicht, Wolken, T/Td, QNH, Bemerkung
        if wx is None:
            return [" " + label] + [""] * (len(atis_cols) - 1)
        pm = wx.parsed
        zeit = wx.time_z or (fetched_at if wx.source.startswith("METAR") else "")
        return [
            " " + label,
            wx.atis_code or "",
            wx.rwy or "",
            "",                                  # TL/FL – nicht aus dem Wetter
            zeit,
            _wx_wind_cell(pm),
            "",                                  # Sicht – nur Wind/Temp/Druck
            "",                                  # Wolken – dito
            _wx_ttd_cell(pm),
            str(pm.qnh_hpa) if pm.qnh_hpa is not None else "",
            wx.source,
        ]

    pdf.set_font(font, "", 7)
    for ri, label in enumerate(atis_rows):
        ry = y2 + atis_header_h + ri * atis_row_h
        pdf.set_xy(pdf.l_margin, ry)
        values = _atis_cell_values(label, wx_by_row[ri])
        for ci, (_, w) in enumerate(atis_cols):
            cell = values[ci]
            text = (" " + cell) if cell else ""
            pdf.cell(w, atis_row_h, text, border=1)
    atis_h = atis_header_h + len(atis_rows) * atis_row_h

    # ---------- nav table ----------
    nav_y = y2 + atis_h + 3
    columns = [
        ("Waypoint",        56, "L"),
        ("VOR\nInfo",       24, "C"),
        ("Alt\nft",         12, "C"),
        ("TAS\nkt",         12, "C"),
        ("Wind\n°/kt",      16, "C"),
        ("TC\n°",           12, "C"),
        ("WCA\n°",          12, "C"),
        ("Var\n°",          10, "C"),
        ("MH\n°",           12, "C"),
        ("Dist\nNM",        14, "C"),
        ("Total\nNM",       14, "C"),
        ("GS\nkt",          12, "C"),
        ("ETE\nmin",        14, "C"),
        ("Total\nmin",      14, "C"),
        ("Fuel\nL",         12, "C"),
        ("ETO / ATO",       25, "L"),
    ]
    total_w = sum(w for _, w, _ in columns)
    if total_w > pw:
        scale = pw / total_w
        columns = [(name, w * scale, a) for name, w, a in columns]

    # Columns the pilot actively scans in cruise; printed larger + bold.
    highlight = {"TC\n°", "MH\n°", "Dist\nNM", "ETE\nmin", "GS\nkt"}

    header_h = 8
    row_h = 6.5
    _note_text = (
        source_note
        or "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. "
           "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
    )
    # Reserve space at the bottom of each table page for the footer line.
    usable_bottom = pdf.h - pdf.b_margin - 7

    def _draw_nav_header(at_y: float) -> None:
        cx = pdf.l_margin
        for col_name, col_w, _ in columns:
            pdf.set_xy(cx, at_y)
            if col_name in highlight:
                pdf.set_font(font, "B", 9)
            else:
                pdf.set_font(font, "B", 7)
            pdf.multi_cell(col_w, header_h / 2, col_name, border=1, align="C")
            cx += col_w

    _draw_nav_header(nav_y)
    current_y = nav_y + header_h

    n_rows = max(len(plan.waypoints), 10)
    cum_dist = 0.0
    cum_ete = 0.0
    cum_fuel = 0.0
    for i in range(n_rows):
        if current_y + row_h > usable_bottom:
            if i >= len(plan.waypoints):
                break  # trailing empty rows: don't overflow to a new page
            # Draw footer on the current page, then continue on a fresh one.
            pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
            pdf.set_font(font, "I", 6)
            pdf.cell(0, 3, _note_text, align="C")
            pdf.add_page()
            current_y = pdf.t_margin
            _draw_nav_header(current_y)
            current_y += header_h

        ry = current_y
        pdf.set_fill_color(245, 245, 245) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)

        if i == 0:
            wp = plan.waypoints[0]
            row = [
                f"{wp.ident}  {wp.name}",
                wp.vor_info or wp.freq or "",
                fmt_int(wp.alt_ft or 0) if wp.alt_ft else "",
                "", "", "", "", "", "",
                "", "", "", "", "", "", "",
            ]
        elif i < len(plan.waypoints):
            leg = legs[i - 1]
            cum_dist += leg.distance_nm
            cum_ete += leg.ete_min
            cum_fuel += leg.fuel_l
            wp = plan.waypoints[i]
            first_leg = (i == 1)
            row = [
                f"{wp.ident}  {wp.name}",
                wp.vor_info or wp.freq or "",
                fmt_int(_effective_leg_alt(plan, i)) if i < len(plan.waypoints) - 1 else fmt_int(wp.alt_ft or 0),
                fmt_int(perf.get("tas_cruise", 0)) if first_leg else "",
                f"{int(wind[0]):03d}/{int(wind[1]):02d}" if first_leg else "",
                fmt_int(leg.tc),
                f"{leg.wca:+.0f}",
                f"{magvar:+.1f}" if first_leg else "",
                fmt_int(leg.mh),
                f"{leg.distance_nm:.1f}",
                f"{cum_dist:.1f}",
                fmt_int(leg.gs_kt),
                f"{leg.ete_min:.0f}",
                f"{cum_ete:.0f}",
                f"{leg.fuel_l:.1f}",
                "",
            ]
        else:
            row = [""] * len(columns)

        # If this row is the tower-call marker, inject the call annotation into
        # the last (ETO / ATO) cell.
        is_call_row = (call_leg_idx is not None and i == call_leg_idx and i < len(plan.waypoints) - 1)

        cx = pdf.l_margin
        for col_idx, ((name, w, align), val) in enumerate(zip(columns, row)):
            pdf.set_xy(cx, ry)
            is_last = (col_idx == len(columns) - 1)
            if is_call_row and is_last:
                pdf.set_font(font, "B", 9)
                pdf.set_text_color(180, 0, 0)
                pdf.cell(w, row_h, " " + call_text, border=1, align="L", fill=True)
                pdf.set_text_color(0, 0, 0)
            elif name in highlight:
                pdf.set_font(font, "B", 10)
                pdf.cell(w, row_h, " " + str(val) if val else "", border=1, align=align, fill=True)
            else:
                pdf.set_font(font, "", 8)
                pdf.cell(w, row_h, " " + str(val) if val else "", border=1, align=align, fill=True)
            cx += w

        current_y += row_h

    # ---------- fuel summary ----------
    fuel = aircraft.get("fuel", {})
    burn = perf.get("fuel_burn_cruise_lph", 60)
    burn_climb = perf.get("fuel_burn_climb_lph", 80)
    burn_taxi = perf.get("fuel_burn_taxi_lph", 15)

    taxi_l = burn_taxi * fuel.get("taxi_minutes", 12) / 60
    climb_l = burn_climb * 5 / 60
    approach_l = burn * fuel.get("approach_minutes", 10) / 60
    reserve_l = burn * fuel.get("reserve_minutes", 30) / 60
    alternate_l = burn * fuel.get("alternate_minutes", 0) / 60
    trip_l = cum_fuel
    min_required = taxi_l + climb_l + trip_l + approach_l + alternate_l + reserve_l

    # If the summary block won't fit on the current page, start a fresh one.
    SUMMARY_H = 65  # conservative: fuel header + 10 lines + signature rect
    if current_y + 4 + SUMMARY_H > usable_bottom:
        pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
        pdf.set_font(font, "I", 6)
        pdf.cell(0, 3, _note_text, align="C")
        pdf.add_page()
        fuel_y = pdf.t_margin + 2
    else:
        fuel_y = current_y + 4
    fuel_x = pdf.l_margin
    fuel_w = 95
    pdf.set_xy(fuel_x, fuel_y)
    pdf.set_font(font, "B", 8)
    pdf.cell(fuel_w, 5, " Kraftstoffberechnung", border=1)
    pdf.ln(5)
    pdf.set_font(font, "", 8)
    fuel_lines = [
        ("Anlassen / Rollen", taxi_l),
        ("Steigflug", climb_l),
        ("Reiseflug (Trip)", trip_l),
        ("An- / Abflug", approach_l),
        ("Ausweichflugplatz", alternate_l),
        ("Reserve (30 min)", reserve_l),
        ("Mindest-Kraftstoffbedarf", min_required),
        ("Kraftstoff-Vorrat", fuel.get("capacity_usable_l", 0)),
        ("Extra-Kraftstoff", fuel.get("capacity_usable_l", 0) - min_required),
    ]
    for label, val in fuel_lines:
        pdf.set_xy(fuel_x, pdf.get_y())
        pdf.cell(fuel_w * 0.65, 5, " " + label, border=1)
        pdf.cell(fuel_w * 0.35, 5, f" {val:7.1f} L", border=1, align="R")
        pdf.ln(5)

    capacity = fuel.get("capacity_usable_l", 0)
    max_flight_min = (capacity / burn) * 60 if burn > 0 else 0
    safe_min = max(0, max_flight_min - 30)
    pdf.set_xy(fuel_x, pdf.get_y() + 1)
    pdf.set_font(font, "B", 8)
    pdf.cell(fuel_w * 0.65, 5, " Sichere Flugzeit (max − 30 min)", border=1)
    pdf.cell(fuel_w * 0.35, 5, f" {int(safe_min // 60)}:{int(safe_min % 60):02d}", border=1, align="R")

    # ---------- planning assumptions ----------
    foot_x = fuel_x + fuel_w + 4
    foot_y = fuel_y
    pdf.set_xy(foot_x, foot_y)
    pdf.set_font(font, "B", 8)
    pdf.cell(80, 5, " Planungsannahmen", border=1)
    pdf.ln(5)
    if call_leg_idx is not None and call_leg_idx < len(plan.waypoints) - 1:
        call_wp = plan.waypoints[call_leg_idx]
        remaining_from_call = sum(l.distance_nm for l in legs[call_leg_idx:])
        freq_part = f" {call_freq}" if call_freq else ""
        call_summary = (
            f"Ruf {destination.ident} {call_freq_label}{freq_part} ab {call_wp.ident} "
            f"({remaining_from_call:.0f} NM)"
        )
    else:
        call_summary = f"Ruf {destination.ident} TWR vor CTR/Meldepunkt"

    items = [
        f"Wind: {int(wind[0]):03d}°/{int(wind[1])} kt (uniform alle Schenkel)",
        f"Magnetische Variation: {magvar:+.1f}°",
        f"AIRAC-Zyklus: {plan.cycle or 'n/a'}",
        f"TAS Reise: {perf.get('tas_cruise', '-')} kt",
        f"Kraftstoffverbrauch: {burn:.1f} L/h",
        f"Gesamtstrecke: {cum_dist:.1f} NM",
        f"Gesamtzeit (Reise): {int(cum_ete)} min",
        f"Gesamtkraftstoff (Trip): {trip_l:.1f} L",
        call_summary,
    ]
    pdf.set_font(font, "", 8)
    for line in items:
        pdf.set_xy(foot_x, pdf.get_y())
        pdf.cell(80, 5, " " + line, border=1)
        pdf.ln(5)

    # signature / remarks block
    sig_x = foot_x + 84
    pdf.set_xy(sig_x, foot_y)
    pdf.set_font(font, "B", 8)
    sig_w = pdf.l_margin + pw - sig_x
    pdf.cell(sig_w, 5, " Bemerkungen / Unterschrift Pilot", border=1)
    pdf.rect(sig_x, foot_y + 5, sig_w, 45)

    # bottom note on the final navlog page
    pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3, _note_text, align="C")

    render_phraseology(pdf, font, plan, aircraft, vatsim, fir_icaos or [])

    if dest_info is not None:
        render_destination_page(pdf, font, dest_info, vatsim)

    if weather is not None:
        render_weather_page(pdf, font, weather, fir_icaos=fir_icaos, vatsim=vatsim)

    if dfs_charts:
        _append_dfs_charts(pdf, destination.ident.upper())

    pdf.output(str(out))


# ------------------------- phraseology page -------------------------

def render_phraseology(pdf: FPDF, font: str, plan: Plan, aircraft: dict, vatsim: VatsimSnapshot | None, fir_icaos: list[str] | None = None) -> None:
    """Two-page phraseology: (1) FIS / Radar en route, (2) CTR entry via Whiskey."""
    dep = plan.waypoints[0]
    dest = plan.waypoints[-1]
    reg = aircraft.get("registration", "D-XXXX")
    ac_type = aircraft.get("type", "C172")

    def clean_name(s: str) -> str:
        return s.replace("- ", "-").strip(" -")

    dest_name = clean_name(dest.name or dest.ident).split()[0] or dest.ident

    tower_freq = ""
    if vatsim:
        tower_freq = vatsim.frequencies.get(dest.ident.upper(), {}).get("tower", "")
    tower_on = f" auf {tower_freq}" if tower_freq else ""

    # ── layout constants (computed once, used in all helpers via closure) ─────
    pw     = pdf.w - pdf.l_margin - pdf.r_margin
    ROLE_W = 14.0
    DE_W   = (pw - ROLE_W) / 2
    EN_W   = pw - ROLE_W - DE_W
    LH     = 4.0          # line height for dialogue rows

    SIT_W  = 52.0
    DE_V   = (pw - SIT_W) / 2
    EN_V   = pw - SIT_W - DE_V

    # colour palette
    C_PILOT = (235, 245, 255)   # pilot rows: pale blue
    C_ATC   = (255, 248, 230)   # ATC rows:   pale amber
    C_NOTE  = (252, 252, 220)   # info notes: pale yellow
    C_SEC   = (225, 225, 225)   # section headers
    C_HDR   = (210, 220, 235)   # variation table header
    C_COL   = (242, 242, 242)   # column label row

    # ── helpers ───────────────────────────────────────────────────────────────

    def note_box(text: str) -> None:
        pdf.set_font(font, "I", 7.5)
        pdf.set_fill_color(*C_NOTE)
        pdf.multi_cell(pw, 3.8, text, border=1, fill=True)
        pdf.ln(3)

    def section_bar(title: str, with_cols: bool = True) -> None:
        pdf.set_font(font, "B", 9)
        pdf.set_fill_color(*C_SEC)
        pdf.cell(pw, 5, "  " + title, border=1, fill=True)
        pdf.ln(5)
        if with_cols:
            pdf.set_fill_color(*C_COL)
            pdf.set_font(font, "B", 7.5)
            pdf.cell(ROLE_W, 4.5, "",           border=1, fill=True)
            pdf.cell(DE_W,   4.5, "  Deutsch",  border=1, fill=True)
            pdf.cell(EN_W,   4.5, "  English",  border=1, fill=True)
            pdf.ln(4.5)

    def drow(role: str, de: str, en: str, atc: bool = False) -> None:
        """One dialogue row: role chip | German text | English text."""
        bg = C_ATC if atc else C_PILOT
        y0 = pdf.get_y()
        pdf.set_fill_color(*bg)

        pdf.set_xy(pdf.l_margin, y0)
        pdf.set_font(font, "B", 7.5)
        pdf.multi_cell(ROLE_W, LH, role, border="LBR", align="C", fill=True)
        h0 = pdf.get_y() - y0

        pdf.set_xy(pdf.l_margin + ROLE_W, y0)
        pdf.set_font(font, "", 8.5)
        pdf.multi_cell(DE_W, LH, de, border="BR", fill=True)
        h1 = pdf.get_y() - y0

        pdf.set_xy(pdf.l_margin + ROLE_W + DE_W, y0)
        pdf.set_font(font, "I", 8.5)
        pdf.multi_cell(EN_W, LH, en, border="BR", fill=True)
        h2 = pdf.get_y() - y0

        pdf.set_y(y0 + max(h0, h1, h2))

    def vbar(title: str) -> None:
        pdf.ln(2)
        pdf.set_font(font, "B", 8.5)
        pdf.set_fill_color(*C_HDR)
        pdf.cell(pw, 5, "  " + title, border=1, fill=True)
        pdf.ln(5)
        pdf.set_fill_color(*C_COL)
        pdf.set_font(font, "B", 7.5)
        pdf.cell(SIT_W, 4, "  Situation", border=1, fill=True)
        pdf.cell(DE_V,  4, "  Deutsch",   border=1, fill=True)
        pdf.cell(EN_V,  4, "  English",   border=1, fill=True)
        pdf.ln(4)

    def vrow(sit: str, de: str, en: str) -> None:
        y0 = pdf.get_y()
        pdf.set_fill_color(255, 255, 255)
        pdf.set_xy(pdf.l_margin, y0)
        pdf.set_font(font, "B", 7.5)
        pdf.multi_cell(SIT_W, LH, sit, border="LBR")
        h0 = pdf.get_y() - y0
        pdf.set_xy(pdf.l_margin + SIT_W, y0)
        pdf.set_font(font, "", 8)
        pdf.multi_cell(DE_V, LH, de, border="BR")
        h1 = pdf.get_y() - y0
        pdf.set_xy(pdf.l_margin + SIT_W + DE_V, y0)
        pdf.set_font(font, "I", 8)
        pdf.multi_cell(EN_V, LH, en, border="BR")
        h2 = pdf.get_y() - y0
        pdf.set_y(y0 + max(h0, h1, h2))

    def page_footer(text: str) -> None:
        pdf.set_y(pdf.h - pdf.b_margin - 4)
        pdf.set_font(font, "I", 6)
        pdf.cell(0, 3, text, align="C")

    # ── PAGE 1: FIS / en-route Radar ─────────────────────────────────────────
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_name = radar_info[0] if radar_info else None
    radar_freq = radar_info[1] if radar_info else None

    pdf.add_page()

    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 13)
    if radar_name:
        page1_title = f"Sprechgruppen VFR  ·  1: {radar_name}  ({radar_freq})"
    else:
        page1_title = "Sprechgruppen VFR  ·  1: FIS Bremen Information"
    pdf.cell(pw, 7, page1_title, align="C")
    pdf.ln(8)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"Templated für {reg} ({ac_type}), {dep.ident} → {dest.ident}.  "
             "[Eckige Klammern] vor jedem Spruch anpassen.",
             align="C")
    pdf.ln(6)

    if radar_name:
        note_box(
            f"{radar_name} ONLINE — {radar_freq} MHz (VATSIM live).  "
            "Radardienst: aktive Staffelung möglich. "
            "Erstanruf: nur Rufzeichen — nach Rückfrage Vollmeldung mit Position, Höhe, POB. "
            "Squawk wie angewiesen (kein automatisches 7000)."
        )
    else:
        note_box(
            "Bremen Information (Langen Center) = Fluginformationsdienst, keine Staffelung. "
            "Erstanruf: nur Rufzeichen — erst nach Rückfrage die Vollmeldung abgeben. "
            "POB immer nennen. Squawk VFR = 7000. Frequenz: AIP / Streckenkarte prüfen."
        )

    section_bar("A · Erstkontakt & Vollmeldung  ·  Initial contact & full position report")

    drow("PILOT",
         f"Bremen Information, {reg}.",
         f"Bremen Information, {reg}.")

    drow("FIS",
         f"{reg}, Bremen Information, bitte melden.",
         f"{reg}, Bremen Information, go ahead.",
         atc=True)

    drow("PILOT",
         f"{reg}, {ac_type}, VFR von {dep.ident} nach {dest.ident}, "
         f"[Position, z. B. 10 km nördlich Osnabrück], [2500 Fuß], "
         "[2] Personen an Bord, erbitte Verkehrsinformationen.",
         f"{reg}, {ac_type}, VFR from {dep.ident} to {dest.ident}, "
         f"[position, e.g. 10 km north of Osnabrück], [2500 feet], "
         "[2] persons on board, request traffic information.")

    drow("FIS",
         f"{reg}, identifiziert, [2500 Fuß], QNH [1018], Squawk [7631], "
         "Verkehrsinformationen soweit möglich.",
         f"{reg}, identified, [2500 feet], QNH [1018], squawk [7631], "
         "traffic information workload permitting.",
         atc=True)

    drow("PILOT",
         f"QNH [1018], Squawk [7631], {reg}.",
         f"QNH [1018], squawk [7631], {reg}.")

    section_bar("B · Verkehrsinformation & Frequenzverlassen  ·  Traffic info & leaving FIS",
                with_cols=False)

    drow("FIS",
         f"{reg}, Verkehr, [Cessna 172, 12 Uhr, 4 Meilen], entgegenkommend, [2500 Fuß], "
         "melden Sie Verkehr in Sicht.",
         f"{reg}, traffic, [Cessna 172, 12 o'clock, 4 miles], opposite direction, [2500 feet], "
         "report traffic in sight.",
         atc=True)

    drow("PILOT",
         f"Verkehr in Sicht / nicht in Sicht, {reg}.",
         f"Traffic in sight / not in sight, {reg}.")

    drow("PILOT",
         f"{reg}, erbitte Verlassen der Frequenz.",
         f"{reg}, request frequency change.")

    drow("FIS",
         f"{reg}, Frequenzwechsel genehmigt, Squawk VFR, auf Wiederhören.",
         f"{reg}, frequency change approved, squawk 7000, goodbye.",
         atc=True)

    drow("PILOT",
         f"Squawk VFR, {reg}, auf Wiederhören.",
         f"Squawk 7000, {reg}, goodbye.")

    vbar("Mögliche FIS-Antworten  ·  Possible FIS responses")

    vrow("Hohe Arbeitsbelastung\nWorkload denial",
         f"{reg}, aufgrund hoher Arbeitsbelastung kein Fluginformationsdienst möglich. "
         f"Squawk 7000, auf Wiederhören.\n→  Verstanden, Squawk 7000, {reg}.",
         f"{reg}, unable to provide FIS due to high workload. "
         f"Squawk 7000, goodbye.\n→  Roger, squawk 7000, {reg}.")

    vrow("Kein Radarkontakt\nNo radar contact",
         f"{reg}, kein Radarkontakt. Bitte Position genauer angeben.\n"
         f"→  {reg}, [5 km westlich Mast Steinkimmen], Kurs [120 Grad].",
         f"{reg}, no radar contact. Say position more precisely.\n"
         f"→  {reg}, [5 km west of Steinkimmen mast], heading [120].")

    vrow("POB-Nachfrage\nPOB query",
         f"{reg}, wie viele Personen an Bord?\n→  [2] Personen an Bord, {reg}.",
         f"{reg}, persons on board?\n→  [2] persons on board, {reg}.")

    page_footer(
        "FIS = Fluginformationsdienst — keine Staffelung, keine Separierung. "
        "Frequenzwechsel erst nach Genehmigung. Squawk 7000 beim Verlassen. "
        "Quelle: DFS Sprechfunkverfahren / VATSIM Germany KB."
    )

    # ── PAGE 2: CTR entry via Whiskey ─────────────────────────────────────────
    pdf.add_page()

    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 13)
    pdf.cell(pw, 7,
             f"Sprechgruppen VFR  ·  2: CTR-Einflug {dest.ident} über Meldepunkt Whiskey",
             align="C")
    pdf.ln(8)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"{reg} ({ac_type}), {dep.ident} → {dest.ident}, "
             f"Einflug über Whiskey, Piste [25]{tower_on}.  [Eckige Klammern] anpassen.",
             align="C")
    pdf.ln(6)

    note_box(
        "Vorher: ATIS abhören, Buchstaben und QNH notieren, max. Einflughöhe beachten (oft 2000 ft). "
        "Erstanruf ca. 10–15 NM vor der CTR-Grenze: nur Rufzeichen — dann warten! "
        "Vollmeldung mit ATIS-Buchstabe erst nach Rückfrage."
    )

    section_bar("C · Erstkontakt Tower & Einflugfreigabe  ·  Initial call & CTR entry clearance")

    drow("PILOT",
         f"{dest_name} Tower, {reg}.",
         f"{dest_name} Tower, {reg}.")

    drow("TWR",
         f"{reg}, {dest_name} Tower, bitte melden.",
         f"{reg}, {dest_name} Tower, go ahead.",
         atc=True)

    drow("PILOT",
         f"{reg}, {ac_type}, VFR von {dep.ident}, [15 km nordwestlich Whiskey], [2000 Fuß], "
         "Information [Alpha] erhalten, erbitte Einflug über Whiskey zur Landung.",
         f"{reg}, {ac_type}, VFR from {dep.ident}, [15 km northwest of Whiskey], [2000 feet], "
         "information [Alpha] received, request entry via Whiskey for landing.")

    drow("TWR",
         f"{reg}, fliegen Sie in die Kontrollzone über Whiskey, "
         "QNH [1018], erwarten Sie Piste [25].",
         f"{reg}, enter the control zone via Whiskey, "
         "QNH [1018], expect runway [25].",
         atc=True)

    drow("PILOT",
         f"Einflug über Whiskey, QNH [1018], Piste [25], {reg}.",
         f"Entering via Whiskey, QNH [1018], runway [25], {reg}.")

    drow("TWR",
         f"{reg}, melden Sie Whiskey.",
         f"{reg}, report Whiskey.",
         atc=True)

    drow("PILOT",
         f"Melde Whiskey, {reg}.",
         f"Wilco, {reg}.")

    section_bar("D · Am Meldepunkt Whiskey bis GA-Vorfeld  ·  At Whiskey through to GA apron",
                with_cols=False)

    drow("PILOT",
         f"{reg}, Whiskey, [2000 Fuß].",
         f"{reg}, Whiskey, [2000 feet].")

    drow("TWR",
         f"{reg}, fliegen Sie in den [rechten] Gegenanflug Piste [25].",
         f"{reg}, join [right] downwind runway [25].",
         atc=True)

    drow("PILOT",
         f"[Rechter] Gegenanflug Piste [25], {reg}.",
         f"[Right] downwind runway [25], {reg}.")

    drow("TWR",
         f"{reg}, Wind [250 Grad, 8 Knoten], Piste [25], Landung frei.",
         f"{reg}, wind [250 degrees, 8 knots], runway [25], cleared to land.",
         atc=True)

    drow("PILOT",
         f"Piste [25], Landung frei, {reg}.",
         f"Runway [25], cleared to land, {reg}.")

    drow("PILOT",
         f"{reg}, Piste [25] verlassen über [Alpha].",
         f"{reg}, runway [25] vacated via [Alpha].")

    drow("TWR",
         f"{reg}, rollen Sie zum GA-Vorfeld über [Alpha, Bravo], Squawk Standby.",
         f"{reg}, taxi to GA apron via [Alpha, Bravo], squawk standby.",
         atc=True)

    drow("PILOT",
         f"GA-Vorfeld über [Alpha, Bravo], Squawk Standby, {reg}.",
         f"GA apron via [Alpha, Bravo], squawk standby, {reg}.")

    drow("PILOT",
         f"{reg}, Parkposition erreicht, auf Wiederhören.",
         f"{reg}, on stand, goodbye.")

    vbar("Mögliche Tower-Antworten  ·  Possible tower responses")

    vrow("Einflug vorübergehend\nnicht möglich",
         f"{reg}, können Sie [5 Minuten] außerhalb der CTR warten?\n"
         f"→  Warte außerhalb CTR, {reg}.",
         f"{reg}, can you hold outside the CTR for [5 minutes]?\n"
         f"→  Holding outside CTR, {reg}.")

    vrow("Squawk-Zuweisung\nSquawk assignment",
         f"Squawk [7023].\n→  Squawk [7023], {reg}.",
         f"Squawk [7023].\n→  Squawk [7023], {reg}.")

    vrow("Sequenzierung hinter\nVerkehr  ·  Sequencing",
         f"{reg}, Verkehr voraus, [Piper auf Endanflug Piste 25], Verkehr in Sicht?\n"
         f"→  Verkehr in Sicht, {reg}.\n"
         f"TWR: Folgen Sie dem Verkehr, Piste [25], Landung frei.",
         f"{reg}, traffic ahead, [Piper on final runway 25], traffic in sight?\n"
         f"→  Traffic in sight, {reg}.\n"
         "TWR: Follow traffic, runway [25], cleared to land.")

    page_footer(
        f"CTR-Einflug nur mit ausdrücklicher Freigabe. ATIS vor Erstkontakt abhören. "
        f"Meldepunkt Whiskey ist {dest.ident}-spezifisch — Bezeichnung vor jedem Flug im Chart prüfen."
    )


# ------------------------- destination briefing page -------------------------

def render_destination_page(pdf: FPDF, font: str, info: AirportInfo, vatsim: VatsimSnapshot | None) -> None:
    pdf.add_page()
    pw = pdf.w - pdf.l_margin - pdf.r_margin

    # Title
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 14)
    pdf.cell(pw, 8, f"Zielflugplatz {info.icao}  ·  Destination airport", align="C")
    pdf.ln(9)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4, info.name + (f"  ·  {info.city}" if info.city else ""), align="C")
    pdf.ln(7)

    # ---------- Airport basics ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 225, 225)
    pdf.cell(pw, 5, "  Flugplatz-Stammdaten  ·  Airport data", border=1, fill=True)
    pdf.ln(5)

    basics = [
        ("ICAO",            info.icao),
        ("IATA",            info.iata or "—"),
        ("Name",            info.name),
        ("Ort / City",      info.city or "—"),
        ("Elevation",       f"{int(info.elevation_ft)} ft"),
        ("TA / TL",         f"{info.transition_alt or '—'} ft / FL {info.transition_level or '—'}"),
    ]
    half = pw / 2
    label_w = 32
    val_w = half - label_w
    pdf.set_font(font, "", 9)
    for i in range(0, len(basics), 2):
        row = basics[i:i + 2]
        for label, value in row:
            pdf.set_font(font, "B", 8)
            pdf.cell(label_w, 5, " " + label, border=1)
            pdf.set_font(font, "", 9)
            pdf.cell(val_w, 5, " " + str(value), border=1)
        if len(row) == 1:
            pdf.cell(half, 5, "", border=0)
        pdf.ln(5)
    pdf.ln(2)

    # ---------- Runways + ILS LOC ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(210, 220, 235)
    pdf.cell(pw, 5, "  Pisten & ILS-Frequenzen  ·  Runways & ILS LOC frequencies", border=1, fill=True)
    pdf.ln(5)

    cols = [
        ("RWY",          22, "C"),
        ("Belag",        24, "L"),
        ("Länge",        22, "R"),
        ("Breite",       18, "R"),
        ("ILS Ident",    24, "C"),
        ("ILS Freq",     24, "C"),
        ("Typ",          pw - 22 - 24 - 22 - 18 - 24 - 24, "L"),
    ]
    pdf.set_font(font, "B", 8)
    pdf.set_fill_color(240, 240, 240)
    for name, w, _ in cols:
        pdf.cell(w, 5, " " + name, border=1, fill=True)
    pdf.ln(5)

    # Build rows: one per runway end, joined with its ILS LOC if any.
    ils_by_rwy = {ils.runway: ils for ils in info.ils_locs}
    pdf.set_font(font, "", 9)
    if info.runways:
        for rwy in info.runways:
            for end in (rwy.ident_a, rwy.ident_b):
                ils = ils_by_rwy.get(end)
                values = [
                    end,
                    rwy.surface,
                    f"{int(round(rwy.length_m))} m",
                    f"{int(round(rwy.width_m))} m",
                    ils.ident if ils else "—",
                    f"{ils.freq_mhz:.3f}" if ils else "—",
                    ils.type_desc if ils else "—",
                ]
                for (_, w, align), val in zip(cols, values):
                    pdf.cell(w, 5, " " + str(val), border=1, align=align)
                pdf.ln(5)
    else:
        pdf.cell(pw, 5, "  Keine Pistendaten gefunden (X-Plane apt.dat fehlt). Pfad mit --xplane setzen.",
                 border=1, align="C")
        pdf.ln(5)
    pdf.ln(2)

    # ---------- Comm frequencies (recap + ATIS text) ----------
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 225, 225)
    suffix = f"  (VATSIM live, {vatsim.fetched_at})" if vatsim else ""
    pdf.cell(pw, 5, "  Frequenzen & ATIS  ·  Communication frequencies" + suffix, border=1, fill=True)
    pdf.ln(5)

    freqs = vatsim.frequencies.get(info.icao, {}) if vatsim else {}
    role_labels = [
        ("Ground",    "ground"),
        ("Tower",     "tower"),
        ("Approach",  "approach"),
        ("Delivery",  "delivery"),
        ("ATIS",      "atis"),
        ("UNICOM",    None),
    ]
    pdf.set_font(font, "", 9)
    label_w2 = 30
    val_w2 = pw / 3 - label_w2
    pdf.set_x(pdf.l_margin)
    col = 0
    row_y = pdf.get_y()
    for label, key in role_labels:
        if key is None:
            value = f"{UNICOM_FREQ} (kein ATC)"
        else:
            value = freqs.get(key, "—") or "—"
        x = pdf.l_margin + col * (label_w2 + val_w2)
        pdf.set_xy(x, row_y)
        pdf.set_font(font, "B", 8)
        pdf.cell(label_w2, 5, " " + label, border=1)
        pdf.set_font(font, "", 9)
        pdf.cell(val_w2, 5, " " + value, border=1)
        col += 1
        if col == 3:
            col = 0
            row_y += 5
    if col != 0:
        # fill remaining slots in current row
        for _ in range(3 - col):
            x = pdf.l_margin + col * (label_w2 + val_w2)
            pdf.set_xy(x, row_y)
            pdf.cell(label_w2 + val_w2, 5, "", border=0)
            col += 1
        row_y += 5
    pdf.set_y(row_y + 1)

    # Live ATIS text, if present
    atis_lines = vatsim.atis_text.get(info.icao) if vatsim else None
    if atis_lines:
        pdf.set_font(font, "B", 8)
        pdf.set_fill_color(245, 245, 220)
        pdf.cell(pw, 4.5, "  ATIS-Text (live)", border=1, fill=True)
        pdf.ln(4.5)
        pdf.set_font(font, "", 8.5)
        atis_block = "\n".join(atis_lines)
        pdf.multi_cell(pw, 4, atis_block, border="LBR")
    else:
        pdf.set_font(font, "I", 8)
        pdf.cell(pw, 4.5, "  Kein ATIS-Text verfügbar (VATSIM ATIS offline oder --vatsim nicht gesetzt).",
                 border=1, align="C")
        pdf.ln(4.5)

    # Footer
    pdf.set_y(pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3,
             "Pisten- und ILS-Daten aus X-Plane apt.dat / earth_nav.dat. Vor dem Flug aktuelle AIP / ATIS prüfen.",
             align="C")


# ------------------------- weather briefing page -------------------------

def render_weather_page(pdf: FPDF, font: str, briefing: WeatherBriefing, fir_icaos: list[str] | None = None, vatsim: "VatsimSnapshot | None" = None) -> None:
    pdf.add_page()
    pw   = pdf.w - pdf.l_margin - pdf.r_margin
    GAP  = 3.0
    cw   = (pw - GAP) / 2          # width of each column
    col_xs = [pdf.l_margin, pdf.l_margin + cw + GAP]

    C_HDR   = (210, 220, 235)
    C_SEC   = (232, 232, 232)
    C_VFR   = (210, 238, 210)
    C_MVFR  = (255, 243, 200)
    C_IFR   = (255, 215, 215)
    C_RADAR = (225, 240, 255)
    LH = 4.0   # line height

    # ── title ─────────────────────────────────────────────────────────────────
    radar_info = _find_radar_online(vatsim, fir_icaos or [])
    radar_str  = f"  ·  {radar_info[0]} {radar_info[1]}" if radar_info else ""
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 12)
    pdf.cell(pw, 7,
             f"Wetterbriefing  ·  {briefing.dep_icao} → {briefing.dest_icao}"
             f"  ({briefing.fetched_at}){radar_str}",
             align="C")
    content_y = pdf.t_margin + 8

    # ── two-column weather sections ───────────────────────────────────────────
    col_data = [
        ("Abflug",   briefing.dep_icao,  briefing.dep_metar_raw,  briefing.dep_taf_raw,  briefing.dep_metar),
        ("Ziel",     briefing.dest_icao, briefing.dest_metar_raw, briefing.dest_taf_raw, briefing.dest_metar),
    ]

    def _metar_rows(p: ParsedMetar) -> list[tuple[str, str]]:
        if p.wind_kt is not None:
            w = ("VRB" if p.wind_vrb else f"{p.wind_dir:03d}°") + f" / {p.wind_kt} kt"
            if p.wind_gust_kt:
                w += f"  G{p.wind_gust_kt}"
        else:
            w = "—"
        if p.cavok:
            vis_str  = "CAVOK"
            ceil_str = "CAVOK"
        else:
            vis_str  = f"{p.vis_m} m" if p.vis_m is not None else "—"
            ceil_str = f"{p.ceiling_ft} ft" if p.ceiling_ft else "—"
        def _tc(v):
            return f"{v:+.0f} °C" if v is not None else "—"
        return [
            ("Wind",     w),
            ("Sicht",    vis_str),
            ("Decke",    ceil_str),
            ("Wolken",   "  ".join(p.clouds) if p.clouds else ("CAVOK" if p.cavok else "—")),
            ("T / Td",   f"{_tc(p.temp_c)} / {_tc(p.dewpoint_c)}"),
            ("QNH",      f"{p.qnh_hpa} hPa" if p.qnh_hpa else "—"),
            ("Wetter",   "  ".join(p.phenomena) if p.phenomena else "—"),
        ]

    final_ys: list[float] = []

    for col_idx, (label, icao, metar_raw, taf_raw, parsed) in enumerate(col_data):
        x = col_xs[col_idx]
        y = content_y

        # Column header
        pdf.set_fill_color(*C_HDR)
        pdf.set_font(font, "B", 10)
        pdf.set_xy(x, y)
        pdf.cell(cw, 6, f"  {icao}  ·  {label}", border=1, fill=True)
        y += 6

        # METAR header
        pdf.set_fill_color(*C_SEC)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(x, y)
        pdf.cell(cw, 5, "  METAR", border=1, fill=True)
        y += 5

        # METAR raw text
        pdf.set_fill_color(255, 255, 255)
        pdf.set_font(font, "", 7.5)
        pdf.set_xy(x, y)
        if metar_raw:
            pdf.multi_cell(cw, LH, metar_raw, border="LBR")
            y = pdf.get_y()
        else:
            pdf.cell(cw, 7, "  Nicht verfügbar", border=1)
            y += 7

        # Parsed METAR key values
        if parsed:
            for row_label, row_val in _metar_rows(parsed):
                pdf.set_xy(x, y)
                pdf.set_font(font, "B", 7.5)
                pdf.cell(cw * 0.28, LH + 0.5, f" {row_label}", border=1)
                pdf.set_font(font, "", 8)
                pdf.cell(cw * 0.72, LH + 0.5, f" {row_val}", border=1)
                y += LH + 0.5

        y += 2  # visual gap before TAF

        # TAF header
        pdf.set_fill_color(*C_SEC)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(x, y)
        pdf.cell(cw, 5, "  TAF", border=1, fill=True)
        y += 5

        # TAF raw text — limit to 15 lines to avoid page overflow
        pdf.set_fill_color(255, 255, 255)
        pdf.set_font(font, "", 7.5)
        pdf.set_xy(x, y)
        if taf_raw:
            lines = taf_raw.splitlines()
            if len(lines) > 15:
                lines = lines[:15] + ["[…]"]
            display = "\n".join(lines)
            pdf.multi_cell(cw, LH, display, border="LBR")
            y = pdf.get_y()
        else:
            pdf.cell(cw, 7, "  Nicht verfügbar", border=1)
            y += 7

        final_ys.append(y)

    # ── VFR assessment strip ──────────────────────────────────────────────────
    assess_y = max(final_ys) + 4

    pdf.set_fill_color(*C_SEC)
    pdf.set_font(font, "B", 9)
    pdf.set_xy(pdf.l_margin, assess_y)
    pdf.cell(pw, 5.5, "  VFR-Beurteilung  ·  Meteorological go/no-go assessment", border=1, fill=True)
    assess_y += 5.5

    STATUS_COLORS = {"VFR": C_VFR, "MVFR": C_MVFR, "IFR": C_IFR}
    STATUS_LABELS = {
        "VFR":  "VFR  OK",
        "MVFR": "MVFR  !",
        "IFR":  "IFR  XX",
    }
    assess_rows = [
        (briefing.dep_icao,  briefing.dep_metar),
        (briefing.dest_icao, briefing.dest_metar),
    ]
    col_defs = [
        ("Platz",   pw * 0.08),
        ("Status",  pw * 0.10),
        ("Wind",    pw * 0.18),
        ("Sicht",   pw * 0.12),
        ("Decke",   pw * 0.12),
        ("QNH hPa", pw * 0.10),
        ("Wetter",  pw * 0.30),
    ]
    # header row
    pdf.set_fill_color(*C_SEC)
    pdf.set_font(font, "B", 8)
    pdf.set_xy(pdf.l_margin, assess_y)
    for col_name, col_w in col_defs:
        pdf.cell(col_w, 5, f" {col_name}", border=1, fill=True)
    assess_y += 5

    for icao, parsed in assess_rows:
        status = parsed.vfr_status() if parsed else "—"
        color  = STATUS_COLORS.get(status, (245, 245, 245))
        pdf.set_fill_color(*color)
        pdf.set_font(font, "B" if status in ("IFR", "MVFR") else "", 8)
        pdf.set_xy(pdf.l_margin, assess_y)

        if parsed:
            if parsed.wind_kt is not None:
                w = ("VRB" if parsed.wind_vrb else f"{parsed.wind_dir:03d}°") + f"/{parsed.wind_kt}kt"
                if parsed.wind_gust_kt:
                    w += f" G{parsed.wind_gust_kt}"
            else:
                w = "—"
            vis_s  = "CAVOK" if parsed.cavok else (f"{parsed.vis_m} m" if parsed.vis_m else "—")
            ceil_s = "CAVOK" if parsed.cavok else (f"{parsed.ceiling_ft} ft" if parsed.ceiling_ft else "—")
            qnh_s  = str(parsed.qnh_hpa) if parsed.qnh_hpa else "—"
            wx_s   = "  ".join(parsed.phenomena) if parsed.phenomena else "—"
        else:
            w = vis_s = ceil_s = qnh_s = wx_s = "—"
            status = "—"

        values = [icao, STATUS_LABELS.get(status, status), w, vis_s, ceil_s, qnh_s, wx_s]
        for (_, col_w), val in zip(col_defs, values):
            pdf.cell(col_w, 5.5, f" {val}", border=1, fill=True)
        assess_y += 5.5

    # VFR minimums note
    pdf.set_fill_color(255, 255, 255)
    pdf.set_xy(pdf.l_margin, assess_y + 1)
    pdf.set_font(font, "I", 6.5)
    pdf.cell(pw, 3.5,
             "VFR: Decke ≥ 3000 ft + Sicht ≥ 5 km  ·  MVFR: Decke 1500–3000 ft oder Sicht 3–5 km  ·  "
             "IFR: Decke < 1500 ft oder Sicht < 3 km  ·  Grenzwerte Deutschland TMZ/CTR: AIP ENR 1.2",
             border=0, align="C")

    # Radar strip (if online)
    if radar_info:
        radar_y = assess_y + 5.5
        pdf.set_fill_color(*C_RADAR)
        pdf.set_font(font, "B", 8)
        pdf.set_xy(pdf.l_margin, radar_y)
        pdf.cell(pw, 5,
                 f"  En-route Radar:  {radar_info[0]}  ·  {radar_info[1]} MHz  (VATSIM live)",
                 border=1, fill=True, align="L")

    # Footer
    pdf.set_y(pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3,
             f"METAR/TAF via VATSIM weather proxy ({briefing.fetched_at}). "
             "Nicht für echten Flugbetrieb — offizielle Briefing-Quellen (DWD, Autobrief) verwenden.",
             align="C")


# ------------------------- FMS export -------------------------

def write_fms(plan: Plan, out_path: Path) -> None:
    """Write an X-Plane FMS v3 flight plan file."""
    type_map = {
        "AIRPORT": 1,
        "VOR": 3,
        "NDB": 2,
        "WAYPOINT": 11,
        "INTERSECTION": 11,
        "USER": 28,
    }
    n = len(plan.waypoints)
    lines = ["I", "3 version", "1", str(n)]
    for i, wp in enumerate(plan.waypoints):
        type_code = type_map.get((wp.type or "").upper(), 28)
        ident = (wp.ident or "UNKN")[:5]
        alt = 0.0 if (i == 0 or i == n - 1) else _effective_leg_alt(plan, i)
        lines.append(f"{type_code} {ident} {alt:.6f} {wp.lat:.6f} {wp.lon:.6f}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------- METAR wind helper -------------------------

def fetch_metar(icao: str, timeout: float = 6.0) -> str | None:
    """Fetch a raw METAR from VATSIM's METAR endpoint. Returns None on failure."""
    url = VATSIM_METAR_URL.format(icao=icao.upper())
    req = urllib.request.Request(url, headers={"User-Agent": VATSIM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8").strip()
            return text if text else None
    except (urllib.error.URLError, TimeoutError):
        return None


def _wind_from_metar(metar: str) -> tuple[float, float] | None:
    """Extract (direction_deg, speed_kt) from a raw METAR string.

    Returns None when wind is missing or can't be parsed.
    Variable-direction wind (VRB) is treated as calm (000).
    """
    # Standard: dddssKT or dddssGggKT
    m = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?KT\b", metar)
    if m:
        return float(m.group(1)) % 360, float(m.group(2))
    # MPS variant (rare at VATSIM, convert to knots)
    m = re.search(r"\b(\d{3})(\d{2,3})(?:G\d{2,3})?MPS\b", metar)
    if m:
        return float(m.group(1)) % 360, round(float(m.group(2)) * 1.944)
    # Variable
    m = re.search(r"\bVRB(\d{2,3})KT\b", metar)
    if m:
        return 0.0, float(m.group(1))
    return None


# ------------------------- weather briefing -------------------------

def fetch_taf(icao: str, timeout: float = 6.0) -> str | None:
    """Fetch a raw TAF from VATSIM's TAF endpoint. Returns None on failure."""
    url = VATSIM_TAF_URL.format(icao=icao.upper())
    req = urllib.request.Request(url, headers={"User-Agent": VATSIM_UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8").strip()
            return text if text else None
    except (urllib.error.URLError, TimeoutError):
        return None


def _parse_temp(s: str) -> float:
    return -float(s[1:]) if s.startswith("M") else float(s)


def parse_metar(raw: str) -> ParsedMetar:
    result = ParsedMetar(raw=raw)

    # Wind
    m = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?KT\b", raw)
    if m:
        if m.group(1) == "VRB":
            result.wind_vrb = True
        else:
            result.wind_dir = int(m.group(1))
        result.wind_kt = int(m.group(2))
        if m.group(3):
            result.wind_gust_kt = int(m.group(3))

    # MPS fallback (convert to kt)
    if result.wind_kt is None:
        m = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?MPS\b", raw)
        if m:
            if m.group(1) == "VRB":
                result.wind_vrb = True
            else:
                result.wind_dir = int(m.group(1))
            result.wind_kt = int(round(float(m.group(2)) * 1.944))
            if m.group(3):
                result.wind_gust_kt = int(round(float(m.group(3)) * 1.944))

    # CAVOK
    if "CAVOK" in raw:
        result.cavok = True
        result.vis_m = 9999
    else:
        # Visibility: standalone 4-digit group or 9999
        m = re.search(r"(?<!\d)(\d{4})(?!\d)", raw)
        if m:
            result.vis_m = int(m.group(1))
        # Clouds
        for cm in re.finditer(r"\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?", raw):
            height_ft = int(cm.group(2)) * 100
            label = cm.group(1) + cm.group(2) + (cm.group(3) or "")
            result.clouds.append(label)
            if cm.group(1) in ("BKN", "OVC"):
                if result.ceiling_ft is None or height_ft < result.ceiling_ft:
                    result.ceiling_ft = height_ft

    # Temp / Dewpoint
    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", raw)
    if m:
        try:
            result.temp_c = _parse_temp(m.group(1))
            result.dewpoint_c = _parse_temp(m.group(2))
        except ValueError:
            pass

    # QNH
    m = re.search(r"\bQ(\d{4})\b", raw)
    if m:
        result.qnh_hpa = int(m.group(1))
    else:
        m = re.search(r"\bA(\d{4})\b", raw)
        if m:
            result.qnh_hpa = int(round(int(m.group(1)) * 0.338639))

    # Significant weather phenomena
    phenom_re = re.compile(
        r"\b(\+|-|VC)?(?:MI|BC|PR|DR|BL|SH|TS|FZ)?"
        r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PO|SQ|FC|SS|DS)\b"
    )
    result.phenomena = [pm.group(0) for pm in phenom_re.finditer(raw)]

    return result


def fetch_weather_briefing(dep_icao: str, dest_icao: str) -> WeatherBriefing:
    fetched_at = datetime.now(timezone.utc).strftime("%H:%MZ")
    dep_m  = fetch_metar(dep_icao)
    dest_m = fetch_metar(dest_icao)
    dep_t  = fetch_taf(dep_icao)
    dest_t = fetch_taf(dest_icao)
    return WeatherBriefing(
        dep_icao=dep_icao.upper(),
        dest_icao=dest_icao.upper(),
        dep_metar_raw=dep_m,
        dest_metar_raw=dest_m,
        dep_taf_raw=dep_t,
        dest_taf_raw=dest_t,
        dep_metar=parse_metar(dep_m) if dep_m else None,
        dest_metar=parse_metar(dest_m) if dest_m else None,
        fetched_at=fetched_at,
    )


# ------------------------- field weather (ATIS / METAR) -------------------------
#
# Quelle pro Platz: zuerst der VATSIM-ATIS-Text der online ATIS-Station (falls
# vorhanden und parsebar), sonst echtes METAR als Fallback. Der Fallback liefert
# bewusst nur Wind, Temperatur und Druck – Sicht/Wolken bleiben leer.

def parse_atis(lines: list[str]) -> ParsedMetar:
    """Extrahiert Wind, Temperatur und QNH aus dem freien VATSIM-ATIS-Text.

    ATIS-Text variiert stark je vACC. Strategie: erst nach eingebetteten
    METAR-Gruppen suchen (viele vACCs hängen das rohe METAR an), dann nach der
    ausgeschriebenen ATIS-Sprache (WIND 250 DEG 8 KT, QNH 1018, TEMP 14 DP 09).
    Sicht/Wolken werden bewusst nicht geparst – der ATIS-Fließtext ist dafür zu
    uneinheitlich, und gefragt sind nur Wind/Temp/Druck.
    """
    text = " ".join(lines).upper()
    pm = ParsedMetar(raw=text)

    # --- Wind: erst METAR-Gruppe (dddssKT / dddssMPS / VRBssKT), dann verbose ---
    m = re.search(r"\b(VRB|\d{3})(\d{2,3})(?:G(\d{2,3}))?(KT|MPS)\b", text)
    if m:
        kt = int(m.group(2))
        gust = int(m.group(3)) if m.group(3) else None
        if m.group(4) == "MPS":
            kt = int(round(kt * 1.944))
            gust = int(round(gust * 1.944)) if gust else None
        pm.wind_kt = kt
        pm.wind_gust_kt = gust
        if m.group(1) == "VRB":
            pm.wind_vrb = True
        else:
            pm.wind_dir = int(m.group(1)) % 360
    else:
        m = re.search(r"\bWIND\b[^0-9]{0,8}(\d{3})[^0-9]{0,10}?(\d{1,3})\s*(?:KT|KNOT)", text)
        if m:
            pm.wind_dir = int(m.group(1)) % 360
            pm.wind_kt = int(m.group(2))
        else:
            m = re.search(r"\bWIND\b[^0-9]{0,4}(?:VRB|VARIABLE)[^0-9]{0,8}(\d{1,3})\s*(?:KT|KNOT)", text)
            if m:
                pm.wind_vrb = True
                pm.wind_kt = int(m.group(1))
        if pm.wind_kt is not None:
            g = re.search(r"\b(?:GUST\w*|MAX(?:IMUM)?)[^0-9]{0,6}(\d{1,3})", text)
            if g:
                pm.wind_gust_kt = int(g.group(1))

    # --- QNH: METAR-Gruppe (Qdddd / Adddd) oder verbose (QNH 1018) ---
    m = re.search(r"\bQ(\d{4})\b", text)
    if m:
        pm.qnh_hpa = int(m.group(1))
    else:
        m = re.search(r"\bQNH\b[^0-9]{0,6}(\d{3,4})", text)
        if m:
            pm.qnh_hpa = int(m.group(1))
        else:
            m = re.search(r"\bA(\d{4})\b", text)   # inHg → hPa
            if m:
                pm.qnh_hpa = int(round(int(m.group(1)) * 0.338639))

    # --- Temp / Taupunkt: METAR-Gruppe (tt/dd) oder verbose (TEMP 14 DP 09) ---
    m = re.search(r"\b(M?\d{2})/(M?\d{2})\b", text)
    if m:
        try:
            pm.temp_c = _parse_temp(m.group(1))
            pm.dewpoint_c = _parse_temp(m.group(2))
        except ValueError:
            pass
    if pm.temp_c is None:
        m = re.search(r"\bTEMP\w*[^0-9M]{0,4}(M?\d{1,2}).{0,14}?(?:DEW\w*|DP|DEWPOINT)[^0-9M]{0,4}(M?\d{1,2})", text)
        if m:
            try:
                pm.temp_c = _parse_temp(m.group(1))
                pm.dewpoint_c = _parse_temp(m.group(2))
            except ValueError:
                pass
        else:
            m = re.search(r"\bTEMP\w*[^0-9M]{0,4}(M?\d{1,2})", text)
            if m:
                try:
                    pm.temp_c = _parse_temp(m.group(1))
                except ValueError:
                    pass

    return pm


def _atis_meta(lines: list[str]) -> dict:
    """ATIS-Kennung (Buchstabe), aktive RWY und Beobachtungszeit aus dem Text."""
    text = " ".join(lines).upper()
    meta: dict = {}
    m = (re.search(r"\bATIS\b(?:\s+\w+)?\s+(?:INFO\w*\s+)?([A-Z])\b", text)
         or re.search(r"\bINFORMATION\s+([A-Z])", text))
    if m:
        meta["atis_code"] = m.group(1)
    m = re.search(r"\bRWY?\s*(\d{2}[LRC]?)", text)
    if m:
        meta["rwy"] = m.group(1)
    m = re.search(r"\b(\d{4})Z\b", text)
    if m:
        meta["time_z"] = m.group(1) + "Z"
    return meta


def field_weather(icao: str, vatsim: "VatsimSnapshot | None",
                  briefing: "WeatherBriefing | None") -> FieldWx | None:
    """Wetter für einen Platz: VATSIM-ATIS bevorzugt, sonst echtes METAR.

    Fällt auf METAR zurück, wenn keine ATIS-Station online ist *oder* der
    ATIS-Text keinen Wind/Temp/QNH hergibt. Der METAR-Fallback wird auf
    Wind/Temp/Druck beschränkt (Sicht/Wolken bleiben leer).
    """
    icao = icao.upper()

    # 1) VATSIM ATIS
    lines = vatsim.atis_text.get(icao) if vatsim else None
    if lines:
        pm = parse_atis(lines)
        if pm.wind_kt is not None or pm.qnh_hpa is not None or pm.temp_c is not None:
            return FieldWx(icao, "VATSIM ATIS", pm, **_atis_meta(lines))

    # 2) Echtes METAR (aus dem bereits geholten Briefing, sonst nachladen)
    pm = None
    if briefing:
        if icao == briefing.dep_icao:
            pm = briefing.dep_metar
        elif icao == briefing.dest_icao:
            pm = briefing.dest_metar
    if pm is None:
        raw = fetch_metar(icao)
        pm = parse_metar(raw) if raw else None
    if pm is not None:
        # nur Wind/Temp/Druck verwenden
        slim = ParsedMetar(
            raw=pm.raw,
            wind_dir=pm.wind_dir, wind_kt=pm.wind_kt,
            wind_gust_kt=pm.wind_gust_kt, wind_vrb=pm.wind_vrb,
            temp_c=pm.temp_c, dewpoint_c=pm.dewpoint_c, qnh_hpa=pm.qnh_hpa,
        )
        return FieldWx(icao, "METAR (real)", slim)

    return None


def _wx_wind_cell(pm: ParsedMetar) -> str:
    if pm.wind_kt is None:
        return ""
    if pm.wind_vrb or pm.wind_dir is None:
        head = "VRB"
    else:
        head = f"{pm.wind_dir:03d}"
    s = f"{head}/{pm.wind_kt:02d}"
    if pm.wind_gust_kt:
        s += f"G{pm.wind_gust_kt}"
    return s


def _wx_ttd_cell(pm: ParsedMetar) -> str:
    if pm.temp_c is None:
        return ""
    s = f"{int(round(pm.temp_c))}"
    if pm.dewpoint_c is not None:
        s += f"/{int(round(pm.dewpoint_c))}"
    return s


# ------------------------- ICAO FPL generator -------------------------

def format_icao_fpl(
    plan: Plan,
    aircraft: dict,
    legs: list[Leg],
    eobt: str,          # HHMM UTC
    pob: int,
    equipment: str,     # e.g. "S/C" or "SDFG/EB1"
    wake: str,          # L / M / H / J
    alternate: str,     # ICAO or ""
    pilot_name: str,    # surname for field 19C
) -> str:
    dep  = plan.waypoints[0]
    dest = plan.waypoints[-1]
    perf = aircraft.get("performance", {})
    fuel = aircraft.get("fuel", {})

    # icao_type overrides type for FPL purposes (e.g. "C172" not "C172S").
    # type is kept as-is for PDF display.
    ac_type = re.sub(r"[^A-Z0-9]", "", (aircraft.get("icao_type") or aircraft.get("type", "C172")).upper())[:10]
    # Field 7: aircraft identification — strip hyphens, max 7 chars
    reg = re.sub(r"[^A-Z0-9]", "", aircraft.get("registration", "NXXXX").upper())[:7]

    tas = int(perf.get("tas_cruise", 110))

    # Field 15 altitude: ICAO Axxx = altitude in hundreds of feet (e.g. A025 = 2500 ft).
    # "VFR" alone is rejected by many parsers including my.vatsim.net.
    alt_code = f"A{int(plan.cruise_alt_ft) // 100:03d}"

    # Route: DCT between named intermediate waypoints only.
    # Coordinate-format waypoints (e.g. 521430N0075330E from user-defined points
    # in Little Navmap / Navigraph) break my.vatsim.net's route parser — drop them.
    def _named(ident: str) -> bool:
        return bool(ident) and not re.match(r"^\d{4,6}[NS]\d{5,7}[EW]$", ident)

    intermediates = [wp.ident for wp in plan.waypoints[1:-1] if _named(wp.ident)]
    if intermediates:
        route_str = "DCT " + " DCT ".join(intermediates) + " DCT"
    else:
        route_str = "DCT"

    # EET for field 16
    total_min = int(round(sum(l.ete_min for l in legs)))
    eet = f"{total_min // 60:02d}{total_min % 60:02d}"

    # Field 16: destination + EET + optional alternate
    f16 = f"{dest.ident}{eet}"
    if alternate:
        f16 += f" {alternate.upper()}"

    # Field 18: other information
    dof = datetime.now().strftime("%y%m%d")
    f18 = f"DOF/{dof} REG/{reg}"

    # Field 19: supplementary — endurance from usable fuel / cruise burn
    capacity = fuel.get("capacity_usable_l", 0)
    burn     = perf.get("fuel_burn_cruise_lph", 33)
    end_min  = int((capacity / burn) * 60) if burn > 0 else 0
    f19_parts = [
        f"E/{end_min // 60:02d}{end_min % 60:02d}",
        f"P/{pob:03d}",
        "R/UV",
        "S/-",
        "J/-",
    ]
    if pilot_name:
        f19_parts.append(f"C/{pilot_name.upper()}")

    lines = [
        f"(FPL-{reg}-VG",
        f"-{ac_type}/{wake}-{equipment}",
        f"-{dep.ident}{eobt}",
        f"-N{tas:04d}{alt_code} {route_str}",
        f"-{f16}",
        f"-{f18}",
        f"-{' '.join(f19_parts)})",
    ]
    return "\n".join(lines)


def _ask_fpl_fields(B: str, C: str, G: str, DIM: str, R: str) -> dict | None:
    """Interactively collect extra FPL fields. Returns a dict or None if declined."""
    print(f"\n{B}{C}ICAO FPL  (für my.vatsim.net Import){R}")
    if input("  → Generieren? [y/N]: ").strip().lower() not in ("y", "yes"):
        return None

    while True:
        raw = input("  EOBT UTC (HHMM, z. B. 1030): ").strip()
        if re.match(r"^\d{4}$", raw) and 0 <= int(raw[:2]) <= 23 and 0 <= int(raw[2:]) <= 59:
            eobt = raw
            break
        print("  Format: HHMM  (z. B. 1030)")

    raw = input("  POB  (Personen an Bord) [2]: ").strip() or "2"
    pob = int(raw) if raw.isdigit() and int(raw) >= 1 else 2

    equipment = (input("  Equipment-Code [SDFG/C]: ").strip() or "SDFG/C").upper()

    raw = input("  Wake turbulence [L]: ").strip().upper() or "L"
    wake = raw if raw in ("L", "M", "H", "J") else "L"

    raw = input("  Ausweichflugplatz ICAO (Enter = keiner): ").strip().upper()
    alternate = raw if re.match(r"^[A-Z]{4}$", raw) else ""

    pilot_name = input("  Pilot Nachname (für Feld 19): ").strip()

    return dict(eobt=eobt, pob=pob, equipment=equipment,
                wake=wake, alternate=alternate, pilot_name=pilot_name)


def collect_vor_info(plan: Plan) -> None:
    """Walk every waypoint and attach a free-text VOR/navaid reference
    (e.g. "233 FROM"). Mutates plan.waypoints in place.

    Called only when the user opted in. Skips silently when stdin is not a
    TTY so non-interactive runs (cron, pipes) never block on input().
    """
    if not sys.stdin.isatty():
        return
    B, C, G, DIM, R = "\033[1m", "\033[36m", "\033[32m", "\033[2m", "\033[0m"
    print(f"\n{B}{C}VOR-Informationen je Wegpunkt{R}")
    print(f"{DIM}Freitext pro Wegpunkt, z. B. \"233 FROM\" oder \"FRD R088\". "
          f"Enter lässt einen Punkt leer.{R}")
    for wp in plan.waypoints:
        label = f"{wp.ident}  {wp.name}".strip() or wp.ident or "(unbenannt)"
        current = f" [{wp.vor_info}]" if wp.vor_info else ""
        raw = input(f"  {label}{current}: ").strip()
        if raw:
            wp.vor_info = raw


# ------------------------- interactive TUI -------------------------

def _tui() -> argparse.Namespace:
    """Interactive setup wizard, runs when navlog.py is called with no arguments."""
    # Enable readline tab-completion for file paths
    try:
        import glob as _glob
        import readline
        readline.set_completer_delims(" \t\n;")
        readline.parse_and_bind("tab: complete")
        readline.set_completer(
            lambda text, state: (
                _glob.glob(text.replace("~", str(Path.home())) + "*") + [None]
            )[state]
        )
    except Exception:
        pass

    B, C, G, DIM, R = "\033[1m", "\033[36m", "\033[32m", "\033[2m", "\033[0m"

    def h(title: str) -> None:
        print(f"\n{B}{C}{title}{R}")

    print(f"\n{B}VFR Navlog{R}  —  no arguments given, running interactive setup")
    print(f"{DIM}Tab-completes file paths. Press Enter to accept [defaults].{R}")

    # --- Plan source ---
    h("Plan source")
    print("  [1]  Little Navmap .lnmpln file")
    print("  [2]  Navigraph Charts  (reads live from the app, macOS only)")
    while True:
        src = input("  → [1]: ").strip() or "1"
        if src in ("1", "2"):
            break
        print("  Enter 1 or 2.")

    navigraph = src == "2"
    plan_path: Path | None = None
    _preview: Plan | None = None

    dep_icao: str | None = None
    cruise_alt_default: float = 2500.0

    if not navigraph:
        h("Flight plan file  (.lnmpln)")
        while True:
            raw = input("  Path: ").strip()
            if not raw:
                print("  (required)")
                continue
            p = Path(raw).expanduser()
            if p.exists():
                plan_path = p
                break
            print(f"  Not found: {p}")
        # Parse immediately so waypoints and cruise alt are available for later prompts.
        try:
            _preview = parse_lnmpln(plan_path)
            dep_icao = _preview.waypoints[0].ident if _preview.waypoints else None
            cruise_alt_default = _preview.cruise_alt_ft
            _wps_str = f"{_preview.waypoints[0].ident} → {_preview.waypoints[-1].ident}"
            print(f"  {G}Loaded: {_wps_str}  ({len(_preview.waypoints)} waypoints, "
                  f"cruise alt {int(cruise_alt_default)} ft){R}")
        except Exception as _exc:
            print(f"  {DIM}Warning: could not parse plan ({_exc}) — waypoints unavailable.{R}")
    else:
        # Navigraph: pre-load the live plan so waypoints are available at the altitude step.
        print(f"  {DIM}Reading active Navigraph flight plan…{R}", end="", flush=True)
        try:
            _preview = read_navigraph_flight(DEFAULT_XPLANE)
            dep_icao = _preview.waypoints[0].ident if _preview.waypoints else None
            cruise_alt_default = _preview.cruise_alt_ft
            _wps_str = f"{_preview.waypoints[0].ident} → {_preview.waypoints[-1].ident}"
            print(f"\r  {G}Loaded: {_wps_str}  ({len(_preview.waypoints)} waypoints, "
                  f"cruise alt {int(cruise_alt_default)} ft){R}")
        except BaseException:
            print(f"\r  {DIM}Could not pre-load — waypoints available after generation.{R}")

    # --- Aircraft ---
    script_dir = Path(__file__).parent
    ac_candidates = sorted(script_dir.glob("aircraft_*.json"))
    if not ac_candidates:
        ac_candidates = sorted(Path(".").glob("aircraft_*.json"))
    ac_default = str(ac_candidates[0]) if ac_candidates else ""

    h("Aircraft JSON")
    if ac_candidates:
        for i, p in enumerate(ac_candidates):
            mark = f"  {G}← default{R}" if i == 0 else ""
            print(f"  [{i + 1}]  {p.name}{mark}")
        print("  [path]  or type a path to a different file")

    aircraft_path: Path | None = None
    while aircraft_path is None:
        hint = f" [{Path(ac_default).name}]" if ac_default else ""
        raw = input(f"  →{hint}: ").strip()
        if not raw and ac_default:
            aircraft_path = Path(ac_default)
        elif raw.isdigit() and ac_candidates:
            idx = int(raw) - 1
            if 0 <= idx < len(ac_candidates):
                aircraft_path = ac_candidates[idx]
            else:
                print(f"  Choose 1–{len(ac_candidates)}.")
        else:
            p = Path(raw).expanduser()
            if p.exists():
                aircraft_path = p
            else:
                print(f"  Not found: {p}")

    # --- Registration ---
    _ac_data: dict = {}
    try:
        _ac_data = json.loads(aircraft_path.read_text())
    except Exception:
        pass
    reg_default = _ac_data.get("registration", "")

    h("Aircraft registration")
    hint = f" [{reg_default}]" if reg_default else ""
    raw = input(f"  →{hint}: ").strip()
    registration = raw if raw else reg_default

    # --- Wind ---
    h("Wind aloft  (DDD/SS, e.g. 270/15 — or 0/0 for calm)")
    if dep_icao:
        print(f"  [M]  fetch surface wind from VATSIM METAR at {dep_icao}")
    else:
        print("  [M]  fetch surface wind from VATSIM METAR  (you'll enter an ICAO)")
    wind_str = "0/0"
    while True:
        raw = input("  → [0/0]: ").strip() or "0/0"
        if raw.upper() == "M":
            icao_q = dep_icao or input("  ICAO for METAR: ").strip().upper()
            if not icao_q:
                print("  (ICAO required)")
                continue
            print(f"  Fetching METAR for {icao_q}…", end="", flush=True)
            metar = fetch_metar(icao_q)
            if not metar:
                print(f"\n  {DIM}Could not reach VATSIM METAR — enter wind manually.{R}")
                continue
            print(f"\r  {DIM}{metar}{R}")
            wdata = _wind_from_metar(metar)
            if wdata:
                wind_str = f"{int(wdata[0]):03d}/{int(wdata[1]):02d}"
                print(f"  {G}Using wind: {wind_str}{R}  {DIM}(surface METAR — not wind aloft){R}")
                break
            else:
                print(f"  {DIM}Could not parse wind from METAR — enter manually.{R}")
                continue
        elif re.match(r"^\d{1,3}/\d{1,3}$", raw):
            wind_str = raw
            break
        else:
            print("  Format: 270/15  or  M to fetch from VATSIM METAR")

    # --- Cruise altitude ---
    h("Cruise altitude  (ft MSL)")
    while True:
        raw = input(f"  → [{int(cruise_alt_default)}]: ").strip() or str(int(cruise_alt_default))
        try:
            val = float(raw)
            if val > 0:
                cruise_alt_ft = val
                break
        except ValueError:
            pass
        print("  Enter a positive number in feet (e.g. 3500).")

    # --- Altitude changes at waypoints ---
    h("Altitude changes at waypoints  (optional)")
    _prev_wps = _preview.waypoints if _preview is not None else []
    # Coordinate-based waypoints (ident starts with a digit) get short aliases GPS1, GPS2, …
    _gps_alias: dict[str, str] = {}       # original_ident.upper() -> "GPS1"
    _alias_to_ident: dict[str, str] = {}  # "GPS1" -> original_ident.upper()
    _gps_n = 1
    for _awp in _prev_wps:
        if _awp.ident and _awp.ident[0].isdigit():
            _al = f"GPS{_gps_n}"
            _gps_alias[_awp.ident.upper()] = _al
            _alias_to_ident[_al] = _awp.ident.upper()
            _gps_n += 1
    if _prev_wps:
        print("  Waypoints in route:")
        for _wi, _wp in enumerate(_prev_wps):
            _al = _gps_alias.get(_wp.ident.upper(), "")
            _label = f"{_al}  ({_wp.ident})" if _al else _wp.ident
            print(f"    {_wi + 1:2d}.  {_label}")
    print(f"  Current cruise alt: {int(cruise_alt_ft)} ft from departure.")
    print("  Enter WP ALT pairs for step climbs/descents. Empty line to finish.")
    # Build example idents from the actual route: first interior named WP and first GPS alias.
    _ex_dep = _prev_wps[0].ident if _prev_wps else "EDDG"
    _ex_named = next(
        (wp.ident for wp in _prev_wps[1:-1] if not wp.ident[0:1].isdigit()),
        "BADGO",
    )
    _ex_named2 = next(
        (wp.ident for wp in _prev_wps[1:-1] if not wp.ident[0:1].isdigit() and wp.ident != _ex_named),
        "WLD",
    )
    _ex_gps = next(iter(_alias_to_ident), "GPS1")
    print(f"  e.g.  {_ex_dep} {_ex_named} 4500   fly 4 500 ft from {_ex_dep} to {_ex_named}, then revert")
    print(f"        {_ex_named} {_ex_named2} 15000  fly 15 000 ft from {_ex_named} to {_ex_named2}, then revert")
    print(f"        {_ex_named2} 5500         step down to 5 500 ft at {_ex_named2} (open-ended)")
    print(f"        {_ex_gps} 4500           same for a GPS fix")
    alt_profile: list[tuple[str, float]] = []
    _known_orig = {wp.ident.upper() for wp in _prev_wps}

    def _resolve(tok: str) -> str:
        t = tok.upper()
        return _alias_to_ident.get(t, t)

    def _disp(ident: str) -> str:
        return _gps_alias.get(ident.upper(), ident)

    while True:
        raw = input("  → ").strip()
        if not raw:
            break
        _parts = raw.split()
        if len(_parts) == 2:
            # WP ALT — altitude from WP onwards until the next change
            _w1, _alt_raw = _parts
            _w1 = _resolve(_w1)
            try:
                _alt_in = float(_alt_raw)
                if _alt_in <= 0:
                    raise ValueError
            except ValueError:
                print("  Altitude must be a positive number in feet.")
                continue
            if _known_orig and _w1 not in _known_orig:
                print(f"  {DIM}Warning: {_w1} not in route — adding anyway.{R}")
            alt_profile.append((_w1, _alt_in))
            print(f"  {G}From {_disp(_w1)}: {int(_alt_in):,} ft{R}")
        elif len(_parts) == 3:
            # WP1 WP2 ALT — closed segment: fly ALT from WP1, revert to cruise_alt_ft at WP2
            _w1, _w2, _alt_raw = _parts
            _w1, _w2 = _resolve(_w1), _resolve(_w2)
            try:
                _alt_in = float(_alt_raw)
                if _alt_in <= 0:
                    raise ValueError
            except ValueError:
                print("  Altitude must be a positive number in feet.")
                continue
            for _wchk in (_w1, _w2):
                if _known_orig and _wchk not in _known_orig:
                    print(f"  {DIM}Warning: {_wchk} not in route — adding anyway.{R}")
            alt_profile.append((_w1, _alt_in))
            alt_profile.append((_w2, cruise_alt_ft))  # auto-revert to base cruise alt
            print(f"  {G}From {_disp(_w1)} to {_disp(_w2)}: {int(_alt_in):,} ft  "
                  f"(→ {int(cruise_alt_ft):,} ft from {_disp(_w2)}){R}")
        else:
            print("  Format:  WP ALT          e.g. BADGO 15000")
            print("           WP1 WP2 ALT     e.g. EDDG HMM 4500")

    # --- Magnetic variation ---
    h("Magnetic variation  (e.g. 4E, 1.0W, -2.5)")
    while True:
        raw = input("  → [4E]: ").strip() or "4E"
        if re.match(r"^[+-]?\d+(\.\d+)?[EWew]?$", raw.strip()):
            magvar_str = raw
            break
        print("  Format: 2.5E  or  2.5W  or  -2.5")

    # --- VATSIM ---
    h("VATSIM  (fetch live ATC frequencies?)")
    vatsim = input("  → [y/N]: ").strip().lower() in ("y", "yes")

    # --- VOR info per waypoint ---
    h("VOR-Informationen je Wegpunkt angeben?  (z. B. 233 FROM)")
    vor_info = input("  → [y/N]: ").strip().lower() in ("y", "yes")

    # --- DFS charts ---
    h("DFS airport charts  (append VFR charts for destination?)")
    raw = input("  → [Y/n]: ").strip().lower()
    dfs_charts = raw not in ("n", "no")

    # --- FMS ---
    h("X-Plane FMS export")
    fms_dir = DEFAULT_XPLANE / "Output" / "FMS plans"
    xp_found = DEFAULT_XPLANE.exists()
    if xp_found:
        print(f"  {DIM}Output: {fms_dir}{R}")
        fms_default = "Y"
    else:
        print(f"  {DIM}X-Plane not found at default path — FMS will go next to PDF if yes{R}")
        fms_default = "N"
    raw = input(f"  → [{fms_default}]: ").strip().lower()
    fms = raw in ("y", "yes") or (not raw and xp_found)

    # --- FPL ---
    fpl_fields = _ask_fpl_fields(B, C, G, DIM, R)

    print()

    return argparse.Namespace(
        navigraph=navigraph,
        plan=plan_path,
        aircraft=aircraft_path,
        wind=wind_str,
        magvar=magvar_str,
        output=None,
        vatsim=vatsim,
        vor_info=vor_info,
        dfs_charts=dfs_charts,
        call_tower_nm=10.0,
        xplane=DEFAULT_XPLANE,
        registration=registration,
        cruise_alt=cruise_alt_ft,
        alt_profile=alt_profile,
        fms=fms,
        fpl_fields=fpl_fields,
        # CLI FPL args are absent in TUI mode; use None as sentinel
        fpl_eobt=None,
    )


# ------------------------- CLI -------------------------

def main():
    ap = argparse.ArgumentParser(
        description="VFR navlog PDF from a Little Navmap plan or Navigraph Charts."
    )
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--plan", type=Path, help="Little Navmap .lnmpln file")
    src.add_argument("--navigraph", action="store_true",
                     help="Read the active flight plan directly from Navigraph Charts (macOS).")
    ap.add_argument("--aircraft", required=True, type=Path)
    ap.add_argument("--wind", default="0/0", help="Wind aloft, DDD/SS, e.g. 270/15")
    ap.add_argument("--magvar", default="4E", help="Magnetic variation, e.g. 4E or -2.5")
    ap.add_argument("--output", default=None, type=Path)
    ap.add_argument("--vatsim", action="store_true",
                    help="Fetch live ATC frequencies from VATSIM for departure and destination.")
    ap.add_argument("--call-tower-nm", type=float, default=10.0,
                    help="Remaining-distance threshold (NM) for the tower-call marker. 0 disables.")
    ap.add_argument("--xplane", type=Path, default=DEFAULT_XPLANE,
                    help="Path to X-Plane 12 root (for apt.dat / earth_nav.dat). "
                         "Set to '' to skip the destination-briefing page.")
    ap.add_argument("--registration", default=None,
                    help="Override the aircraft registration from the JSON (e.g. D-EXXX).")
    ap.add_argument("--cruise-alt", type=float, default=None,
                    help="Override the cruise altitude from the flight plan (feet MSL, e.g. 3500).")
    ap.add_argument("--alt-change", action="append", nargs=2, metavar=("WP", "ALT"),
                    help="Step to a new cruise altitude at a waypoint "
                         "(e.g. --alt-change BADGO 15000). May be given multiple times in route order.")
    ap.add_argument("--fms", action="store_true",
                    help="Write an X-Plane FMS flight plan to Output/FMS plans/ (or next to PDF if --xplane is unset).")
    ap.add_argument("--dfs-charts", action="store_true", default=False,
                    help="Append VFR charts for the destination from the DFS AIP.")
    ap.add_argument("--vor-info", action="store_true", default=False,
                    help="Prompt for a free-text VOR reference (e.g. '233 FROM') per waypoint.")
    fpl_grp = ap.add_argument_group("ICAO FPL output  (my.vatsim.net import)")
    fpl_grp.add_argument("--fpl-eobt", default=None, metavar="HHMM",
                         help="Generate ICAO FPL with this EOBT (UTC), e.g. 1030. "
                              "Omit remaining --fpl-* flags to be prompted interactively.")
    fpl_grp.add_argument("--fpl-pob", type=int, default=2, metavar="N",
                         help="Persons on board (default 2).")
    fpl_grp.add_argument("--fpl-equipment", default="SDFG/C", metavar="CODE",
                         help="Equipment/surveillance code, e.g. SDFG/C (default) or SG/S.")
    fpl_grp.add_argument("--fpl-wake", default="L", choices=["L", "M", "H", "J"],
                         help="Wake turbulence category (default L).")
    fpl_grp.add_argument("--fpl-alternate", default="", metavar="ICAO",
                         help="Alternate aerodrome ICAO (optional).")
    fpl_grp.add_argument("--fpl-pilot", default="", metavar="NAME",
                         help="Pilot surname for FPL field 19C.")

    if not sys.argv[1:]:
        args = _tui()
    else:
        args = ap.parse_args()

    xplane_path: Path | None = Path(args.xplane) if args.xplane and str(args.xplane).strip() else None

    if args.navigraph:
        plan = read_navigraph_flight(xplane_path)
        source_note = (
            "Erzeugt aus Navigraph Charts — Werte ohne Gewähr. "
            "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
        )
    else:
        plan = parse_lnmpln(args.plan)
        source_note = (
            "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. "
            "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
        )

    aircraft = json.loads(args.aircraft.read_text())
    if getattr(args, "registration", None):
        aircraft["registration"] = args.registration
    if getattr(args, "cruise_alt", None) is not None:
        plan.cruise_alt_ft = float(args.cruise_alt)
    # TUI sets alt_profile directly; CLI uses --alt-change WP ALT pairs.
    _tui_profile = getattr(args, "alt_profile", None)
    if _tui_profile is not None:
        plan.alt_profile = _tui_profile
    else:
        raw_changes = getattr(args, "alt_change", None) or []
        plan.alt_profile = [(wp.upper(), float(alt)) for wp, alt in raw_changes]
    wind = parse_wind(args.wind)
    magvar = parse_magvar(args.magvar)

    if getattr(args, "vor_info", False):
        collect_vor_info(plan)

    fir_icaos = _german_firs_for_route(plan.waypoints)

    snapshot: VatsimSnapshot | None = None
    briefing: WeatherBriefing | None = None
    field_wx: dict[str, FieldWx] = {}
    if args.vatsim:
        dep_icao  = plan.waypoints[0].ident
        dest_icao = plan.waypoints[-1].ident
        icaos     = [dep_icao, dest_icao]
        all_icaos = icaos + [f for f in fir_icaos if f not in icaos]
        snapshot  = fetch_vatsim(all_icaos)
        if snapshot is not None:
            for icao in icaos:
                got = snapshot.frequencies.get(icao.upper(), {})
                if got:
                    print(f"[vatsim] {icao}: " + ", ".join(f"{k}={v}" for k, v in got.items()))
                else:
                    print(f"[vatsim] {icao}: no controllers online")
            for fir in fir_icaos:
                got = snapshot.frequencies.get(fir.upper(), {})
                if got:
                    print(f"[vatsim] {fir}: " + ", ".join(f"{k}={v}" for k, v in got.items()))
                else:
                    print(f"[vatsim] {fir}: no radar online")
        print(f"[weather] fetching METAR/TAF for {dep_icao}, {dest_icao}…")
        briefing = fetch_weather_briefing(dep_icao, dest_icao)
        for icao, pm in [(dep_icao, briefing.dep_metar), (dest_icao, briefing.dest_metar)]:
            if pm:
                print(f"[weather] {icao}: {pm.vfr_status()}  ceiling={pm.ceiling_ft} ft  vis={pm.vis_m} m  QNH={pm.qnh_hpa}")

        # Platzwetter: VATSIM-ATIS bevorzugt, sonst echtes METAR (Wind/Temp/Druck).
        for icao in icaos:
            wx = field_weather(icao, snapshot, briefing)
            if wx:
                field_wx[icao] = wx
                print(f"[weather] {icao}: {wx.source}  Wind={_wx_wind_cell(wx.parsed) or '—'}  "
                      f"T/Td={_wx_ttd_cell(wx.parsed) or '—'}  QNH={wx.parsed.qnh_hpa or '—'}")

        # Ohne explizites --wind den Abflug-Oberflächenwind als Wind aloft nutzen.
        dep_wx = field_wx.get(dep_icao.upper())
        if args.wind == "0/0" and dep_wx and dep_wx.parsed.wind_kt is not None:
            wd = dep_wx.parsed.wind_dir if (dep_wx.parsed.wind_dir is not None
                                            and not dep_wx.parsed.wind_vrb) else 0
            wind = (float(wd), float(dep_wx.parsed.wind_kt))
            print(f"[weather] wind aloft aus {dep_icao} {wd:03d}/{int(wind[1]):02d} "
                  f"({dep_wx.source}, Oberfläche – kein Höhenwind)")

    tas = aircraft["performance"]["tas_cruise"]
    burn = aircraft["performance"]["fuel_burn_cruise_lph"]
    legs = compute_legs(plan, tas, wind, magvar, burn)
    apply_hemispheric_rule(plan, legs)

    dest_info: AirportInfo | None = None
    if xplane_path:
        dest_info = load_destination_info(plan, xplane_path)

    if args.output is not None:
        out = args.output
    else:
        env = _load_env(Path(__file__).parent / ".env")
        out = _smart_output(
            env,
            plan.waypoints[0].ident,
            plan.waypoints[-1].ident,
            aircraft.get("type", ""),
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    render(plan, aircraft, legs, wind, magvar, out,
           vatsim=snapshot, call_tower_nm=args.call_tower_nm,
           dest_info=dest_info, source_note=source_note,
           fir_icaos=fir_icaos, weather=briefing,
           dfs_charts=getattr(args, "dfs_charts", False),
           field_wx=field_wx)
    print(f"Wrote {out}")
    total_d = sum(l.distance_nm for l in legs)
    total_t = sum(l.ete_min for l in legs)
    total_f = sum(l.fuel_l for l in legs)
    print(f"Total: {total_d:.1f} NM, {total_t:.0f} min, {total_f:.1f} L (trip only)")

    if getattr(args, "fms", False):
        dep_icao = plan.waypoints[0].ident.upper()
        dest_icao = plan.waypoints[-1].ident.upper()
        fms_name = f"{dep_icao}-{dest_icao}.fms"
        if xplane_path:
            fms_path = xplane_path / "Output" / "FMS plans" / fms_name
        else:
            fms_path = out.parent / fms_name
        write_fms(plan, fms_path)
        print(f"Wrote FMS  {fms_path}")

    # ── ICAO FPL ─────────────────────────────────────────────────────────────
    # Resolve FPL fields from TUI (args.fpl_fields) or from CLI (--fpl-eobt …).
    fpl_fields: dict | None = getattr(args, "fpl_fields", None)
    if fpl_fields is None and getattr(args, "fpl_eobt", None):
        # CLI mode: --fpl-eobt was given; remaining fields from CLI args or defaults.
        fpl_fields = dict(
            eobt=args.fpl_eobt,
            pob=args.fpl_pob,
            equipment=args.fpl_equipment,
            wake=args.fpl_wake,
            alternate=args.fpl_alternate,
            pilot_name=args.fpl_pilot,
        )

    if fpl_fields is not None:
        import urllib.parse
        B, C, DIM, R = "\033[1m", "\033[36m", "\033[2m", "\033[0m"
        fpl_str = format_icao_fpl(plan, aircraft, legs, **fpl_fields)
        sep = "─" * 62
        print(f"\n{B}ICAO FPL{R}")
        print(sep)
        print(fpl_str)
        print(sep)
        fpl_path = out.with_suffix(".fpl")
        fpl_path.write_text(fpl_str + "\n", encoding="utf-8")
        print(f"{DIM}Saved:  {fpl_path}{R}")

        # Build the my.vatsim.net direct-open URL (CRLF + quote_plus, matching
        # the encoding the site uses in its own shareable links).
        raw = urllib.parse.quote_plus(fpl_str.replace("\n", "\r\n"))
        fpl_url = f"https://my.vatsim.net/pilots/flightplan/beta?raw={raw}"
        print(f"\n{B}Prefile URL{R}  (öffnet Formular vorausgefüllt):")
        print(fpl_url)
        if sys.platform == "darwin":
            subprocess.run(["open", fpl_url], check=False)
        print()

    if sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
