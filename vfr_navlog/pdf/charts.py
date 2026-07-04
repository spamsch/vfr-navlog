"""Append DFS AIP chart pages to a navlog PDF."""
from __future__ import annotations

import sys
from io import BytesIO

from fpdf import FPDF


def _append_dfs_charts(pdf: FPDF, icao: str) -> int:
    """
    Fetch VFR charts from the DFS AIP and append them as pages to *pdf*.
    Returns the number of pages added. Gracefully skips on any failure.
    """
    try:
        from vfr_navlog.dfs_charts import extract_png, find_chapter_url, list_charts
    except ImportError:
        print("[dfs] dfs_charts.py not found — skipping chart pages", file=sys.stderr)
        return 0

    from PIL import Image as PILImage

    print(f"[dfs] Fetching VFR charts for {icao}…")
    pngs: list[tuple[str, bytes]] = []

    try:
        vfr_url = find_chapter_url(icao, "vfr")
        if vfr_url:
            for title, page_url in list_charts(vfr_url, skip_junk=True):
                png = extract_png(page_url)
                if png:
                    pngs.append((title, png))
                    print(f"[dfs]   {title}")

        ifr_url = find_chapter_url(icao, "ifr")
        if ifr_url:
            for title, page_url in list_charts(ifr_url, vfr_filter=True):
                png = extract_png(page_url)
                if png:
                    pngs.append((title, png))
                    print(f"[dfs]   {title}")
    except Exception as exc:
        print(f"[dfs] Chart fetch failed: {exc}", file=sys.stderr)
        return 0

    for _title, png_bytes in pngs:
        img = PILImage.open(BytesIO(png_bytes))
        w_px, h_px = img.size
        pdf.add_page(orientation="L" if w_px > h_px else "P")
        pdf.image(BytesIO(png_bytes), x=0, y=0, w=pdf.w, h=pdf.h)

    if pngs:
        print(f"[dfs] Added {len(pngs)} chart page(s)")
    else:
        print(f"[dfs] No VFR charts found for {icao}")
    return len(pngs)
