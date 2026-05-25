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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fpdf import FPDF


VATSIM_URL = "https://data.vatsim.net/v3/vatsim-data.json"
VATSIM_METAR_URL = "https://metar.vatsim.net/metar.php?id={icao}"
VATSIM_UA = "navlog.py/1.0 (+local VFR planning script)"

# VATSIM convention: tune 122.800 ("UNICOM") whenever no ATC station is online
# in the airspace you're operating in.
UNICOM_FREQ = "122.800"

# Default macOS Steam install. Override via --xplane.
DEFAULT_XPLANE = Path.home() / "Library/Application Support/Steam/steamapps/common/X-Plane 12"
NAV_REL = "Custom Data/earth_nav.dat"
NAV_FALLBACK_REL = "Resources/default data/earth_nav.dat"
FIX_REL = "Resources/default data/earth_fix.dat"
APT_REL = "Global Scenery/Global Airports/Earth nav data/apt.dat"

NAVIGRAPH_LDB = Path.home() / "Library/Application Support/Navigraph Charts/Local Storage/leveldb"

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
    freq: str | None = None


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


def render(plan: Plan, aircraft: dict, legs: list[Leg], wind: tuple[float, float], magvar: float, out: Path, vatsim: VatsimSnapshot | None = None, call_tower_nm: float = 10.0, dest_info: AirportInfo | None = None, source_note: str = "") -> None:
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
                wp.freq or "",
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
                wp.freq or "",
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
    note = source_note or "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
    pdf.cell(0, 3, note, align="C")

    render_phraseology(pdf, font, plan, aircraft, vatsim)

    if dest_info is not None:
        render_destination_page(pdf, font, dest_info, vatsim)

    pdf.output(str(out))


# ------------------------- phraseology page -------------------------

def render_phraseology(pdf: FPDF, font: str, plan: Plan, aircraft: dict, vatsim: VatsimSnapshot | None) -> None:
    """Two-page phraseology: (1) FIS Bremen Information, (2) CTR entry via Whiskey."""
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

    # ── PAGE 1: FIS Bremen Information ────────────────────────────────────────
    pdf.add_page()

    pdf.set_xy(pdf.l_margin, pdf.t_margin)
    pdf.set_font(font, "B", 13)
    pdf.cell(pw, 7, "Sprechgruppen VFR  ·  1: FIS Bremen Information", align="C")
    pdf.ln(8)
    pdf.set_font(font, "I", 8)
    pdf.cell(pw, 4,
             f"Templated für {reg} ({ac_type}), {dep.ident} → {dest.ident}.  "
             "[Eckige Klammern] vor jedem Spruch anpassen.",
             align="C")
    pdf.ln(6)

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


# ------------------------- Navigraph Charts integration -------------------------

def _decode_dms(token: str) -> tuple[float, float] | None:
    """Parse a Navigraph/ICAO DMS coordinate like '520630N0075118E' → (lat, lon)."""
    m = re.match(r'^(\d{2})(\d{2})(\d{2})([NS])(\d{3})(\d{2})(\d{2})([EW])$', token)
    if not m:
        return None
    lat = int(m.group(1)) + int(m.group(2)) / 60 + int(m.group(3)) / 3600
    if m.group(4) == 'S':
        lat = -lat
    lon = int(m.group(5)) + int(m.group(6)) / 60 + int(m.group(7)) / 3600
    if m.group(8) == 'W':
        lon = -lon
    return lat, lon


def _airport_position(apt_path: Path, icao: str) -> tuple[float, float] | None:
    """Approximate airport lat/lon by averaging its runway endpoints from apt.dat."""
    if not apt_path.exists():
        return None
    icao_upper = icao.upper()
    in_target = False
    lats: list[float] = []
    lons: list[float] = []
    try:
        with open(apt_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] == "1" and len(parts) >= 5:
                    if in_target:
                        break
                    in_target = (parts[4].upper() == icao_upper)
                elif in_target and parts[0] == "100" and len(parts) >= 20:
                    try:
                        lats += [float(parts[9]), float(parts[18])]
                        lons += [float(parts[10]), float(parts[19])]
                    except (ValueError, IndexError):
                        pass
    except OSError:
        return None
    if not lats:
        return None
    return sum(lats) / len(lats), sum(lons) / len(lons)


def _build_nav_index(xplane_path: Path, idents: set[str]) -> dict[str, tuple[float, float, str]]:
    """Resolve navaid/fix idents to (lat, lon, freq_str) from X-Plane's nav and fix databases."""
    result: dict[str, tuple[float, float, str]] = {}
    if not idents:
        return result

    # earth_nav.dat — VOR (type 3, freq in 10s of kHz → MHz) and NDB (type 2, freq in kHz)
    nav_path = xplane_path / NAV_REL
    if not nav_path.exists():
        nav_path = xplane_path / NAV_FALLBACK_REL
    if nav_path.exists():
        try:
            with open(nav_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) < 9 or parts[0] not in {"2", "3"}:
                        continue
                    ident = parts[7]
                    if ident in idents and ident not in result:
                        try:
                            lat, lon = float(parts[1]), float(parts[2])
                            raw_freq = int(parts[4])
                            if parts[0] == "3":
                                freq_str = f"{raw_freq / 100:.2f}"
                            else:
                                freq_str = str(raw_freq)
                            result[ident] = (lat, lon, freq_str)
                        except ValueError:
                            pass
        except OSError:
            pass

    # earth_fix.dat — named waypoints/intersections (no frequency)
    remaining = idents - set(result)
    if remaining:
        fix_path = xplane_path / FIX_REL
        if fix_path.exists():
            try:
                with open(fix_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) < 3:
                            continue
                        ident = parts[2]
                        if ident in remaining and ident not in result:
                            try:
                                result[ident] = (float(parts[0]), float(parts[1]), "")
                            except ValueError:
                                pass
            except OSError:
                pass

    return result


def _navigraph_plan(data: dict, xplane_path: Path | None) -> Plan:
    """Convert a parsed Navigraph flightSelectedFlight JSON to a Plan."""
    routestring = data.get("routestring", "")
    cruise_alt = float(data.get("cruisingAltitude") or 2500)

    # Tokenise; drop routing keywords
    tokens = [t for t in routestring.split() if t.upper() not in {"DCT", "N/A"}]
    if not tokens:
        sys.exit("Navigraph plan has an empty routestring.")

    # Strip runway suffix from both ends (airports can carry e.g. EDLI-RW11)
    tokens[0] = tokens[0].split("-")[0]
    tokens[-1] = tokens[-1].split("-")[0]

    # Navigraph sometimes stores the routestring reversed relative to the title.
    # Detect via "X to Y" in the title and flip if needed.
    title = data.get("title", "")
    m_title = re.match(r'^(\w+)\s+to\s+(\w+)$', title.strip(), re.IGNORECASE)
    if m_title:
        expected_origin = m_title.group(1).upper()
        if tokens[0].upper() != expected_origin and tokens[-1].upper() == expected_origin:
            tokens = list(reversed(tokens))

    # Sort tokens into inline lat/lon vs. named idents that need lookup
    named_idents: set[str] = {t for t in tokens if _decode_dms(t) is None}

    apt_pos: dict[str, tuple[float, float, str]] = {}
    nav_pos: dict[str, tuple[float, float, str]] = {}

    if xplane_path:
        apt_path = xplane_path / APT_REL
        for ident in named_idents:
            p = _airport_position(apt_path, ident)
            if p:
                apt_pos[ident] = (p[0], p[1], "")
        unresolved = named_idents - set(apt_pos)
        if unresolved:
            nav_pos = _build_nav_index(xplane_path, unresolved)

    all_pos = {**apt_pos, **nav_pos}

    waypoints: list[Waypoint] = []
    for i, tok in enumerate(tokens):
        coords = _decode_dms(tok)
        if coords:
            waypoints.append(Waypoint(name="", ident=tok, type="USER", lat=coords[0], lon=coords[1]))
        else:
            entry = all_pos.get(tok)
            if entry is None:
                print(f"[navigraph] could not resolve {tok!r} — skipped", file=sys.stderr)
                continue
            lat, lon, freq_str = entry
            is_airport = (i == 0 or i == len(tokens) - 1)
            waypoints.append(Waypoint(
                name="", ident=tok,
                type="AIRPORT" if is_airport else "VOR",
                lat=lat, lon=lon,
                freq=freq_str or None,
            ))

    if len(waypoints) < 2:
        sys.exit("Navigraph plan: fewer than 2 waypoints could be resolved.")

    return Plan(
        waypoints=waypoints,
        cruise_alt_ft=cruise_alt,
        flightplan_type=data.get("rules", "VFR"),
        cycle="Navigraph",
        created=data.get("updatedAt", ""),
    )


def read_navigraph_flight(xplane_path: Path | None) -> Plan:
    """Read the active flight plan from Navigraph Charts' Electron localStorage."""
    import shutil
    import tempfile

    # ccl_chromium_reader is not on PyPI; look for it next to this script or in $HOME
    script_dir = Path(__file__).parent
    for candidate in [script_dir / "ccl_chromium_reader", Path.home() / "ccl_chromium_reader"]:
        if candidate.exists():
            sys.path.insert(0, str(candidate))
            break

    try:
        from ccl_chromium_reader import ccl_chromium_localstorage
    except ImportError:
        sys.exit(
            "ccl_chromium_reader not found. Install it next to navlog.py:\n"
            "  pip install brotli\n"
            "  pip install 'ccl_simplesnappy @ git+https://github.com/cclgroupltd/ccl_simplesnappy.git'\n"
            "  git clone --depth 1 https://github.com/cclgroupltd/ccl_chromium_reader.git"
        )

    if not NAVIGRAPH_LDB.exists():
        sys.exit(f"Navigraph Charts LevelDB not found at {NAVIGRAPH_LDB}")

    # Copy to a temp dir — LevelDB is exclusively locked while Navigraph is open
    tmp = Path(tempfile.mkdtemp())
    try:
        shutil.copytree(NAVIGRAPH_LDB, tmp / "ldb")
        with ccl_chromium_localstorage.LocalStoreDb(tmp / "ldb") as ls:
            best = None
            for rec in ls.iter_all_records():
                if rec.script_key == "flightSelectedFlight" and rec.is_live and rec.value:
                    if best is None or rec.leveldb_seq_number > best.leveldb_seq_number:
                        best = rec
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if best is None:
        sys.exit("No active flight plan found in Navigraph Charts.")

    data = json.loads(best.value)
    title = data.get("title", "?")
    rules = data.get("rules", "?")
    print(f"[navigraph] {title}  ({rules})")
    return _navigraph_plan(data, xplane_path)


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
        alt = 0.0 if (i == 0 or i == n - 1) else float(plan.cruise_alt_ft)
        lines.append(f"{type_code} {ident} {alt:.6f} {wp.lat:.6f} {wp.lon:.6f}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------- config / output path -------------------------

def _load_env(path: Path) -> dict[str, str]:
    """Parse a .env file (KEY=VALUE). Ignores blank lines and # comments."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip().strip('"').strip("'")
    return result


def _smart_output(env: dict[str, str], dep_icao: str, dest_icao: str, ac_type: str) -> Path:
    base = Path(env.get("NAVLOG_OUTPUT_DIR", ".")).expanduser()
    subdir = base / f"{dep_icao.upper()}-{dest_icao.upper()}"
    date_slug = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ac_type).strip("-").lower()
    filename = f"navlog_{date_slug}_{slug}.pdf" if slug else f"navlog_{date_slug}.pdf"
    return subdir / filename


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


# ------------------------- interactive TUI -------------------------

def _tui() -> argparse.Namespace:
    """Interactive setup wizard, runs when navlog.py is called with no arguments."""
    # Enable readline tab-completion for file paths
    try:
        import readline
        import glob as _glob
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
        # Parse early so we can suggest the departure ICAO and cruise altitude.
        try:
            _preview = parse_lnmpln(plan_path)
            dep_icao = _preview.waypoints[0].ident if _preview.waypoints else None
            cruise_alt_default = _preview.cruise_alt_ft
        except Exception:
            pass

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

    # --- Magnetic variation ---
    h("Magnetic variation  (e.g. 2.5E, 1.0W, -2.5)")
    while True:
        raw = input("  → [2.5E]: ").strip() or "2.5E"
        if re.match(r"^[+-]?\d+(\.\d+)?[EWew]?$", raw.strip()):
            magvar_str = raw
            break
        print("  Format: 2.5E  or  2.5W  or  -2.5")

    # --- VATSIM ---
    h("VATSIM  (fetch live ATC frequencies?)")
    vatsim = input("  → [y/N]: ").strip().lower() in ("y", "yes")

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

    print()

    return argparse.Namespace(
        navigraph=navigraph,
        plan=plan_path,
        aircraft=aircraft_path,
        wind=wind_str,
        magvar=magvar_str,
        output=None,
        vatsim=vatsim,
        call_tower_nm=10.0,
        xplane=DEFAULT_XPLANE,
        registration=registration,
        cruise_alt=cruise_alt_ft,
        fms=fms,
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
    ap.add_argument("--magvar", default="2.5E", help="Magnetic variation, e.g. 2.5E or -2.5")
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
    ap.add_argument("--fms", action="store_true",
                    help="Write an X-Plane FMS flight plan to Output/FMS plans/ (or next to PDF if --xplane is unset).")

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
           dest_info=dest_info, source_note=source_note)
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

    if sys.platform == "darwin":
        subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
