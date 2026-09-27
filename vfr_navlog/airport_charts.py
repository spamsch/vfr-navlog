"""Airport chart pages (DFS AIP) cached on disk for the app and the in-sim kneeboard.

German aerodromes only (ICAO prefix ED/ET): the BasicVFR chapter (visual
approach / aerodrome charts) plus the VFR-relevant BasicIFR aerodrome, ground
movement and parking charts — the same selection as ``--dfs-charts``.

Cache: ``~/.cache/vfr-navlog/charts/<ICAO>/`` holds the PNGs and an
``index.json`` naming the AIP chapter each set came from. A refresh asks DFS for
the current chapter URLs (cheap) and re-downloads only when they changed, i.e.
when a new AIP issue replaced the charts. Offline, the cached set is returned.

DFS charts are copyrighted: personal flight preparation only, never redistribute
(the cache stays local; nothing here is committed or uploaded).
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

CACHE_ROOT = Path.home() / ".cache" / "vfr-navlog" / "charts"
REFRESH_DAYS = 7  # don't ask DFS again for a set fetched within this many days

_ICAO_RE = re.compile(r"^E[DT][A-Z]{2}$")


def is_supported(icao: str) -> bool:
    return bool(_ICAO_RE.match(icao.upper()))


def _index_path(icao: str, cache_root: Path) -> Path:
    return cache_root / icao.upper() / "index.json"


def cached(icao: str, cache_root: Path = CACHE_ROOT) -> dict | None:
    """The cached chart set for *icao* (no network), or None."""
    path = _index_path(icao, cache_root)
    if not path.exists():
        return None
    try:
        index = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    base = path.parent
    pages = [p for p in index.get("pages", []) if (base / p["file"]).exists()]
    if not pages:
        return None
    return {**index, "pages": [{**p, "path": str(base / p["file"])} for p in pages]}


def _issue(chapter_url: str | None) -> str:
    """'2026SEP17' from https://aip.dfs.de/BasicVFR/2026SEP17/chapter/…"""
    m = re.search(r"/Basic(?:VFR|IFR)/([^/]+)/", chapter_url or "")
    return m.group(1) if m else "unknown"


def fetch(icao: str, cache_root: Path = CACHE_ROOT, force: bool = False) -> dict:
    """Chart set for *icao*: cached when current, else downloaded. Never raises for
    network trouble — falls back to the cache and reports it in "error"."""
    from PIL import Image

    from . import dfs_charts

    icao = icao.upper()
    if not is_supported(icao):
        return {"icao": icao, "pages": [], "error": "Airport charts are available for German aerodromes (ED/ET) only."}

    have = cached(icao, cache_root)
    if have and not force:
        fetched = date.fromisoformat(have.get("fetched", "1970-01-01")[:10])
        if (date.today() - fetched).days < REFRESH_DAYS:
            return have

    try:
        vfr_url = dfs_charts.find_chapter_url(icao, "vfr")
        ifr_url = dfs_charts.find_chapter_url(icao, "ifr")
    except Exception as exc:  # offline or DFS unreachable
        if have:
            return {**have, "error": f"DFS not reachable, showing cached charts ({exc.__class__.__name__})"}
        return {"icao": icao, "pages": [], "error": f"DFS not reachable: {exc}"}

    if have and have.get("vfr_url") == vfr_url and have.get("ifr_url") == ifr_url and not force:
        # Same AIP chapters: charts unchanged. Just note that we checked.
        index = {k: v for k, v in have.items() if k != "pages"}
        index["pages"] = [{k: v for k, v in p.items() if k != "path"} for p in have["pages"]]
        index["fetched"] = datetime.now(timezone.utc).isoformat()
        _index_path(icao, cache_root).write_text(json.dumps(index, indent=1), encoding="utf-8")
        return {**have, "fetched": index["fetched"]}

    sections = []
    if vfr_url:
        sections.append(("vfr", vfr_url, {"skip_junk": True}))
    if ifr_url:
        sections.append(("ifr", ifr_url, {"vfr_filter": True}))
    if not sections:
        return {"icao": icao, "pages": [], "error": f"{icao} is not in the DFS AIP."}

    base = cache_root / icao
    base.mkdir(parents=True, exist_ok=True)
    pages = []
    try:
        for section, url, kw in sections:
            issue = _issue(url)
            for title, page_url in dfs_charts.list_charts(url, **kw):
                png = dfs_charts.extract_png(page_url)
                if not png:
                    continue
                name = f"{section}_{issue}_{len(pages) + 1:02d}.png"
                (base / name).write_bytes(png)
                with Image.open(base / name) as im:
                    w, h = im.size
                pages.append({"title": title, "file": name, "width": w, "height": h, "section": section})
                print(f"[charts] {icao}: {title}", file=sys.stderr)
    except Exception as exc:
        if have:
            return {**have, "error": f"Download failed, showing cached charts: {exc}"}
        return {"icao": icao, "pages": [], "error": f"Download failed: {exc}"}

    index = {
        "icao": icao,
        "name": dfs_charts._vfr_name_cache.get(icao, ""),
        "vfr_url": vfr_url,
        "ifr_url": ifr_url,
        "fetched": datetime.now(timezone.utc).isoformat(),
        "pages": pages,
    }
    _index_path(icao, cache_root).write_text(json.dumps(index, indent=1), encoding="utf-8")
    # Drop PNGs of the previous issue.
    keep = {p["file"] for p in pages}
    for old in base.glob("*.png"):
        if old.name not in keep:
            old.unlink(missing_ok=True)
    return cached(icao, cache_root) or {"icao": icao, "pages": []}
