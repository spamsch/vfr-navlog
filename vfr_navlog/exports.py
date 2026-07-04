"""Non-PDF outputs: X-Plane FMS, ICAO FPL, and the interactive VOR-info prompt."""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

from .legs import _effective_leg_alt
from .model import Leg, Plan


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
    B, C, DIM, R = "\033[1m", "\033[36m", "\033[2m", "\033[0m"
    print(f"\n{B}{C}VOR-Informationen je Wegpunkt{R}")
    print(f"{DIM}Freitext pro Wegpunkt, z. B. \"233 FROM\" oder \"FRD R088\". "
          f"Enter lässt einen Punkt leer.{R}")
    for wp in plan.waypoints:
        label = f"{wp.ident}  {wp.name}".strip() or wp.ident or "(unbenannt)"
        current = f" [{wp.vor_info}]" if wp.vor_info else ""
        raw = input(f"  {label}{current}: ").strip()
        if raw:
            wp.vor_info = raw
