import pytest

from vfr_navlog.lnmpln import parse_lnmpln, parse_magvar, parse_wind


def test_parse_lnmpln(sample_plan_path):
    plan = parse_lnmpln(sample_plan_path)
    assert len(plan.waypoints) == 3
    assert plan.cruise_alt_ft == 3500
    assert plan.flightplan_type == "VFR"
    assert plan.cycle == "2607"
    dep, mid, dest = plan.waypoints
    assert dep.ident == "EDDV"
    assert dep.type == "AIRPORT"
    assert dep.lat == pytest.approx(52.461140)
    assert dep.lon == pytest.approx(9.685077)
    assert mid.ident == "NIE"
    assert dest.ident == "EDLI"
    assert dest.type == "AIRPORT"


def test_parse_wind():
    assert parse_wind("270/15") == (270.0, 15.0)
    assert parse_wind(" 0/0 ") == (0.0, 0.0)
    assert parse_wind("360/8") == (0.0, 8.0)


def test_parse_magvar_east_west():
    assert parse_magvar("2.5E") == 2.5
    assert parse_magvar("2.5W") == -2.5
    assert parse_magvar("4E") == 4.0
    assert parse_magvar("-2.5") == -2.5
    assert parse_magvar("3") == 3.0
