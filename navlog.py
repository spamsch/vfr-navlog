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
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF


VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_UA = "navlog.py/1.0 (+local VFR planning script)"

# VATSIM convention: tune 122.800 ("UNICOM") whenever no ATC station is online
# in the airspace you're operating in.
UNICOM_FREQ = "122.800"

# Default macOS Steam install. Override via --xplane.
DEFAULT_XPLANE = Path.home() / "Library/Application Support/Steam/steamapps/common/X-Plane 12"
NAV_REL = "Custom Data/earth_nav.dat"
NAV_FALLBACK_REL = "Resources/default data/earth_nav.dat"
APT_REL = "Global Scenery/Global Airports/Earth nav data/apt.dat"

SURFACE_NAMES = {
    "1": "Asphalt", "2": "Beton", "3": "Gras", "4": "Sand", "5": "Schotter",
    "12": "Trocken", "13": "Wasser", "14": "Schnee/Eis", "15": "Transparent",
}


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


# ------------------------- nav math -------------------------

def great_circle(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Returns (initial_true_course_deg, distance_nm) between two points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlon = math.radians(lon2 - lon1)

    # initial bearing
    y = math.sin(dlon) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlon)
    tc = (math.degrees(math.atan2(y, x)) + 360) % 360

    # great-circle distance (haversine), nm
    a = math.sin((phi2 - phi1) / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlon / 2) ** 2
    dist_nm = 2 * 3440.065 * math.asin(math.sqrt(a))
    return tc, dist_nm


def apply_wind(tc_deg: float, tas_kt: float, wind_from_deg: float, wind_kt: float) -> tuple[float, float, float]:
    """Returns (wca_deg, magnetic_heading_no_var, gs_kt). Caller adds magvar."""
    # wind correction angle: sin(WCA) = (W/TAS) * sin(wind_from - TC)
    rel = math.radians(wind_from_deg - tc_deg)
    if tas_kt <= 0:
        return 0.0, tc_deg, 0.0
    sin_wca = (wind_kt / tas_kt) * math.sin(rel)
    sin_wca = max(-1.0, min(1.0, sin_wca))
    wca = math.degrees(math.asin(sin_wca))
    th = tc_deg + wca
    # ground speed
    gs = math.sqrt(
        tas_kt ** 2 + wind_kt ** 2 - 2 * tas_kt * wind_kt * math.cos(rel - math.radians(wca))
    )
    return wca, th, gs


# ------------------------- parsing -------------------------

@dataclass
class Waypoint:
    name: str
    ident: str
    type: str
    lat: float
    lon: float
    alt_ft: float | None = None
    region: str | None = None


@dataclass
class Plan:
    waypoints: list[Waypoint]
    cruise_alt_ft: float
    flightplan_type: str
    cycle: str
    created: str


def parse_lnmpln(path: Path) -> Plan:
    tree = ET.parse(path)
    root = tree.getroot()
    fp = root.find("Flightplan")
    if fp is None:
        sys.exit(f"No <Flightplan> in {path}")

    header = fp.find("Header")
    cruise = float(header.findtext("CruisingAlt", "2500")) if header is not None else 2500
    fp_type = header.findtext("FlightplanType", "VFR") if header is not None else "VFR"
    created = header.findtext("CreationDate", "") if header is not None else ""
    nav = fp.find("NavData")
    cycle = nav.get("Cycle", "") if nav is not None else ""

    wps: list[Waypoint] = []
    for w in fp.findall("Waypoints/Waypoint"):
        pos = w.find("Pos")
        if pos is None:
            continue
        wps.append(Waypoint(
            name=w.findtext("Name", "") or w.findtext("Ident", ""),
            ident=w.findtext("Ident", ""),
            type=w.findtext("Type", ""),
            lat=float(pos.get("Lat", "0")),
            lon=float(pos.get("Lon", "0")),
            alt_ft=float(pos.get("Alt", "0")) if pos.get("Alt") else None,
            region=w.findtext("Region"),
        ))
    return Plan(waypoints=wps, cruise_alt_ft=cruise, flightplan_type=fp_type, cycle=cycle, created=created)


def parse_wind(s: str) -> tuple[float, float]:
    m = re.match(r"^\s*(\d{1,3})\s*/\s*(\d{1,3})\s*$", s)
    if not m:
        sys.exit(f"--wind must be DDD/SS (e.g. 270/15), got {s!r}")
    return float(m.group(1)) % 360, float(m.group(2))


def parse_magvar(s: str) -> float:
    """Accepts '2.5E', '2.5W', '+2.5', '-2.5', '2.5' (default east)."""
    s = s.strip().upper()
    m = re.match(r"^([+-]?\d+(\.\d+)?)([EW])?$", s)
    if not m:
        sys.exit(f"--magvar must be like 2.5E or -2.5, got {s!r}")
    val = float(m.group(1))
    suffix = m.group(3)
    # Convention: east variation is negative when applied (TC - VAR = MH for east)
    # We'll return a signed value where positive = east.
    if suffix == "W":
        val = -abs(val)
    elif suffix == "E":
        val = abs(val)
    return val


# ------------------------- VATSIM -------------------------

@dataclass
class VatsimSnapshot:
    fetched_at: str
    update_time: str
    frequencies: dict[str, dict[str, str]]  # icao -> {role -> "118.300"}
    atis_text: dict[str, list[str]]         # icao -> raw ATIS lines

    def empty(self) -> bool:
        return not any(self.frequencies.values())


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


# ------------------------- X-Plane nav data -------------------------

@dataclass
class Runway:
    ident_a: str
    ident_b: str
    surface: str
    width_m: float
    length_m: float


@dataclass
class IlsLoc:
    runway: str
    ident: str
    freq_mhz: float
    type_desc: str


@dataclass
class AirportInfo:
    icao: str
    name: str
    elevation_ft: float = 0.0
    city: str = ""
    transition_alt: str = ""
    transition_level: str = ""
    iata: str = ""
    runways: list[Runway] = None  # type: ignore[assignment]
    ils_locs: list[IlsLoc] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.runways is None:
            self.runways = []
        if self.ils_locs is None:
            self.ils_locs = []


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * 6371000.0 * math.asin(math.sqrt(a))


def parse_airport(apt_path: Path, icao: str) -> AirportInfo | None:
    if not apt_path.exists():
        return None
    icao_upper = icao.upper()
    info: AirportInfo | None = None
    in_target = False
    try:
        with open(apt_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                parts = line.split()
                if not parts:
                    continue
                row = parts[0]
                if row == "1" and len(parts) >= 5:
                    # New airport header. Finish previous if it was ours.
                    if in_target:
                        break
                    if parts[4].upper() == icao_upper:
                        info = AirportInfo(
                            icao=icao_upper,
                            name=" ".join(parts[5:]),
                            elevation_ft=float(parts[1]),
                        )
                        in_target = True
                    else:
                        in_target = False
                elif in_target and info is not None:
                    if row == "1302" and len(parts) >= 3:
                        key, val = parts[1], " ".join(parts[2:])
                        if key == "city":
                            info.city = val
                        elif key == "transition_alt":
                            info.transition_alt = val
                        elif key == "transition_level":
                            info.transition_level = val
                        elif key == "iata_code":
                            info.iata = val
                    elif row == "100" and len(parts) >= 26:
                        # 100 width surface ... end1_ident lat lon ... end2_ident lat lon ...
                        try:
                            width = float(parts[1])
                            surface = parts[2]
                            ident_a = parts[8]
                            lat_a = float(parts[9])
                            lon_a = float(parts[10])
                            ident_b = parts[17]
                            lat_b = float(parts[18])
                            lon_b = float(parts[19])
                            length_m = _haversine_m(lat_a, lon_a, lat_b, lon_b)
                            info.runways.append(Runway(
                                ident_a=ident_a, ident_b=ident_b,
                                surface=SURFACE_NAMES.get(surface, surface),
                                width_m=width, length_m=length_m,
                            ))
                        except (ValueError, IndexError):
                            continue
    except OSError:
        return None
    return info


def parse_ils_locs(nav_path: Path, icao: str) -> list[IlsLoc]:
    if not nav_path.exists():
        return []
    icao_upper = icao.upper()
    out: list[IlsLoc] = []
    try:
        with open(nav_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if icao_upper not in line:
                    continue
                parts = line.split()
                if len(parts) < 11:
                    continue
                if parts[0] not in {"4", "5"}:  # ILS LOC or LOC-only
                    continue
                if parts[8].upper() != icao_upper:
                    continue
                try:
                    freq_raw = int(parts[4])
                except ValueError:
                    continue
                runway = parts[10]
                ident = parts[7]
                type_desc = " ".join(parts[11:]) if len(parts) > 11 else ""
                out.append(IlsLoc(
                    runway=runway, ident=ident,
                    freq_mhz=freq_raw / 100.0,
                    type_desc=type_desc,
                ))
    except OSError:
        return []
    return out


def load_destination_info(plan: Plan, xplane_path: Path) -> AirportInfo | None:
    if not plan.waypoints:
        return None
    dest = plan.waypoints[-1]
    if dest.type.upper() != "AIRPORT":
        return None
    apt_path = xplane_path / APT_REL
    info = parse_airport(apt_path, dest.ident)
    if info is None:
        info = AirportInfo(icao=dest.ident.upper(), name=dest.name or dest.ident,
                           elevation_ft=dest.alt_ft or 0.0)

    nav_path = xplane_path / NAV_REL
    if not nav_path.exists():
        nav_path = xplane_path / NAV_FALLBACK_REL
    info.ils_locs = parse_ils_locs(nav_path, info.icao)
    return info


# ------------------------- tower-call marker -------------------------

def find_call_marker(legs: list["Leg"], threshold_nm: float) -> int | None:
    """Index of the *waypoint row* where the pilot should make the tower call.

    Convention: the call should go out while inbound to the leg whose end is
    inside `threshold_nm` of the destination. We anchor the visible mark at
    the *start* of that leg (the waypoint the pilot is leaving when the call
    should happen). Returns None if no legs.
    """
    if not legs or threshold_nm <= 0:
        return None
    total = sum(l.distance_nm for l in legs)
    cum = 0.0
    for i, l in enumerate(legs):
        cum += l.distance_nm
        remaining_after = total - cum
        if remaining_after <= threshold_nm:
            return i  # row index of legs[i].from_wp (i+0 in waypoint table, since row 0 is departure)
    return 0


# ------------------------- leg computation -------------------------

@dataclass
class Leg:
    from_wp: Waypoint
    to_wp: Waypoint
    tc: float
    wca: float
    th: float
    mh: float
    distance_nm: float
    gs_kt: float
    ete_min: float
    fuel_l: float


def compute_legs(plan: Plan, tas: float, wind: tuple[float, float], magvar: float, burn_lph: float) -> list[Leg]:
    legs: list[Leg] = []
    for i in range(len(plan.waypoints) - 1):
        a, b = plan.waypoints[i], plan.waypoints[i + 1]
        tc, dist = great_circle(a.lat, a.lon, b.lat, b.lon)
        wca, th, gs = apply_wind(tc, tas, wind[0], wind[1])
        mh = (th - magvar + 360) % 360  # east variation subtracts
        th = th % 360
        ete = (dist / gs) * 60 if gs > 0 else 0
        fuel = (ete / 60) * burn_lph
        legs.append(Leg(a, b, tc % 360, wca, th, mh, dist, gs, ete, fuel))
    return legs


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


def render(plan: Plan, aircraft: dict, legs: list[Leg], wind: tuple[float, float], magvar: float, out: Path, vatsim: VatsimSnapshot | None = None, call_tower_nm: float = 10.0, dest_info: AirportInfo | None = None) -> None:
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
    sub_rh = (block_h - 8) / 4
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

    # UNICOM row: merged across the full Frequenzen width, bold and centered.
    unicom_y = y + 8 + 3 * sub_rh
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
    pdf.set_font(font, "", 7)
    for ri, label in enumerate(atis_rows):
        ry = y2 + atis_header_h + ri * atis_row_h
        pdf.set_xy(pdf.l_margin, ry)
        for ci, (_, w) in enumerate(atis_cols):
            text = " " + label if ci == 0 else ""
            pdf.cell(w, atis_row_h, text, border=1)
    atis_h = atis_header_h + len(atis_rows) * atis_row_h

    # ---------- nav table ----------
    nav_y = y2 + atis_h + 3
    columns = [
        ("Waypoint",        56, "L"),
        ("VOR/NDB",         18, "C"),
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
    cx = pdf.l_margin
    for name, w, _ in columns:
        pdf.set_xy(cx, nav_y)
        if name in highlight:
            pdf.set_font(font, "B", 9)
        else:
            pdf.set_font(font, "B", 7)
        pdf.multi_cell(w, header_h / 2, name, border=1, align="C")
        cx += w

    row_h = 6.5
    n_rows = max(len(plan.waypoints), 10)
    cum_dist = 0.0
    cum_ete = 0.0
    cum_fuel = 0.0
    for i in range(n_rows):
        ry = nav_y + header_h + i * row_h
        pdf.set_fill_color(245, 245, 245) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)

        if i == 0:
            wp = plan.waypoints[0]
            row = [
                f"{wp.ident}  {wp.name}",
                "",
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
                "",
                fmt_int(plan.cruise_alt_ft) if i < len(plan.waypoints) - 1 else fmt_int(wp.alt_ft or 0),
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

    fuel_y = nav_y + header_h + n_rows * row_h + 4
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

    # bottom note
    pdf.set_xy(pdf.l_margin, pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 6)
    pdf.cell(0, 3,
             "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen.",
             align="C")

    render_phraseology(pdf, font, plan, aircraft, vatsim)

    if dest_info is not None:
        render_destination_page(pdf, font, dest_info, vatsim)

    pdf.output(str(out))


# ------------------------- phraseology page -------------------------

def render_phraseology(pdf: FPDF, font: str, plan: Plan, aircraft: dict, vatsim: VatsimSnapshot | None) -> None:
    """A bilingual VFR phraseology cheat-sheet templated to this flight."""
    pdf.add_page()
    pw = pdf.w - pdf.l_margin - pdf.r_margin

    dep = plan.waypoints[0]
    dest = plan.waypoints[-1]
    reg = aircraft.get("registration", "D-XXXX")
    ac_type = aircraft.get("type", "Cirrus")

    def clean_name(s: str) -> str:
        # lnmpln files sometimes have "Muenster- Osnabrueck" (space after hyphen).
        return s.replace("- ", "-").strip(" -")

    dep_name = clean_name(dep.name or dep.ident).split()[0] or dep.ident
    dest_name = clean_name(dest.name or dest.ident).split()[0] or dest.ident

    tower_freq = ""
    if vatsim:
        tower_freq = vatsim.frequencies.get(dest.ident.upper(), {}).get("tower", "")

    # FIS sector for north Germany. Pilot edits when flying elsewhere.
    fis_station_de = "Bremen Information"
    fis_station_en = "Bremen Information"

    # Title
    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 14)
    pdf.cell(pw, 8, "Sprechgruppen VFR  ·  VFR Radio Phraseology", align="C")
    pdf.ln(9)

    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"Templated für {reg} ({ac_type}), {dep.ident} → {dest.ident}. "
             f"Platzhalter [in eckigen Klammern] vor jedem Funkspruch anpassen.",
             align="C")
    pdf.ln(7)

    tower_suffix = f" auf {tower_freq}" if tower_freq else ""
    tower_suffix_en = f" on {tower_freq}" if tower_freq else ""
    info_letter = "[Information X]"
    info_letter_en = "[information X]"

    sections = [
        (
            "1. Abflug auf UNICOM (kein ATC)  ·  Departure call on UNICOM",
            f"Verkehr {dep_name}, {reg}, {ac_type}, rollt zur Piste [25] zum Start, "
            f"VFR nach {dest_name}, Flughöhe [2500 Fuß].",
            f"{dep_name} traffic, {reg}, {ac_type}, taxiing to runway [25] for departure, "
            f"VFR to {dest_name}, cruising [2500 feet].",
        ),
        (
            "2. Erstanruf FIS  ·  FIS initial call",
            f"{fis_station_de}, {reg}.",
            f"{fis_station_en}, {reg}.",
        ),
        (
            "3. Positionsmeldung an FIS  ·  Position report to FIS  (POB hier abgeben!)",
            f"{reg}, {ac_type}, [5 NM östlich Osnabrück], [2500 Fuß], "
            f"VFR von {dep.ident} nach {dest.ident}, [1] Person an Bord, "
            f"Flugverfolgung erbeten.",
            f"{reg}, {ac_type}, [5 NM east of Osnabrück], [2500 feet], "
            f"VFR from {dep.ident} to {dest.ident}, [1] POB, "
            f"request flight following.",
        ),
        (
            "4. Frequenzwechsel verlassen FIS  ·  Leaving FIS frequency",
            f"{reg} verlässt Ihre Frequenz, wechselt auf {dest.ident} Turm{tower_suffix}, "
            "vielen Dank, auf Wiederhören.",
            f"{reg} leaving your frequency, switching to {dest.ident} Tower{tower_suffix_en}, "
            "thank you, good day.",
        ),
        (
            "5. Erstanruf Zielturm  ·  Destination tower initial call",
            f"{dest_name} Turm, {reg}.",
            f"{dest_name} Tower, {reg}.",
        ),
        (
            "6. Anflug-Positionsmeldung  ·  Inbound position report",
            f"{reg}, {ac_type}, [10 NM östlich], [2500 Fuß], {info_letter} erhalten, "
            f"VFR-Landung {dest_name}.",
            f"{reg}, {ac_type}, [10 NM east], [2500 feet], {info_letter_en} received, "
            f"VFR landing {dest_name}.",
        ),
        (
            "7. Am Pflichtmeldepunkt  ·  Compulsory reporting point",
            f"{reg}, am Punkt [Whiskey / VP227], [2500 Fuß].",
            f"{reg}, point [Whiskey / VP227], [2500 feet].",
        ),
        (
            "8. Landefreigabe-Rücklesung  ·  Landing clearance readback",
            f"Landung Piste [25], {reg}.",
            f"Cleared to land runway [25], {reg}.",
        ),
        (
            "9. Piste verlassen / Bodenkontrolle  ·  Vacated / ground call",
            f"{reg} Piste [25] verlassen, frage Rollanweisung zum [GA-Vorfeld].",
            f"{reg} runway [25] vacated, request taxi to [GA apron].",
        ),
        (
            "10. Notruf  ·  Distress call (MAYDAY)",
            f"Mayday, Mayday, Mayday — [Station], {reg}, {ac_type}, "
            f"[Position], [Problem: z. B. Triebwerksausfall], [Absicht], "
            f"[Personen an Bord], [Treibstoff in min].",
            f"Mayday, Mayday, Mayday — [station], {reg}, {ac_type}, "
            f"[position], [problem: e.g. engine failure], [intentions], "
            f"[souls on board], [fuel remaining].",
        ),
    ]

    de_w = pw / 2
    en_w = pw - de_w
    line_h = 3.6

    for title, de_text, en_text in sections:
        # section header bar
        pdf.set_font(font, "B", 9)
        pdf.set_fill_color(225, 225, 225)
        pdf.cell(pw, 5, " " + title, border=1, fill=True)
        pdf.ln(5)

        start_y = pdf.get_y()
        # German cell
        pdf.set_xy(pdf.l_margin, start_y)
        pdf.set_font(font, "", 9)
        pdf.multi_cell(de_w, line_h + 0.6, de_text, border="LBR")
        de_end = pdf.get_y()

        # English cell
        pdf.set_xy(pdf.l_margin + de_w, start_y)
        pdf.set_font(font, "I", 9)
        pdf.multi_cell(en_w, line_h + 0.6, en_text, border="LBR")
        en_end = pdf.get_y()

        pdf.set_y(max(de_end, en_end) + 1.2)

    # ---------- destination controller responses ----------
    pdf.ln(2)
    pdf.set_font(font, "B", 10)
    pdf.set_fill_color(210, 220, 235)
    pdf.cell(pw, 6, f"  Was Sie vom {dest.ident} Turm hören können  ·  What you may hear from {dest.ident} Tower",
             border=1, fill=True)
    pdf.ln(6)

    controller_rows = [
        (
            "Personen an Bord  ·  POB query",
            f"{reg}, Personen an Bord?",
            f"{reg}, persons on board?",
        ),
        (
            "Einflug CTR freigegeben  ·  CTR entry clearance",
            f"{reg}, Einflug genehmigt über [Whiskey], QNH [1018], Information [Bravo] aktuell.",
            f"{reg}, cleared to enter via [Whiskey], QNH [1018], information [Bravo] current.",
        ),
        (
            "Meldepunkt verlangt  ·  Report point",
            f"{reg}, melden Sie [Pflichtmeldepunkt Whiskey], [2500 Fuß].",
            f"{reg}, report [compulsory point Whiskey], [2500 feet].",
        ),
        (
            "Landefreigabe  ·  Landing clearance",
            f"{reg}, Landung Piste [25], Wind [240 Grad 12 Knoten].",
            f"{reg}, cleared to land runway [25], wind [240 degrees 12 knots].",
        ),
        (
            "Durchstart  ·  Go-around",
            f"{reg}, durchstarten, Steigflug [3000 Fuß], links nach [Norden].",
            f"{reg}, go around, climb [3000 feet], left turn [northbound].",
        ),
        (
            "Rollanweisung nach Landung  ·  Taxi after landing",
            f"{reg}, rollen Sie über [Bravo] zum [GA-Vorfeld], Bodenkontrolle nicht erforderlich.",
            f"{reg}, taxi via [Bravo] to [GA apron], ground contact not required.",
        ),
    ]

    sit_w = 56
    de_resp_w = (pw - sit_w) / 2
    en_resp_w = pw - sit_w - de_resp_w
    pdf.set_fill_color(240, 240, 240)
    pdf.set_font(font, "B", 7.5)
    pdf.cell(sit_w, 4, " Situation", border=1, fill=True)
    pdf.cell(de_resp_w, 4, " Deutsch", border=1, fill=True)
    pdf.cell(en_resp_w, 4, " English", border=1, fill=True)
    pdf.ln(4)
    pdf.set_fill_color(255, 255, 255)
    for sit, de_msg, en_msg in controller_rows:
        start_y = pdf.get_y()
        pdf.set_xy(pdf.l_margin, start_y)
        pdf.set_font(font, "B", 8)
        pdf.multi_cell(sit_w, 4, sit, border="LBR")
        h_sit = pdf.get_y() - start_y
        pdf.set_xy(pdf.l_margin + sit_w, start_y)
        pdf.set_font(font, "", 8)
        pdf.multi_cell(de_resp_w, 4, de_msg, border="BR")
        h_de = pdf.get_y() - start_y
        pdf.set_xy(pdf.l_margin + sit_w + de_resp_w, start_y)
        pdf.set_font(font, "I", 8)
        pdf.multi_cell(en_resp_w, 4, en_msg, border="BR")
        h_en = pdf.get_y() - start_y
        pdf.set_y(start_y + max(h_sit, h_de, h_en))

    # footer reminder
    pdf.ln(2)
    pdf.set_font(font, "I", 7)
    pdf.multi_cell(pw, 3,
                   "Hinweis: FIS-Sektor und -Frequenz richten sich nach dem Fluggebiet "
                   "(Bremen / Langen / München Information). POB wird in der Regel beim FIS-Erstkontakt "
                   "angegeben; der Tower fragt ggf. separat nach. Pflichtmeldepunkte, Pistennummer "
                   "und ATIS-Buchstabe vor dem Anflug aktualisieren. Bei VATSIM ohne ATC: "
                   f"Position blind auf UNICOM {UNICOM_FREQ} MHz absetzen.",
                   align="C")


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


# ------------------------- CLI -------------------------

def main():
    ap = argparse.ArgumentParser(description="VFR navlog PDF from a Little Navmap plan.")
    ap.add_argument("--plan", required=True, type=Path)
    ap.add_argument("--aircraft", required=True, type=Path)
    ap.add_argument("--wind", default="0/0", help="Wind aloft, DDD/SS, e.g. 270/15")
    ap.add_argument("--magvar", default="2.5E", help="Magnetic variation, e.g. 2.5E or -2.5")
    ap.add_argument("--output", default="navlog.pdf", type=Path)
    ap.add_argument("--vatsim", action="store_true",
                    help="Fetch live ATC frequencies from VATSIM for departure and destination.")
    ap.add_argument("--call-tower-nm", type=float, default=10.0,
                    help="Remaining-distance threshold (NM) for the tower-call marker. 0 disables.")
    ap.add_argument("--xplane", type=Path, default=DEFAULT_XPLANE,
                    help="Path to X-Plane 12 root (for apt.dat / earth_nav.dat). "
                         "Set to '' to skip the destination-briefing page.")
    args = ap.parse_args()

    plan = parse_lnmpln(args.plan)
    aircraft = json.loads(args.aircraft.read_text())
    wind = parse_wind(args.wind)
    magvar = parse_magvar(args.magvar)

    snapshot: VatsimSnapshot | None = None
    if args.vatsim:
        icaos = [plan.waypoints[0].ident, plan.waypoints[-1].ident]
        snapshot = fetch_vatsim(icaos)
        if snapshot is not None:
            for icao in icaos:
                got = snapshot.frequencies.get(icao.upper(), {})
                if got:
                    print(f"[vatsim] {icao}: " + ", ".join(f"{k}={v}" for k, v in got.items()))
                else:
                    print(f"[vatsim] {icao}: no controllers online")

    tas = aircraft["performance"]["tas_cruise"]
    burn = aircraft["performance"]["fuel_burn_cruise_lph"]
    legs = compute_legs(plan, tas, wind, magvar, burn)

    dest_info: AirportInfo | None = None
    if args.xplane and str(args.xplane).strip():
        dest_info = load_destination_info(plan, args.xplane)

    render(plan, aircraft, legs, wind, magvar, args.output,
           vatsim=snapshot, call_tower_nm=args.call_tower_nm,
           dest_info=dest_info)
    print(f"Wrote {args.output}")
    total_d = sum(l.distance_nm for l in legs)
    total_t = sum(l.ete_min for l in legs)
    total_f = sum(l.fuel_l for l in legs)
    print(f"Total: {total_d:.1f} NM, {total_t:.0f} min, {total_f:.1f} L (trip only)")


if __name__ == "__main__":
    main()
