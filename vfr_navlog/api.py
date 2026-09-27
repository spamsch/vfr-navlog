"""JSON bridge for the desktop app: ``python -m vfr_navlog.api`` reads one request
object on stdin and writes one response object on stdout.

Requests: ``{"cmd": "aircraft"}``, ``{"cmd": "resolve", ...}``, ``{"cmd": "pdf", ...}``.
Responses: ``{"ok": true, ...}`` or ``{"ok": false, "error": "..."}``.

Everything the pipeline prints (progress lines, warnings) is diverted to stderr so
stdout carries only the JSON response.
"""
from __future__ import annotations

import contextlib
import json
import sys
from datetime import date
from pathlib import Path

from .config import DEFAULT_XPLANE, PROJECT_ROOT
from .fixes import attach_vor_fixes
from .geo import haversine_nm
from .legs import _effective_leg_alt, apply_hemispheric_rule, compute_legs, find_call_marker
from .lnmpln import parse_magvar, parse_wind
from .model import Plan, RunConfig, VorStation
from .navigraph import plan_from_route
from .pdf.navlog_page import _fix_line
from .vatsim import _find_radar_online, _german_firs_for_route, fetch_vatsim
from .weather import fetch_metar, fetch_taf, parse_metar
from .xplane import load_airport_infos, load_vors

# VORs offered for radial tracking: within this distance of the route and of their
# published range (capped like the cross-check fixes).
VOR_SEARCH_NM = 60.0
VOR_MAX_RANGE_NM = 80.0


def vors_near_route(plan: Plan, stations: list[VorStation], limit_nm: float = VOR_SEARCH_NM) -> list[dict]:
    """Stations within *limit_nm* of any point along the route, nearest first."""
    samples: list[tuple[float, float]] = []
    wps = plan.waypoints
    for a, b in zip(wps, wps[1:]):
        steps = max(1, int(haversine_nm(a.lat, a.lon, b.lat, b.lon) // 2))
        samples += [(a.lat + (b.lat - a.lat) * t / steps, a.lon + (b.lon - a.lon) * t / steps)
                    for t in range(steps)]
    samples.append((wps[-1].lat, wps[-1].lon))

    out = []
    for st in stations:
        # Cheap box filter first: 1° lat = 60 NM.
        if not any(abs(st.lat - la) < 1.2 and abs(st.lon - lo) < 2.0 for la, lo in samples):
            continue
        d = min(haversine_nm(st.lat, st.lon, la, lo) for la, lo in samples)
        if d <= limit_nm:
            out.append({
                "ident": st.ident, "name": st.name, "freq": st.freq, "lat": st.lat, "lon": st.lon,
                "var": st.slaved_var, "dme": st.has_dme,
                "range_nm": min(st.range_nm or VOR_MAX_RANGE_NM, VOR_MAX_RANGE_NM),
                "route_dist_nm": round(d, 1),
            })
    return sorted(out, key=lambda v: v["route_dist_nm"])


def _xplane(req: dict) -> Path | None:
    raw = req.get("xplane")
    path = Path(raw) if raw else DEFAULT_XPLANE
    return path if path.exists() else None


def _aircraft_path(name: str) -> Path:
    path = Path(name)
    return path if path.is_absolute() else PROJECT_ROOT / path


def cmd_aircraft(_req: dict) -> dict:
    """The aircraft_*.json profiles next to navlog.py."""
    out = []
    for path in sorted(PROJECT_ROOT.glob("aircraft_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        perf = data.get("performance", {})
        out.append({
            "file": path.name,
            "type": data.get("type", ""),
            "registration": data.get("registration", ""),
            "tas_kt": perf.get("tas_cruise"),
            "burn_lph": perf.get("fuel_burn_cruise_lph"),
        })
    return {"aircraft": out}


def cmd_resolve(req: dict) -> dict:
    """Route string → waypoints, legs, VOR cross-checks, destination ILS, OFM cycle."""
    xplane = _xplane(req)
    plan = plan_from_route(req["route"], xplane)
    aircraft = json.loads(_aircraft_path(req["aircraft"]).read_text(encoding="utf-8"))
    if req.get("cruise_alt_ft"):
        plan.cruise_alt_ft = float(req["cruise_alt_ft"])
    wind = parse_wind(req.get("wind") or "0/0")
    magvar = parse_magvar(req.get("magvar") or "4E")

    stations = load_vors(xplane) if xplane else []
    if req.get("vor_fixes", True) and stations:
        attach_vor_fixes(plan, stations)

    perf = aircraft["performance"]
    legs = compute_legs(plan, perf["tas_cruise"], wind, magvar, perf["fuel_burn_cruise_lph"])
    if req.get("hemispheric", True):
        apply_hemispheric_rule(plan, legs)

    dep_info, dest_info = load_airport_infos(plan, xplane) if xplane else (None, None)

    from .baselayers import _resolve_cycle
    from .ofm import DEFAULT_CACHE
    dep = plan.waypoints[0]
    cycle = _resolve_cycle(dep.lat, dep.lon, date.today(), DEFAULT_CACHE)

    return {
        "waypoints": [
            {
                "ident": wp.ident, "name": wp.name, "type": wp.type,
                "lat": wp.lat, "lon": wp.lon, "freq": wp.freq,
                "fixes": [_fix_line(fx) for fx in wp.fixes],
            }
            for wp in plan.waypoints
        ],
        "legs": [
            {
                "from": leg.from_wp.ident, "to": leg.to_wp.ident,
                "tc": round(leg.tc), "mh": round(leg.mh), "wca": round(leg.wca),
                "distance_nm": round(leg.distance_nm, 1), "gs_kt": round(leg.gs_kt),
                "ete_min": round(leg.ete_min, 1), "fuel_l": round(leg.fuel_l, 1),
                "alt_ft": round(_effective_leg_alt(plan, k)),
            }
            for k, leg in enumerate(legs)
        ],
        "call_leg_idx": find_call_marker(legs, 10.0),
        "dest_ils": [
            {"runway": ils.runway, "ident": ils.ident, "freq_mhz": round(ils.freq_mhz, 2)}
            for ils in sorted(dest_info.ils_locs, key=lambda l: l.runway)
        ] if dest_info else [],
        "dep_freqs": dep_info.frequencies if dep_info else {},
        "dest_freqs": dest_info.frequencies if dest_info else {},
        "aircraft": {"type": aircraft.get("type", ""), "registration": aircraft.get("registration", ""),
                     "tas_kt": perf["tas_cruise"]},
        "wind": [wind[0], wind[1]],
        "magvar": magvar,
        "ofm_cycle": cycle,
        "vors": vors_near_route(plan, stations) if stations else [],
        "dest_info": {
            "name": dest_info.name,
            "elevation_ft": round(dest_info.elevation_ft),
            "transition_alt": dest_info.transition_alt,
            "runways": [f"{rw.ident_a}/{rw.ident_b} {rw.surface} {round(rw.length_m)} m"
                        for rw in dest_info.runways],
        } if dest_info else None,
        "firs": _german_firs_for_route(plan.waypoints),
    }


def cmd_pdf(req: dict) -> dict:
    """Generate the navlog PDF with the app's per-waypoint notes; returns its path."""
    from .cli import run

    xplane = _xplane(req)
    config = RunConfig(
        navigraph=False,
        plan_path=None,
        route=req["route"],
        aircraft_path=_aircraft_path(req["aircraft"]),
        wind=parse_wind(req.get("wind") or "0/0"),
        wind_was_default=(req.get("wind") or "0/0") == "0/0",
        magvar=parse_magvar(req.get("magvar") or "4E"),
        registration=req.get("registration") or None,
        cruise_alt_ft=float(req["cruise_alt_ft"]) if req.get("cruise_alt_ft") else None,
        # [[waypoint ident, alt ft], …]: from that waypoint on, cruise at alt.
        alt_profile=[(str(ident), float(alt)) for ident, alt in req.get("alt_profile") or []],
        output=Path(req["output"]) if req.get("output") else None,
        xplane_path=xplane,
        vatsim=bool(req.get("vatsim", False)),
        vor_info=False,
        with_dfs_charts=bool(req.get("dfs_charts", False)),
        call_tower_nm=10.0,
        fms=False,
        fpl_fields=None,
        vor_fixes=bool(req.get("vor_fixes", True)),
        wp_maps=bool(req.get("wp_maps", False)),
        table_style=req.get("table", "notes"),
        phraseology=bool(req.get("phraseology", False)),
        waypoint_notes=list(req.get("notes") or []),
        hemispheric=bool(req.get("hemispheric", True)),
    )
    out = run(config)
    return {"path": str(Path(out).resolve())}


def cmd_field(req: dict) -> dict:
    """Live data for one airfield: VATSIM frequencies + ATIS text, en-route radar, METAR/TAF.

    Every source fails soft: a missing feed comes back empty, never as an error.
    """
    from concurrent.futures import ThreadPoolExecutor
    from datetime import datetime, timezone

    icao = str(req["icao"]).upper()
    firs = [str(f).upper() for f in req.get("firs") or []]
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_vatsim = ex.submit(fetch_vatsim, [icao, *firs])
        f_metar = ex.submit(fetch_metar, icao)
        f_taf = ex.submit(fetch_taf, icao)
    snapshot = f_vatsim.result()
    metar = f_metar.result()
    parsed = parse_metar(metar) if metar else None
    radar = _find_radar_online(snapshot, firs)
    return {
        "icao": icao,
        "fetched_at": datetime.now(timezone.utc).strftime("%H:%MZ"),
        "vatsim_ok": snapshot is not None,
        "freqs": snapshot.frequencies.get(icao, {}) if snapshot else {},
        "atis": snapshot.atis_text.get(icao, []) if snapshot else [],
        "radar": {"name": radar[0], "freq": radar[1]} if radar else None,
        "metar": metar,
        "taf": f_taf.result(),
        "vfr_status": parsed.vfr_status() if parsed else None,
        "qnh_hpa": parsed.qnh_hpa if parsed else None,
    }


def cmd_charts(req: dict) -> dict:
    """Airport chart pages for one aerodrome (cached; downloads when missing or outdated)."""
    from . import airport_charts

    icao = str(req["icao"]).upper()
    if req.get("cached_only"):
        return airport_charts.cached(icao) or {"icao": icao, "pages": []}
    return airport_charts.fetch(icao, force=bool(req.get("force")))


def cmd_kneeboard(req: dict) -> dict:
    """Send the app's kneeboard data to the in-sim FlyWithLua window."""
    from .kneeboard import send

    root = Path(req["xplane"]) if req.get("xplane") else DEFAULT_XPLANE
    return send(req["plan"], root)


COMMANDS = {"aircraft": cmd_aircraft, "resolve": cmd_resolve, "pdf": cmd_pdf, "field": cmd_field,
            "kneeboard": cmd_kneeboard, "charts": cmd_charts}


def handle(req: dict) -> dict:
    """Dispatch one request. Never raises: failures come back as {"ok": false}."""
    try:
        with contextlib.redirect_stdout(sys.stderr):
            fn = COMMANDS.get(req.get("cmd", ""))
            if fn is None:
                return {"ok": False, "error": f"unknown command {req.get('cmd')!r}"}
            return {"ok": True, **fn(req)}
    except SystemExit as exc:  # the pipeline reports user errors via sys.exit(msg)
        return {"ok": False, "error": str(exc.code) if exc.code not in (None, 0) else "aborted"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> None:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    req = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    resp = handle(req)
    sys.stdout.buffer.write(json.dumps(resp, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
