"""Automatic VOR radial cross-checks per waypoint.

A radial is the magnetic bearing FROM a station. Tune the frequency, set the OBS
to the printed radial, and when the CDI centers you are on that line of position.
Two radials from stations that cross at a healthy angle turn the line into a
point. This module computes those radials from X-Plane nav data.

Everything here is pure except nothing — the file scan lives in xplane.load_vors.
The geometry heuristics (crossing angle, range, DME bonus) live only in
select_fixes, so there is one place to argue with.
"""
from __future__ import annotations

from dataclasses import dataclass

from .geo import great_circle, haversine_nm
from .model import Plan, RadialFix, VorStation, Waypoint

# Domain thresholds — see VOR-FIXES-PLAN.md "Domain rules".
OVERHEAD_NM = 2.0     # within this, the fix is station passage, not a radial
MAX_RANGE_NM = 80.0   # hard cap on top of each station's published range
MIN_CROSS_DEG = 30.0  # primary radial must cross the course at least this steeply
MIN_INTERSECT_DEG = 30.0  # the two radials must intersect at least this steeply


def radial_from(station: VorStation, lat: float, lon: float) -> int:
    """Magnetic radial FROM `station` to (lat, lon), 0–359, whole degrees.

    True bearing from the station, minus the station's slaved variation (East
    positive: magnetic = true - variation). This is the number you dial into the
    OBS, so it is rounded to whole degrees and printed three digits elsewhere.
    """
    true_brg, _ = great_circle(station.lat, station.lon, lat, lon)
    mag = (true_brg - station.slaved_var) % 360.0
    return int(round(mag)) % 360


def _acute_angle(a: float, b: float) -> float:
    """Acute angle (0–90°) between two undirected lines of bearing a and b."""
    d = abs(a - b) % 360.0
    if d > 180.0:
        d = 360.0 - d
    if d > 90.0:
        d = 180.0 - d
    return d


@dataclass
class _Candidate:
    station: VorStation
    dist_nm: float
    radial: int
    lop_true: float   # true bearing of the line of position (station → waypoint)
    crossing: float   # acute angle between the LOP and the course
    score: float


def _to_fix(c: _Candidate) -> RadialFix:
    return RadialFix(
        vor_ident=c.station.ident,
        vor_name=c.station.name,
        freq=c.station.freq,
        radial=c.radial,
        dist_nm=c.dist_nm,
        has_dme=c.station.has_dme,
    )


def select_fixes(
    wp: Waypoint,
    inbound_course: float,
    stations: list[VorStation],
    max_fixes: int = 2,
) -> list[RadialFix]:
    """Pick up to `max_fixes` VOR cross-checks for one waypoint.

    inbound_course is the true course of the leg arriving at the waypoint.
    Rules: a station within OVERHEAD_NM short-circuits to a single "overhead"
    fix. Otherwise the primary is the highest-scoring in-range station whose
    radial crosses the course at >= MIN_CROSS_DEG; the secondary is the best
    remaining station whose radial intersects the primary's at
    >= MIN_INTERSECT_DEG. Score rewards a near-90° crossing and a near station,
    with a small bonus for DME (a distance check on the same frequency).
    """
    overhead: _Candidate | None = None
    scored: list[_Candidate] = []

    for s in stations:
        dist = haversine_nm(s.lat, s.lon, wp.lat, wp.lon)
        radial = radial_from(s, wp.lat, wp.lon)
        if dist <= OVERHEAD_NM:
            if overhead is None or dist < overhead.dist_nm:
                overhead = _Candidate(s, dist, radial, 0.0, 0.0, 0.0)
            continue
        if dist > min(s.range_nm, MAX_RANGE_NM):
            continue
        lop_true, _ = great_circle(s.lat, s.lon, wp.lat, wp.lon)
        crossing = _acute_angle(lop_true, inbound_course)
        # Higher crossing is better; closer is better (1° CDI ≈ dist/60 nm of
        # lateral error). DME adds a same-frequency distance check.
        score = crossing - dist / 4.0 + (5.0 if s.has_dme else 0.0)
        scored.append(_Candidate(s, dist, radial, lop_true, crossing, score))

    if overhead is not None:
        fix = _to_fix(overhead)
        fix.overhead = True
        return [fix]

    primary_pool = [c for c in scored if c.crossing >= MIN_CROSS_DEG]
    if not primary_pool:
        return []
    primary = max(primary_pool, key=lambda c: c.score)
    fixes = [_to_fix(primary)]
    if max_fixes < 2:
        return fixes

    secondary_pool = [
        c for c in scored
        if c.station.ident != primary.station.ident
        and _acute_angle(c.lop_true, primary.lop_true) >= MIN_INTERSECT_DEG
    ]
    if secondary_pool:
        fixes.append(_to_fix(max(secondary_pool, key=lambda c: c.score)))
    return fixes


def attach_vor_fixes(plan: Plan, stations: list[VorStation]) -> None:
    """Fill wp.fixes for every waypoint except the departure.

    The departure is skipped (you know where you are on the ground); the
    destination is kept (a radial confirming field identification is useful).
    Each waypoint is scored against its inbound leg's true course.
    """
    if not stations:
        return
    wps = plan.waypoints
    for i in range(1, len(wps)):
        prev, wp = wps[i - 1], wps[i]
        course, _ = great_circle(prev.lat, prev.lon, wp.lat, wp.lon)
        wp.fixes = select_fixes(wp, course, stations)


def navaids_in_plan(plan: Plan) -> list[RadialFix]:
    """One RadialFix per distinct station used anywhere in the plan, in first-use
    order. Feeds the navaid reference (Morse) block."""
    seen: dict[str, RadialFix] = {}
    for wp in plan.waypoints:
        for fx in wp.fixes:
            seen.setdefault(fx.vor_ident, fx)
    return list(seen.values())


# --- Morse reference -------------------------------------------------------
# You identify a VOR by its Morse before trusting it; the dot-dash pattern on
# the kneeboard closes that loop.
MORSE: dict[str, str] = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".",
    "F": "..-.", "G": "--.", "H": "....", "I": "..", "J": ".---",
    "K": "-.-", "L": ".-..", "M": "--", "N": "-.", "O": "---",
    "P": ".--.", "Q": "--.-", "R": ".-.", "S": "...", "T": "-",
    "U": "..-", "V": "...-", "W": ".--", "X": "-..-", "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
}


def morse(ident: str) -> str:
    """Morse for an ident, letters space-separated: 'HLZ' -> '.... .-.. --..'.
    Unknown characters become '?'."""
    return " ".join(MORSE.get(ch.upper(), "?") for ch in ident if not ch.isspace())
