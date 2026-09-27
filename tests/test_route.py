import shutil
from pathlib import Path

import pytest

from vfr_navlog.cli import _build_parser, _runconfig_from_cli
from vfr_navlog.config import APT_REL, NAV_REL
from vfr_navlog.navigraph import plan_from_route

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def xplane(tmp_path: Path) -> Path:
    """A minimal X-Plane tree holding the mini apt.dat / earth_nav.dat fixtures."""
    for src, rel in (("mini_apt.dat", APT_REL), ("mini_nav.dat", NAV_REL)):
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / src, dest)
    return tmp_path


def test_route_resolves_airports_navaids_and_coordinates(xplane):
    plan = plan_from_route("eddv dct N0110A035 520000N0090000E DLE dct edli-rw11", xplane, cruise_alt_ft=3500)

    assert [w.ident for w in plan.waypoints] == ["EDDV", "520000N0090000E", "DLE", "EDLI"]
    assert [w.type for w in plan.waypoints] == ["AIRPORT", "USER", "VOR", "AIRPORT"]
    assert plan.waypoints[1].lat == pytest.approx(52.0)
    assert plan.waypoints[2].lat == pytest.approx(51.8)
    assert plan.cruise_alt_ft == 3500
    assert plan.flightplan_type == "VFR"
    assert plan.cycle == "Route"


def test_route_without_xplane_needs_nav_data():
    with pytest.raises(SystemExit):
        plan_from_route("EDDV DLE EDLI", None)


def test_route_flag_reaches_runconfig():
    args = _build_parser().parse_args(["--route", "EDDV DLE EDLI", "--aircraft", "aircraft_c172.json"])
    config = _runconfig_from_cli(args)
    assert config.route == "EDDV DLE EDLI"
    assert config.plan_path is None and not config.navigraph


def test_route_is_exclusive_with_plan():
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--route", "EDDV EDLI", "--plan", "x.lnmpln", "--aircraft", "a.json"])
