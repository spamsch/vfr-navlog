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
from vfr_navlog.vatsim import (  # noqa: E402,F401
    _find_radar_online,
    _german_firs_for_route,
    _normalize_freq,
    fetch_vatsim,
)
from vfr_navlog.weather import (  # noqa: E402,F401
    _wind_from_metar,
    _wx_ttd_cell,
    _wx_wind_cell,
    fetch_metar,
    fetch_taf,
    fetch_weather_briefing,
    field_weather,
    parse_atis,
    parse_metar,
)
from vfr_navlog.exports import (  # noqa: E402,F401
    _ask_fpl_fields,
    collect_vor_info,
    format_icao_fpl,
    write_fms,
)
from vfr_navlog.pdf import (  # noqa: E402,F401
    render,
    render_destination_page,
    render_phraseology,
    render_weather_page,
)
from vfr_navlog.pdf.base import (  # noqa: E402,F401
    FONT_CANDIDATES,
    NavlogPDF,
    fmt_int,
    hms,
    install_fonts,
)
from vfr_navlog.pdf.charts import _append_dfs_charts  # noqa: E402,F401


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
