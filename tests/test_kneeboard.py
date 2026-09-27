"""The in-sim kneeboard: Lua data export, and the FlyWithLua script itself run
under LuaJIT (lupa) with the FlyWithLua / ImGui API stubbed."""
from pathlib import Path

import pytest

from vfr_navlog.kneeboard import SCRIPT_SRC, ascii_text, send, to_lua

lupa = pytest.importorskip("lupa.luajit21")


def _plan(route_key: str = "A B C", sent_at: int = 1) -> dict:
    nav = {
        "ident": "HMM", "freq": "115.65", "freq_10khz": 11565, "obs": 172, "flag": "TO",
        "trackable": True, "radial_label": "inbound R352", "max_dev": 1.4, "r_start": 350, "r_end": 353,
        "lat": 51.86, "lon": 7.71, "var": 1.0, "dme": True, "suggested": False,
    }
    return {
        "sent_at": sent_at, "route_key": route_key, "title": "A -> C (SR22)", "call_leg": 2,
        "waypoints": [
            {"ident": "A", "lat": 52.0, "lon": 7.7, "notes": "", "fixes": []},
            {"ident": "B", "lat": 51.9, "lon": 7.7, "notes": "Kirchturm rechts, Köln → Süd", "fixes": ["HMM 115.65 R352"]},
            {"ident": "C", "lat": 51.8, "lon": 7.7, "notes": "", "fixes": []},
        ],
        "legs": [
            {"from": "A", "to": "B", "from_idx": 1, "mh": 180, "alt": 3500, "gs": 120, "dist": 6.0, "ete_min": 3.0,
             "nav": nav, "checkpoints": [{"label": "Autobahn A1 (L 0.5 NM)", "min": 1.5}]},
            {"from": "B", "to": "C", "from_idx": 2, "mh": 180, "alt": 4500, "gs": 120, "dist": 6.0, "ete_min": 3.0,
             "checkpoints": []},
        ],
        "dest": {"ident": "C", "name": "Ceeport", "elev": 300, "freqs": [{"label": "Tower", "freq": "124.980", "live": True}],
                 "ils": ["ILS 14L IKOE 110.90"], "runways": ["14L/32R"], "atis": [], "metar": "C 271450Z CAVOK Q1015",
                 "as_of": "15:32Z"},
    }


def test_ascii_text():
    assert ascii_text("Köln → Süd · 5°") == "Koeln -> Sued - 5 deg"
    assert ascii_text("Café “x”") == 'Cafe "x"'


def test_to_lua_literals():
    assert to_lua({"a": 1, "b": [True, None, "x\"y"], "c": None}) == '{\n  a = 1,\n  b = {\n    true,\n    nil,\n    "x\\"y"\n  }\n}'
    with pytest.raises(ValueError):
        to_lua({"bad key": 1})


class FakeSim:
    """FlyWithLua globals + a recording ImGui, driven from Python."""

    def __init__(self, root: Path):
        self.lua = lupa.LuaRuntime(unpack_returned_tuples=True)
        self.texts: list[str] = []
        self.images: list[tuple[float, float]] = []  # (w, h) of every imgui.Image call
        self.children = 0  # open BeginChild calls (must be back to 0 after a draw)
        self.mouse_down = False
        self.mouse = (0.0, 0.0)
        self.scroll = [0.0, 0.0]
        self.buttons: list[str] = []
        self.click: set[str] = set()
        g = self.lua.globals()
        g.SYSTEM_DIRECTORY = str(root).replace("\\", "/") + "/"
        # The pilot's setup: 5120x1440 monitor at 150% UI scaling = 3413x960 boxels.
        g.SCREEN_WIDTH, g.SCREEN_HEIGHT = 5120, 1440
        self.lua.execute("""
            vfrkb_flight_time, vfrkb_zulu = 100.0, 50000.0
            vfrkb_nav1_hz, vfrkb_nav1_obs, vfrkb_nav2_hz, vfrkb_nav2_obs = 0, 0, 0, 0
            vfrkb_nav1_flag, vfrkb_nav1_dme = 0, 0
            function dataref(name, path, mode, idx) end
            function do_often(s) end
            function do_every_frame(s) end
            function add_macro(a, b) end
            function create_command(a, b, c, d, e) end
            function logMsg(s) end
            in_build = false  -- true while a window's ImGui builder runs
            function float_wnd_create()
                -- In X-Plane this crashes: every window has its own ImGui context.
                if in_build then error("window created during a draw") end
                return {}
            end
            function float_wnd_set_title() end
            wnd_pos, wnd_geom = nil, nil
            function float_wnd_set_position(w, left, bottom) wnd_pos = { left = left, bottom = bottom } end
            function float_wnd_set_geometry(w, l, t, r, b) wnd_geom = { l = l, t = t, r = r, b = b } end
            function float_wnd_get_geometry(w) return wnd_geom.l, wnd_geom.t, wnd_geom.r, wnd_geom.b end
            -- The pilot's monitors in X-Plane UI units (from Log.txt, 150% scaling).
            function XPLMGetAllMonitorBoundsGlobal()
                return { { inLeft = 0, inTop = 960, inRight = 3413, inBottom = 0 },
                         { inLeft = 3413, inTop = 512, inRight = 4096, inBottom = 0 } }
            end
            function float_wnd_set_imgui_builder() end
            function float_wnd_set_onclose() end
            function float_wnd_destroy()
                if in_build then error("window destroyed during a draw") end
            end
            loaded_images = {}
            function float_wnd_load_image(path) loaded_images[#loaded_images + 1] = path; return #loaded_images end
        """)
        imgui = self.lua.table_from({
            "TextUnformatted": lambda s: self.texts.append(s),
            "Button": lambda label, w=0, h=0: (self.buttons.append(label), label in self.click)[1],
            "SameLine": lambda *a: None, "Separator": lambda *a: None, "Spacing": lambda *a: None,
            "PushStyleColor": lambda *a: None, "PopStyleColor": lambda *a: None,
            "SetWindowFontScale": lambda *a: None,
            "Image": lambda img, w, h, *a: self.images.append((w, h)),
            "BeginChild": lambda *a: self._child(1), "EndChild": lambda *a: self._child(-1),
            "GetWindowWidth": lambda: 740.0, "GetWindowHeight": lambda: 800.0,
            "GetCursorPosX": lambda: 8.0, "GetCursorPosY": lambda: 80.0, "SetCursorPos": lambda x, y: None,
            # Mouse and scrolling of the chart view: driven from the tests.
            "InvisibleButton": lambda label, w, h: False,
            "IsItemActive": lambda: self.mouse_down,
            "GetMousePos": lambda: self.mouse,
            "GetScrollX": lambda: self.scroll[0], "GetScrollY": lambda: self.scroll[1],
            "SetScrollX": lambda v: self._set_scroll(0, v), "SetScrollY": lambda v: self._set_scroll(1, v),
        })
        imgui.constant = self.lua.table_from({"Col": self.lua.table_from({"Text": 0})})
        g.imgui = imgui
        self.lua.execute(SCRIPT_SRC.read_text(encoding="utf-8"))

    def draw(self, click: tuple[str, ...] = ()) -> str:
        self.texts.clear()
        self.buttons.clear()
        self.click = set(click)
        return self._build("vfrkb_build")

    def _child(self, d: int):
        self.children += d

    def _set_scroll(self, axis: int, v: float):
        self.scroll[axis] = max(0.0, v)

    def draw_charts(self, click: tuple[str, ...] = ()) -> str:
        self.texts.clear()
        self.buttons.clear()
        self.click = set(click)
        text = self._build("vfrkb_chart_build")
        assert self.children == 0, "BeginChild/EndChild unbalanced"
        return text

    def _build(self, builder: str) -> str:
        """Run a window builder the way X-Plane does; errors the script swallowed fail the test."""
        g = self.lua.globals()
        g.in_build = True
        try:
            g[builder](None, 0, 0)
        finally:
            g.in_build = False
        text = "\n".join(self.texts)
        assert "draw error" not in text, text
        return text

    def set_time(self, t: float):
        self.lua.globals().vfrkb_flight_time = t


def test_script_without_plan_asks_to_send(tmp_path):
    (tmp_path / "Resources" / "plugins").mkdir(parents=True)
    sim = FakeSim(tmp_path)
    assert "Send to X-Plane" in sim.draw()


def test_script_flies_a_plan(tmp_path):
    (tmp_path / "Resources" / "plugins" / "FlyWithLua" / "Scripts").mkdir(parents=True)
    info = send(_plan(), tmp_path)
    assert info["script_installed"] and Path(info["plan_path"]).exists()
    assert send(_plan(sent_at=2), tmp_path)["script_installed"] is False  # unchanged script: no reinstall

    sim = FakeSim(tmp_path)
    text = sim.draw()
    assert "Ready  A -> B" in text and "Start: over A now" in sim.buttons
    assert "OBS 172 TO" in text

    sim.draw(click=("Start: over A now",))
    sim.set_time(160.0)  # 60 s into leg 1
    text = sim.draw()
    assert "A -> B   (leg 1/2)" in text
    assert "to B 2:00" in text  # 3 min ETE − 60 s
    assert "At B: climb to 4500 ft" in text
    assert "Autobahn A1 (L 0.5 NM)   in 0:30" in text
    assert "Kirchturm rechts, Koeln -> Sued" in text  # notes, transliterated
    assert "expect now R" in text

    sim.draw(click=("Tune NAV1",))
    g = sim.lua.globals()
    assert g.vfrkb_nav1_hz == 11565 and g.vfrkb_nav1_obs == 172

    sim.draw(click=("Over B now",))  # leg 1 flown in 60 s instead of 180 s
    sim.set_time(170.0)
    text = sim.draw()
    assert "B -> C   (leg 2/2)" in text
    assert "ILS 14L IKOE 110.90" in text  # destination opens by itself on the call leg

    # Re-sending the same route (e.g. edited notes) keeps the flight log; a new route resets it.
    send(_plan(sent_at=3), tmp_path)
    g.vfrkb_poll()
    assert "B -> C   (leg 2/2)" in sim.draw()
    send(_plan(route_key="X Y", sent_at=4), tmp_path)
    g.vfrkb_poll()
    assert "Ready  A -> B" in sim.draw()


def test_flight_log_survives_script_reload(tmp_path):
    (tmp_path / "Resources" / "plugins" / "FlyWithLua" / "Scripts").mkdir(parents=True)
    send(_plan(), tmp_path)
    sim = FakeSim(tmp_path)
    sim.draw(click=("Start: over A now",))
    sim2 = FakeSim(tmp_path)  # "Reload all Lua script files"
    sim2.set_time(130.0)
    assert "A -> B   (leg 1/2)" in sim2.draw()


def test_window_opens_on_screen_with_ui_scaling(tmp_path):
    """Window positions are in UI units, not pixels: at 150% scaling the 5120x1440
    main monitor is 3413x960 units. The whole window must land inside it."""
    (tmp_path / "Resources" / "plugins").mkdir(parents=True)
    sim = FakeSim(tmp_path)
    g = sim.lua.globals()
    g.vfrkb_toggle()
    geom = g.wnd_geom
    assert 0 <= geom.l < geom.r <= 3413
    assert 0 <= geom.b < geom.t <= 960
    assert geom.t - geom.b >= 500  # still tall enough to be useful

    # Dragged elsewhere, closed and reopened: comes back where the pilot left it.
    g.wnd_geom = sim.lua.table_from({"l": 1000, "t": 900, "r": 1470, "b": 260})
    g.vfrkb_toggle()
    g.vfrkb_toggle()
    assert (g.wnd_geom.l, g.wnd_geom.t) == (1000, 900)
    g.vfrkb_reset_position()
    assert g.wnd_geom.l == 40


def test_window_fallback_without_monitor_bounds(tmp_path):
    (tmp_path / "Resources" / "plugins").mkdir(parents=True)
    sim = FakeSim(tmp_path)
    g = sim.lua.globals()
    g.XPLMGetAllMonitorBoundsGlobal = None
    g.vfrkb_toggle()
    assert g.wnd_pos.bottom == 40  # set_position takes the bottom edge
