"""Leg computation, the effective-altitude walk, and the VFR hemispheric rule."""
from __future__ import annotations

import math

from .geo import apply_wind, great_circle
from .model import Leg, Plan


def find_call_marker(legs: list[Leg], threshold_nm: float) -> int | None:
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


def _effective_leg_alt(plan: Plan, wp_idx: int) -> float:
    """Cruising altitude for the leg departing from plan.waypoints[wp_idx].

    Walks the alt_profile in route order and returns the last altitude change
    at or before wp_idx. Falls back to plan.cruise_alt_ft if no profile entry
    has been reached yet.
    """
    if not plan.alt_profile:
        return plan.cruise_alt_ft
    alt = plan.cruise_alt_ft
    for j in range(wp_idx + 1):
        ident = plan.waypoints[j].ident.upper()
        for pid, palt in plan.alt_profile:
            if pid.upper() == ident:
                alt = palt
    return alt


def hemispheric_alt(alt_ft: float, mh: float) -> float:
    """Return the nearest compliant VFR hemispheric cruising altitude >= alt_ft.

    ICAO/SERA semi-circular rule:
      MH 000–179 (eastbound): 1500, 3500, 5500, 7500 … ft
      MH 180–359 (westbound): 2500, 4500, 6500, 8500 … ft

    Altitudes below 1500 ft are returned unchanged (below the rule's floor).
    """
    if alt_ft < 1500:
        return alt_ft
    if mh < 180:
        base = 1500.0
    else:
        base = 2500.0
    n = math.ceil((alt_ft - base) / 2000.0)
    return base + max(0, n) * 2000.0


def apply_hemispheric_rule(plan: Plan, legs: list[Leg]) -> None:
    """Adjust plan altitudes so every leg complies with the VFR hemispheric rule.

    Checks each leg after all user-entered altitudes are final. Prints a notice
    for every leg that required adjustment. Updates plan.cruise_alt_ft and
    plan.alt_profile in-place; the new profile is a minimal encoding of the
    per-leg corrected altitudes.
    """
    if not legs:
        return

    old_alts = [_effective_leg_alt(plan, i) for i in range(len(legs))]
    new_alts = [hemispheric_alt(old_alts[i], legs[i].mh) for i in range(len(legs))]

    changed = [(i, old_alts[i], new_alts[i]) for i in range(len(legs)) if new_alts[i] != old_alts[i]]
    if not changed:
        return

    print("[hemispheric] Adjusted altitudes to comply with the VFR semi-circular rule:")
    for i, old, new in changed:
        dep = plan.waypoints[i].ident
        arr = plan.waypoints[i + 1].ident
        mh = legs[i].mh
        direction = "E" if mh < 180 else "W"
        print(f"  {dep}→{arr}  MH {mh:.0f}° ({direction}):  {int(old):,} ft → {int(new):,} ft")

    # Rebuild alt_profile from the corrected per-leg altitudes.
    # cruise_alt_ft becomes the corrected altitude of the first leg.
    plan.cruise_alt_ft = new_alts[0]
    new_profile: list[tuple[str, float]] = []
    prev = new_alts[0]
    for i in range(1, len(legs)):
        if new_alts[i] != prev:
            new_profile.append((plan.waypoints[i].ident, new_alts[i]))
            prev = new_alts[i]
    plan.alt_profile = new_profile
