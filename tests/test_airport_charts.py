"""Airport chart cache (DFS stubbed — no network) and the kneeboard's chart window."""
import io
import json
from datetime import date, timedelta

import pytest
from PIL import Image

from vfr_navlog import airport_charts, dfs_charts
from vfr_navlog.kneeboard import copy_charts


def _png(w=40, h=30) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), "white").save(buf, "PNG")
    return buf.getvalue()


class FakeDfs:
    """Stands in for aip.dfs.de: two VFR pages and one aerodrome chart."""

    def __init__(self, monkeypatch, issue="2026SEP17"):
        self.issue = issue
        self.downloads = 0
        self.offline = False
        monkeypatch.setattr(dfs_charts, "find_chapter_url", self.find_chapter_url)
        monkeypatch.setattr(dfs_charts, "list_charts", self.list_charts)
        monkeypatch.setattr(dfs_charts, "extract_png", self.extract_png)

    def find_chapter_url(self, icao, section):
        if self.offline:
            raise ConnectionError("offline")
        book = "BasicVFR" if section == "vfr" else "BasicIFR"
        return f"https://aip.dfs.de/{book}/{self.issue}/chapter/{icao}{section}.html"

    def list_charts(self, url, vfr_filter=False, skip_junk=False):
        if "BasicVFR" in url:
            return [("EDDK Koeln Bonn 1", "p1"), ("EDDK Koeln Bonn 2", "p2")]
        return [("AD 2 EDDK 2-5 Aerodrome Chart - ICAO", "p3")]

    def extract_png(self, page_url):
        self.downloads += 1
        return _png(60 if page_url == "p1" else 40, 30)


def test_download_then_cache(tmp_path, monkeypatch):
    dfs = FakeDfs(monkeypatch)
    got = airport_charts.fetch("eddk", cache_root=tmp_path)
    assert [p["title"] for p in got["pages"]] == ["EDDK Koeln Bonn 1", "EDDK Koeln Bonn 2", "AD 2 EDDK 2-5 Aerodrome Chart - ICAO"]
    assert got["pages"][0]["width"] == 60 and got["pages"][0]["file"].startswith("vfr_2026SEP17_")
    assert dfs.downloads == 3

    # Within a week: straight from the cache, no DFS request at all.
    dfs.offline = True
    assert len(airport_charts.fetch("EDDK", cache_root=tmp_path)["pages"]) == 3
    assert dfs.downloads == 3


def _age(tmp_path, days):
    idx = tmp_path / "EDDK" / "index.json"
    data = json.loads(idx.read_text())
    data["fetched"] = (date.today() - timedelta(days=days)).isoformat()
    idx.write_text(json.dumps(data))


def test_refresh_only_when_aip_issue_changes(tmp_path, monkeypatch):
    dfs = FakeDfs(monkeypatch)
    airport_charts.fetch("EDDK", cache_root=tmp_path)
    _age(tmp_path, 10)
    airport_charts.fetch("EDDK", cache_root=tmp_path)  # same chapters: no new download
    assert dfs.downloads == 3

    _age(tmp_path, 10)
    dfs.issue = "2026OCT15"  # new AIP issue
    got = airport_charts.fetch("EDDK", cache_root=tmp_path)
    assert dfs.downloads == 6
    files = sorted(f.name for f in (tmp_path / "EDDK").glob("*.png"))
    assert all("2026OCT15" in f for f in files)
    assert len(files) == 3 and all(p["file"] in files for p in got["pages"])


def test_offline_falls_back_to_cache(tmp_path, monkeypatch):
    dfs = FakeDfs(monkeypatch)
    airport_charts.fetch("EDDK", cache_root=tmp_path)
    _age(tmp_path, 10)
    dfs.offline = True
    got = airport_charts.fetch("EDDK", cache_root=tmp_path)
    assert len(got["pages"]) == 3 and "cached" in got["error"]
    assert airport_charts.fetch("EDLI", cache_root=tmp_path)["pages"] == []  # never fetched, offline


def test_non_german_airport():
    got = airport_charts.fetch("LOWS")
    assert got["pages"] == [] and "German" in got["error"]


def test_copy_charts_into_xplane(tmp_path, monkeypatch):
    FakeDfs(monkeypatch)
    cache = tmp_path / "cache"
    airport_charts.fetch("EDDK", cache_root=cache)
    xp = tmp_path / "xp"
    sets = copy_charts(["EDDG", "EDDK"], xp, cache_root=cache)  # EDDG not cached: skipped
    assert [s["icao"] for s in sets] == ["EDDK"]
    first = sets[0]["pages"][0]
    path = xp / "Output" / "vfr-navlog" / first["file"]
    assert first["file"].startswith("charts/EDDK/") and first["file"].endswith(".jpg") and path.exists()
    with Image.open(path) as im:
        assert im.format == "JPEG" and im.mode == "RGB"


def test_large_charts_are_scaled_for_xplane(tmp_path, monkeypatch):
    FakeDfs(monkeypatch)
    monkeypatch.setattr(dfs_charts, "extract_png", lambda page_url: _png(2983, 2286))  # EDDK's biggest page
    cache = tmp_path / "cache"
    airport_charts.fetch("EDDK", cache_root=cache)
    page = copy_charts(["EDDK"], tmp_path / "xp", cache_root=cache)[0]["pages"][0]
    assert max(page["width"], page["height"]) == 2048
    assert page["width"] / page["height"] == pytest.approx(2983 / 2286, rel=0.01)


# --- In-sim chart window -------------------------------------------------------

lupa = pytest.importorskip("lupa.luajit21")


def test_chart_window(tmp_path, monkeypatch):
    from test_kneeboard import FakeSim, _plan  # same stubs as the kneeboard tests

    from vfr_navlog.kneeboard import send

    FakeDfs(monkeypatch)
    cache = tmp_path / "cache"
    monkeypatch.setattr(airport_charts, "CACHE_ROOT", cache)
    airport_charts.fetch("EDDK", cache_root=cache)
    (tmp_path / "Resources" / "plugins" / "FlyWithLua" / "Scripts").mkdir(parents=True)

    plan = _plan()
    plan["waypoints"][-1]["ident"] = "EDDK"
    plan["legs"][-1]["to"] = "EDDK"
    plan["chart_icaos"] = ["A", "EDDK"]  # departure "A" has no charts
    monkeypatch.setattr("vfr_navlog.kneeboard.copy_charts",
                        lambda icaos, root: copy_charts(icaos, root, cache_root=cache))
    send(plan, tmp_path)

    sim = FakeSim(tmp_path)
    g = sim.lua.globals()
    # The kneeboard's "Charts" button only queues the window (creating a window
    # mid-draw crashes X-Plane); the frame callback opens it.
    sim.draw(click=("Charts",))
    g.vfrkb_frame()
    text = sim.draw_charts()
    assert "Loading chart..." in text  # images load in the poll, never while drawing
    g.vfrkb_tick()
    text = sim.draw_charts()
    assert "EDDK Koeln Bonn 1" in text and " 1/3 " in text
    w, h = sim.images[-1]
    assert w == pytest.approx(740 - 20) and h == pytest.approx(w * 30 / 60)  # fit to window width, aspect kept

    g.vfrkb_chart_page(1)
    g.vfrkb_tick()
    assert "EDDK Koeln Bonn 2" in sim.draw_charts()
    g.vfrkb_chart_page(-2)  # wraps around
    g.vfrkb_tick()
    assert "Aerodrome Chart" in sim.draw_charts()

    sim.draw_charts(click=("+",))
    g.vfrkb_tick()
    sim.draw_charts()
    assert sim.images[-1][0] > sim.images[0][0]  # zoomed in: drawn wider


def test_chart_pan_and_zoom(tmp_path, monkeypatch):
    from test_kneeboard import FakeSim, _plan

    from vfr_navlog.kneeboard import send

    FakeDfs(monkeypatch)
    cache = tmp_path / "cache"
    airport_charts.fetch("EDDK", cache_root=cache)
    (tmp_path / "Resources" / "plugins" / "FlyWithLua" / "Scripts").mkdir(parents=True)
    plan = _plan()
    plan["chart_icaos"] = ["EDDK"]
    monkeypatch.setattr("vfr_navlog.kneeboard.copy_charts",
                        lambda icaos, root: copy_charts(icaos, root, cache_root=cache))
    send(plan, tmp_path)

    sim = FakeSim(tmp_path)
    g = sim.lua.globals()
    g.vfrkb_charts_toggle()
    sim.draw_charts()
    g.vfrkb_tick()
    sim.draw_charts()

    # Zoom 2 steps (1x -> 2x): the centre of the view stays in place.
    sim.draw_charts(click=("+",))
    sim.draw_charts()
    sim.draw_charts(click=("+",))
    sim.draw_charts()
    view_w, view_h = 740.0, 800.0
    # The view centre (370, 400) at 1x is at (740, 800) at 2x.
    assert sim.scroll[0] + view_w / 2 == pytest.approx(view_w / 2 * 2)
    assert sim.scroll[1] + view_h / 2 == pytest.approx(view_h / 2 * 2)

    # Drag: press, move the mouse 100 left / 50 up, release — the chart follows the mouse.
    before = list(sim.scroll)
    sim.mouse_down, sim.mouse = True, (500.0, 400.0)
    sim.draw_charts()
    sim.mouse = (400.0, 350.0)
    sim.draw_charts()
    assert sim.scroll == [before[0] + 100, before[1] + 50]
    sim.mouse_down = False
    sim.draw_charts()
    sim.mouse = (0.0, 0.0)  # moving without the button held does nothing
    sim.draw_charts()
    assert sim.scroll == [before[0] + 100, before[1] + 50]

    # A new page starts at its top-left corner.
    g.vfrkb_chart_page(1)
    g.vfrkb_tick()
    sim.draw_charts()
    assert sim.scroll == [0.0, 0.0]
