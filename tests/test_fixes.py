"""Unit tests for the automatic VOR radial-fix feature.

All the interesting logic is pure. The one file-touching function (load_vors)
is driven against a small earth_nav.dat fixture staged under a temp X-Plane root
— no network, no dependency on a real X-Plane install.
"""
from pathlib import Path

from vfr_navlog.fixes import (
    attach_vor_fixes,
    morse,
    navaids_in_plan,
    radial_from,
    select_fixes,
)
from vfr_navlog.model import Plan, VorStation, Waypoint
from vfr_navlog.xplane import load_vors

FIXTURES = Path(__file__).parent / "fixtures"


def _vor(lat, lon, var, ident="TST", freq="112.00", range_nm=100.0, has_dme=False):
    return VorStation(ident=ident, name=f"{ident} Test", freq=freq,
                      lat=lat, lon=lon, range_nm=range_nm,
                      slaved_var=var, has_dme=has_dme)


def _wp(lat, lon, ident="WP"):
    return Waypoint(name=ident, ident=ident, type="WAYPOINT", lat=lat, lon=lon)


# --- radial_from -----------------------------------------------------------

def test_radial_due_east_equator_variation_subtracts():
    # On the equator the initial bearing to a due-east point is exactly 090 true.
    station = _vor(0.0, 0.0, 0.0)
    assert radial_from(station, 0.0, 1.0) == 90
    # East (positive) slaved variation subtracts: 090 - 3 = 087.
    assert radial_from(_vor(0.0, 0.0, 3.0), 0.0, 1.0) == 87
    # West (negative) slaved variation adds: 090 - (-6) = 096.
    assert radial_from(_vor(0.0, 0.0, -6.0), 0.0, 1.0) == 96


def test_radial_wraparound_000():
    # Due-north point is bearing 000 true; a small east variation wraps to 359.
    assert radial_from(_vor(0.0, 0.0, 1.0), 1.0, 0.0) == 359
    # A negative (west) variation carries it the other way to 001.
    assert radial_from(_vor(0.0, 0.0, -1.0), 1.0, 0.0) == 1


# --- select_fixes ----------------------------------------------------------

COURSE_EAST = 90.0  # flying due east across the waypoint at the origin


def test_abeam_beats_dead_ahead():
    wp = _wp(0.0, 0.0)
    abeam = _vor(1.0, 0.0, 0.0, ident="ABM")     # due north → LOP 180, crosses at 90
    ahead = _vor(0.0, 1.0, 0.0, ident="AHD")     # dead ahead → LOP 270, crosses at 0
    fixes = select_fixes(wp, COURSE_EAST, [abeam, ahead])
    assert fixes[0].vor_ident == "ABM"           # abeam is the primary
    # The dead-ahead station has a useless crossing with the course but a fine
    # 90° intersection with the abeam radial, so it is a valid secondary.
    assert [f.vor_ident for f in fixes] == ["ABM", "AHD"]


def test_dead_ahead_alone_yields_no_primary():
    wp = _wp(0.0, 0.0)
    ahead = _vor(0.0, 1.0, 0.0, ident="AHD")     # crossing ~0° < 30°
    assert select_fixes(wp, COURSE_EAST, [ahead]) == []


def test_second_station_rejected_when_radials_too_shallow():
    wp = _wp(0.0, 0.0)
    s1 = _vor(1.0, 0.0, 0.0, ident="ST1")               # LOP 180
    # ~10° off in bearing-from-waypoint → its radial intersects ST1's at ~10°.
    import math
    s2 = _vor(math.cos(math.radians(10)), math.sin(math.radians(10)), 0.0, ident="ST2")
    fixes = select_fixes(wp, COURSE_EAST, [s1, s2])
    assert len(fixes) == 1
    assert fixes[0].vor_ident == "ST1"


def test_out_of_range_returns_empty():
    wp = _wp(0.0, 0.0)
    far = _vor(2.0, 0.0, 0.0, ident="FAR", range_nm=100.0)  # ~120 nm > 80 nm cap
    assert select_fixes(wp, COURSE_EAST, [far]) == []


def test_overhead_short_circuits():
    wp = _wp(0.0, 0.0)
    on_top = _vor(0.01, 0.0, 0.0, ident="TOP")   # ~0.6 nm away
    abeam = _vor(1.0, 0.0, 0.0, ident="ABM")
    fixes = select_fixes(wp, COURSE_EAST, [on_top, abeam])
    assert len(fixes) == 1
    assert fixes[0].vor_ident == "TOP"
    assert fixes[0].overhead is True


def test_dme_bonus_and_flag_propagate():
    wp = _wp(0.0, 0.0)
    plain = _vor(1.0, 0.0, 0.0, ident="PLN", freq="111.00")
    dme = _vor(1.0, 0.0, 0.0, ident="DME", freq="112.00", has_dme=True)
    # Same geometry; the DME bonus makes it the primary.
    fixes = select_fixes(wp, COURSE_EAST, [plain, dme])
    assert fixes[0].vor_ident == "DME"
    assert fixes[0].has_dme is True


# --- attach_vor_fixes / navaids_in_plan -----------------------------------

def test_attach_skips_departure_includes_destination():
    dep = _wp(0.0, -1.0, ident="DEP")
    mid = _wp(0.0, 0.0, ident="MID")
    dest = _wp(0.0, 1.0, ident="DST")
    plan = Plan(waypoints=[dep, mid, dest], cruise_alt_ft=3500,
                flightplan_type="VFR", cycle="2607", created="")
    abeam = _vor(1.0, 0.0, 0.0, ident="ABM")
    attach_vor_fixes(plan, [abeam])
    assert dep.fixes == []            # departure skipped
    assert mid.fixes                  # interior waypoint gets a fix
    # navaids collects one entry per distinct station
    assert [n.vor_ident for n in navaids_in_plan(plan)] == ["ABM"]


def test_attach_noop_without_stations():
    wp = _wp(0.0, 0.0)
    plan = Plan(waypoints=[_wp(0.0, -1.0), wp], cruise_alt_ft=3500,
                flightplan_type="VFR", cycle="2607", created="")
    attach_vor_fixes(plan, [])
    assert wp.fixes == []


# --- load_vors -------------------------------------------------------------

def test_load_vors_from_fixture(tmp_path):
    # Stage the fixture under a temp X-Plane root at the fallback nav path.
    nav_dir = tmp_path / "Resources" / "default data"
    nav_dir.mkdir(parents=True)
    (nav_dir / "earth_nav.dat").write_text(
        (FIXTURES / "mini_nav.dat").read_text(), encoding="utf-8")

    vors = load_vors(tmp_path)
    by_ident = {v.ident: v for v in vors}

    # VOR, VOR-DME, VORTAC kept; standalone DME, NDB, malformed line, and pure
    # TACAN dropped — civil receivers get no azimuth from a TACAN.
    assert set(by_ident) == {"HLZ", "DLE", "NVO"}
    assert "OSB" not in by_ident

    hlz = by_ident["HLZ"]
    assert hlz.freq == "116.30"
    assert hlz.range_nm == 130.0
    assert hlz.slaved_var == 3.0
    assert hlz.has_dme is False
    assert "Hehlingen" in hlz.name

    dle = by_ident["DLE"]
    assert dle.freq == "115.20"
    assert dle.slaved_var == -2.0
    assert dle.has_dme is True     # matching type-12 DME row


def test_load_vors_missing_path_returns_empty(tmp_path):
    assert load_vors(tmp_path / "nope") == []


# --- morse -----------------------------------------------------------------

def test_morse_ident():
    assert morse("HLZ") == ".... .-.. --.."
    assert morse("DLE") == "-.. .-.. ."
    assert morse("A1") == ".- .----"
