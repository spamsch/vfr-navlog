#!/usr/bin/env python3
"""
Download VFR charts for a German airport from the DFS AIP.

Fetches:
  - All chart pages from the DFS BasicVFR section (VFR approach/area charts)
  - Aerodrome and ground movement charts from the DFS BasicIFR section

Output: <ICAO>/<ICAO>_vfr_charts.pdf  (combined, all pages)
        <ICAO>/NN_<title>.png          (individual PNGs, indexed)

Usage:
    python3 dfs_charts.py EDDV
    python3 dfs_charts.py EDLI --no-ifr-aerodrome
    python3 dfs_charts.py EDDV --out /tmp/charts
"""

import argparse
import base64
import html as html_module
import re
import sys
from pathlib import Path

import img2pdf
import requests

# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en",
})


def get(url: str) -> str:
    r = SESSION.get(url, timeout=30)
    r.raise_for_status()
    return r.text


# ---------------------------------------------------------------------------
# Discovery: find current DFS chapter URL via aip.aero
# ---------------------------------------------------------------------------

_CHAPTER_RE = re.compile(
    r'https://aip\.dfs\.de/Basic(?:VFR|IFR)/\d{4}[A-Z]+\d+/chapter/[0-9a-fA-F]+\.html'
)


def find_chapter_url(icao: str, section: str) -> str | None:
    """
    Query aip.aero to get the current DFS chapter URL for *section* ('vfr'|'ifr').
    aip.aero embeds the URL in its HTML/JS bundle even before JS executes.
    """
    url = f"https://aip.aero/de/en/{section}/?{icao}="
    html = get(url)
    m = _CHAPTER_RE.search(html)
    return m.group(0) if m else None


# ---------------------------------------------------------------------------
# Chart listing
# ---------------------------------------------------------------------------

# IFR chart title keywords relevant for VFR pilots.
# Excludes: STARs, SIDs, ILS/RNP approach charts, obstacle/terrain charts.
_IFR_KEEP = (
    "Aerodrome Chart",
    "Aerodrome Ground Movement Chart",
    "Aircraft Parking",
    "Docking Chart",
    "Visual Operation Chart",
    "Hot Spot",
)

# BasicVFR pages whose titles are bare section references (e.g. "AD 2-43"),
# not actual charts — these are legend/index pages from the VFR manual.
_VFR_JUNK_RE = re.compile(r'^AD\s+\d+-\d+$')

# Page hash format: DFS uses uppercase hex for page hashes (chapter hashes are
# lowercase). Accept both to be safe against future normalisation changes.
_PAGE_HASH_RE = re.compile(
    r'<a[^>]*href="\.\./pages/([0-9A-Fa-f]+)\.html"[^>]*>\s*<span[^>]*>([^<]+)</span>'
)


def is_vfr_relevant_ifr_chart(title: str) -> bool:
    return any(kw in title for kw in _IFR_KEEP)


def list_charts(
    chapter_url: str,
    vfr_filter: bool = False,
    skip_junk: bool = False,
) -> list[tuple[str, str]]:
    """
    Return [(title, page_url), ...] for charts in a chapter page.

    vfr_filter: keep only IFR charts that are useful for VFR pilots.
    skip_junk:  drop bare AIP section-reference pages (e.g. "AD 2-43").
    """
    html = get(chapter_url)
    # Strip /chapter/HASH.html to get the AIRAC base URL, then append /pages/
    base = "/".join(chapter_url.split("/")[:-2])
    results = []
    for page_hash, raw_title in _PAGE_HASH_RE.findall(html):
        title = html_module.unescape(raw_title.strip())
        if skip_junk and _VFR_JUNK_RE.match(title):
            continue
        if vfr_filter and not is_vfr_relevant_ifr_chart(title):
            continue
        results.append((title, f"{base}/pages/{page_hash}.html"))
    return results


# ---------------------------------------------------------------------------
# Image extraction
# ---------------------------------------------------------------------------

_IMG_RE = re.compile(r'id="imgAIP"[^>]*src="data:image/png;base64,([^"]+)"')


def extract_png(page_url: str) -> bytes | None:
    """Fetch a DFS AIP page and return the embedded chart PNG bytes, or None."""
    html = get(page_url)
    m = _IMG_RE.search(html)
    if not m:
        return None
    return base64.b64decode(m.group(1).replace("\n", "").replace("\r", "").replace(" ", ""))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_name(title: str) -> str:
    return re.sub(r'[^\w\-. ]+', '_', title).strip("_")


def download_section(
    label: str,
    chapter_url: str,
    *,
    vfr_filter: bool = False,
    skip_junk: bool = False,
) -> list[tuple[str, bytes]]:
    """Fetch all matching chart pages from one chapter, return (title, png) list."""
    charts = list_charts(chapter_url, vfr_filter=vfr_filter, skip_junk=skip_junk)
    kind = "aerodrome chart(s)" if vfr_filter else "chart(s)"
    print(f"      {len(charts)} {kind} found")
    results = []
    for title, page_url in charts:
        print(f"      Downloading: {title}")
        png = extract_png(page_url)
        if png:
            results.append((title, png))
        else:
            print(f"      WARNING: no image on {page_url}", file=sys.stderr)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Download VFR charts from the DFS AIP.")
    ap.add_argument("icao", help="ICAO airport code, e.g. EDDV")
    ap.add_argument("--out", type=Path, default=None,
                    help="Output directory (default: ./<ICAO>)")
    ap.add_argument("--no-ifr-aerodrome", action="store_true",
                    help="Skip aerodrome/ground charts from the BasicIFR section")
    args = ap.parse_args()

    icao = args.icao.upper()
    out_dir: Path = args.out or Path(icao)
    out_dir.mkdir(parents=True, exist_ok=True)

    all_charts: list[tuple[str, bytes]] = []

    # ---- VFR section --------------------------------------------------------
    print(f"[1/2] BasicVFR chapter for {icao} …")
    vfr_url = find_chapter_url(icao, "vfr")
    if vfr_url:
        print(f"      → {vfr_url}")
        all_charts += download_section("VFR", vfr_url, skip_junk=True)
    else:
        print(f"      No BasicVFR entry found for {icao}")

    # ---- IFR aerodrome charts -----------------------------------------------
    if not args.no_ifr_aerodrome:
        print(f"[2/2] BasicIFR aerodrome charts for {icao} …")
        ifr_url = find_chapter_url(icao, "ifr")
        if ifr_url:
            print(f"      → {ifr_url}")
            all_charts += download_section("IFR-aerodrome", ifr_url, vfr_filter=True)
        else:
            print(f"      No BasicIFR entry found for {icao}")

    if not all_charts:
        sys.exit(f"No charts downloaded for {icao}.")

    # ---- Save individual PNGs -----------------------------------------------
    for i, (title, png) in enumerate(all_charts):
        fname = out_dir / f"{i+1:02d}_{safe_name(title)}.png"
        fname.write_bytes(png)
        print(f"Saved  {fname}")

    # ---- Assemble PDF -------------------------------------------------------
    pdf_path = out_dir / f"{icao}_vfr_charts.pdf"
    try:
        pdf_bytes = img2pdf.convert([png for _, png in all_charts])
    except img2pdf.ImageOpenError as e:
        sys.exit(f"img2pdf failed ({e}). Try installing Pillow and re-running.")
    pdf_path.write_bytes(pdf_bytes)
    print(f"\nPDF  → {pdf_path}  ({len(all_charts)} page(s))")


if __name__ == "__main__":
    main()
