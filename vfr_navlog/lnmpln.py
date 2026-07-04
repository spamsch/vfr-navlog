"""Little Navmap .lnmpln parsing plus wind / magvar argument parsing."""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import Plan, Waypoint


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
