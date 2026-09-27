"""Hand a plan to the in-sim kneeboard (xplane/vfr_kneeboard.lua, FlyWithLua).

The app sends the kneeboard data as JSON; this module writes it as a Lua table
literal to ``<X-Plane>/Output/vfr-navlog/kneeboard_plan.lua`` (the script loads
it in an empty environment, so it is data only) and installs or updates the
script in FlyWithLua's Scripts folder.

Strings are transliterated to ASCII: FlyWithLua's ImGui font has no umlauts,
arrows or degree signs.
"""
from __future__ import annotations

import math
import unicodedata
from pathlib import Path

from . import airport_charts
from .config import PROJECT_ROOT

SCRIPT_SRC = PROJECT_ROOT / "xplane" / "vfr_kneeboard.lua"
PLAN_REL = Path("Output") / "vfr-navlog" / "kneeboard_plan.lua"
CHARTS_REL = Path("Output") / "vfr-navlog" / "charts"
# Chart pictures for X-Plane: JPEG like FlyWithLua's own image demo, and no edge
# longer than this (a texture size every GPU/driver path handles).
CHART_MAX_PX = 2048
SCRIPT_REL = Path("Resources") / "plugins" / "FlyWithLua" / "Scripts" / "vfr_kneeboard.lua"

_ASCII = {
    "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
    "→": "->", "←": "<-", "↑": "^", "·": "-", "•": "*", "°": " deg", "≤": "<=", "≥": ">=",
    "–": "-", "—": "-", "…": "...", "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "⚠": "!",
}


def ascii_text(s: str) -> str:
    """Readable ASCII: German umlauts spelled out, symbols replaced, accents dropped."""
    s = "".join(_ASCII.get(ch, ch) for ch in s)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s


def _lua_string(s: str) -> str:
    out = ['"']
    for ch in ascii_text(s):
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ord(ch) < 32 or ord(ch) == 127:
            out.append(f"\\{ord(ch):03d}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def to_lua(value, indent: int = 0) -> str:
    """JSON-shaped value → Lua literal. Lists become sequences, dicts records."""
    pad = "  " * (indent + 1)
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value) if math.isfinite(value) else "0"
    if isinstance(value, str):
        return _lua_string(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return "{}"
        items = ",\n".join(pad + to_lua(v, indent + 1) for v in value)
        return "{\n" + items + "\n" + "  " * indent + "}"
    if isinstance(value, dict):
        # nil fields are simply left out (a Lua table cannot hold nil).
        entries = [(str(k), v) for k, v in value.items() if v is not None]
        if not entries:
            return "{}"
        for key, _ in entries:
            if not key.isidentifier():
                raise ValueError(f"kneeboard field name must be an identifier: {key!r}")
        items = ",\n".join(f"{pad}{k} = {to_lua(v, indent + 1)}" for k, v in entries)
        return "{\n" + items + "\n" + "  " * indent + "}"
    raise TypeError(f"cannot write {type(value).__name__} to Lua")


def _export_chart(src: Path, target: Path) -> tuple[int, int]:
    """Write *src* as an RGB JPEG no larger than CHART_MAX_PX; returns (w, h)."""
    from PIL import Image

    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((CHART_MAX_PX, CHART_MAX_PX), Image.LANCZOS)
        if not target.exists():
            im.save(target, "JPEG", quality=90)
        return im.size


def copy_charts(icaos: list[str], xplane_root: Path, cache_root: Path = airport_charts.CACHE_ROOT) -> list[dict]:
    """Export the cached chart pages of *icaos* next to the plan; never downloads.

    File names carry the AIP issue, so a new issue gets new names and the
    script loads fresh textures (FlyWithLua cannot reload an image in place).
    """
    out = []
    for icao in dict.fromkeys(i.upper() for i in icaos if i):
        have = airport_charts.cached(icao, cache_root)
        if not have:
            continue
        dest = xplane_root / CHARTS_REL / icao
        dest.mkdir(parents=True, exist_ok=True)
        pages = []
        for p in have["pages"]:
            name = Path(p["file"]).with_suffix(".jpg").name
            w, h = _export_chart(Path(p["path"]), dest / name)
            pages.append({"title": p["title"], "file": f"charts/{icao}/{name}", "width": w, "height": h})
        keep = {Path(p["file"]).name for p in pages}
        for old in [*dest.glob("*.png"), *dest.glob("*.jpg")]:
            if old.name not in keep:
                old.unlink(missing_ok=True)
        out.append({"icao": icao, "name": have.get("name", ""), "pages": pages})
    return out


def send(plan: dict, xplane_root: Path) -> dict:
    """Write the plan for the kneeboard and install/update the script. Returns what happened."""
    if not (xplane_root / "Resources" / "plugins").is_dir():
        raise FileNotFoundError(f"X-Plane not found at {xplane_root}")
    plan = dict(plan)
    plan["charts"] = copy_charts(plan.pop("chart_icaos", None) or [], xplane_root)
    plan_path = xplane_root / PLAN_REL
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan_path.with_suffix(".tmp")
    # Write-then-rename: the script polls the file and must never read half of it.
    tmp.write_text("-- Written by VFR Navlog (Send to X-Plane). Data only.\nreturn " + to_lua(plan) + "\n",
                   encoding="utf-8")
    tmp.replace(plan_path)

    script_dir = xplane_root / SCRIPT_REL.parent
    installed = False
    if script_dir.is_dir():
        target = xplane_root / SCRIPT_REL
        src = SCRIPT_SRC.read_text(encoding="utf-8")
        if not target.exists() or target.read_text(encoding="utf-8") != src:
            target.write_text(src, encoding="utf-8")
            installed = True
    return {
        "plan_path": str(plan_path),
        "script_installed": installed,
        "flywithlua": script_dir.is_dir(),
    }
