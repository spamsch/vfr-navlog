"""Command-line entry point and run orchestration."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .config import DEFAULT_XPLANE, PROJECT_ROOT, _load_env, _smart_output
from .exports import collect_vor_info, format_icao_fpl, write_fms
from .legs import apply_hemispheric_rule, compute_legs
from .lnmpln import parse_lnmpln, parse_magvar, parse_wind
from .model import (
    AirportInfo,
    FieldWx,
    RenderContext,
    RunConfig,
    VatsimSnapshot,
    WeatherBriefing,
)
from .navigraph import read_navigraph_flight
from .pdf import render
from .tui import _tui
from .vatsim import _german_firs_for_route, fetch_vatsim
from .weather import (
    _wx_ttd_cell,
    _wx_wind_cell,
    fetch_weather_briefing,
    field_weather,
)
from .xplane import load_destination_info


def _build_parser() -> argparse.ArgumentParser:
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
    return ap


def _runconfig_from_cli(args: argparse.Namespace) -> RunConfig:
    xplane_path = Path(args.xplane) if args.xplane and str(args.xplane).strip() else None
    alt_profile = [(wp.upper(), float(alt)) for wp, alt in (args.alt_change or [])]
    fpl_fields = None
    if args.fpl_eobt:
        fpl_fields = dict(
            eobt=args.fpl_eobt,
            pob=args.fpl_pob,
            equipment=args.fpl_equipment,
            wake=args.fpl_wake,
            alternate=args.fpl_alternate,
            pilot_name=args.fpl_pilot,
        )
    return RunConfig(
        navigraph=args.navigraph,
        plan_path=args.plan,
        aircraft_path=args.aircraft,
        wind=parse_wind(args.wind),
        wind_was_default=(args.wind == "0/0"),
        magvar=parse_magvar(args.magvar),
        registration=args.registration,
        cruise_alt_ft=(float(args.cruise_alt) if args.cruise_alt is not None else None),
        alt_profile=alt_profile,
        output=args.output,
        xplane_path=xplane_path,
        vatsim=args.vatsim,
        vor_info=args.vor_info,
        with_dfs_charts=args.dfs_charts,
        call_tower_nm=args.call_tower_nm,
        fms=args.fms,
        fpl_fields=fpl_fields,
    )


def main():
    if not sys.argv[1:]:
        config = _tui()
    else:
        config = _runconfig_from_cli(_build_parser().parse_args())
    run(config)


def run(config: RunConfig) -> None:
    xplane_path = config.xplane_path

    if config.navigraph:
        plan = read_navigraph_flight(xplane_path)
        source_note = (
            "Erzeugt aus Navigraph Charts — Werte ohne Gewähr. "
            "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
        )
    else:
        plan = parse_lnmpln(config.plan_path)
        source_note = (
            "Erzeugt aus Little Navmap .lnmpln — Werte ohne Gewähr. "
            "Vor dem Flug gegen aktuelle Briefing-Unterlagen prüfen."
        )

    aircraft = json.loads(config.aircraft_path.read_text())
    if config.registration:
        aircraft["registration"] = config.registration
    if config.cruise_alt_ft is not None:
        plan.cruise_alt_ft = config.cruise_alt_ft
    plan.alt_profile = config.alt_profile
    wind = config.wind
    magvar = config.magvar

    if config.vor_info:
        collect_vor_info(plan)

    fir_icaos = _german_firs_for_route(plan.waypoints)

    snapshot: VatsimSnapshot | None = None
    briefing: WeatherBriefing | None = None
    field_wx: dict[str, FieldWx] = {}
    if config.vatsim:
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
        if config.wind_was_default and dep_wx and dep_wx.parsed.wind_kt is not None:
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

    if config.output is not None:
        out = config.output
    else:
        env = _load_env(PROJECT_ROOT / ".env")
        out = _smart_output(
            env,
            plan.waypoints[0].ident,
            plan.waypoints[-1].ident,
            aircraft.get("type", ""),
        )
    out.parent.mkdir(parents=True, exist_ok=True)

    ctx = RenderContext(
        plan=plan, aircraft=aircraft, legs=legs, wind=wind, magvar=magvar,
        vatsim=snapshot, dest_info=dest_info, weather=briefing, field_wx=field_wx,
        fir_icaos=fir_icaos, source_note=source_note,
        call_tower_nm=config.call_tower_nm, with_dfs_charts=config.with_dfs_charts,
    )
    render(ctx, out)
    print(f"Wrote {out}")
    total_d = sum(l.distance_nm for l in legs)
    total_t = sum(l.ete_min for l in legs)
    total_f = sum(l.fuel_l for l in legs)
    print(f"Total: {total_d:.1f} NM, {total_t:.0f} min, {total_f:.1f} L (trip only)")

    if config.fms:
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
    # FPL fields were resolved when the RunConfig was built (TUI prompts or --fpl-* flags).
    fpl_fields = config.fpl_fields
    if fpl_fields is not None:
        import urllib.parse
        B, DIM, R = "\033[1m", "\033[2m", "\033[0m"
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
