"""Per-waypoint briefing pages: chart and photo side by side.

One landscape page per waypoint that has at least one prepared map, in route
order, inserted directly after the nav table. Layout:

    header strip          — WP n/N, ident, name, altitude, in/out headings
    VOR fix band          — full width under the header; collapses when empty
    two maps side by side  — chart (left) + photo (right), ~equal squares
    per-map captions       — the OFM cycle line and the photo licence line

Degrades cleanly: photo missing → chart alone, full width; chart missing →
photo alone, full width; neither → the orchestrator returned None and the page
is skipped. The images arrive fully annotated (marker, route, scale) and already
sized to a common square from cli.run()'s fetch stage; this module only places
them. Both attributions are printed verbatim as the licences require.
"""
from __future__ import annotations

from io import BytesIO

from ..fixes import morse
from ..legs import _effective_leg_alt
from .base import fmt_int, hms


def _dms(value: float, positive: str, negative: str) -> str:
    """Degrees-minutes for a GPS cross-check: 52 27.7 N."""
    hemi = positive if value >= 0 else negative
    v = abs(value)
    deg = int(v)
    minutes = (v - deg) * 60.0
    return f"{deg:02d}° {minutes:04.1f}' {hemi}"


def render_waypoint_pages(pdf, font, ctx) -> int:
    """Append one page per waypoint that has a prepared excerpt. Returns count."""
    wp_maps = ctx.wp_maps or []
    if not wp_maps:
        return 0
    plan = ctx.plan
    legs = ctx.legs
    wps = plan.waypoints
    n = len(wps)
    added = 0
    for i, wp in enumerate(wps):
        wl = wp_maps[i] if i < len(wp_maps) else None
        if wl is None or wl.empty():
            continue
        _render_one(pdf, font, ctx, i, wp, wl, plan, legs, n)
        added += 1
    return added


def _render_one(pdf, font, ctx, i, wp, wl, plan, legs, n) -> None:
    pdf.add_page()
    pw = pdf.w - pdf.l_margin - pdf.r_margin
    x0 = pdf.l_margin
    y0 = pdf.t_margin

    inbound = legs[i - 1] if i > 0 else None
    outbound = legs[i] if i < n - 1 else None

    # ---------- header strip ----------
    pdf.set_xy(x0, y0)
    pdf.set_font(font, "B", 13)
    title = f"WP {i + 1}/{n}   ·   {wp.ident}   {wp.name}"
    pdf.cell(pw * 0.55, 7, title, border=0)

    # Altitude at this waypoint: departure/destination show field elevation,
    # interior waypoints the effective cruise altitude of the outbound leg.
    if 0 < i < n - 1:
        alt = _effective_leg_alt(plan, i)
    else:
        alt = wp.alt_ft or 0
    pdf.set_font(font, "", 9)
    pdf.set_xy(x0 + pw * 0.55, y0)
    parts = [f"Alt {fmt_int(alt)} ft"]
    if inbound is not None:
        parts.append(f"in MH {fmt_int(inbound.mh):>03}°  {inbound.distance_nm:.1f} NM  {hms(inbound.ete_min)}")
    if outbound is not None:
        parts.append(f"out MH {fmt_int(outbound.mh):>03}°")
    pdf.cell(pw * 0.45, 7, "   ".join(parts), border=0, align="R")
    pdf.ln(8)
    pdf.set_draw_color(120, 120, 120)
    pdf.line(x0, y0 + 8.5, x0 + pw, y0 + 8.5)
    pdf.set_draw_color(0, 0, 0)

    # ---------- VOR fix band (full width, collapses when empty) ----------
    body_top = y0 + 11
    band_h = _render_fix_band(pdf, font, x0, body_top, pw, wp)
    maps_top = body_top + band_h + (2 if band_h else 0)

    # Coordinate strip (GPS cross-check), pinned to the page bottom.
    coord_h = 5.0
    caption_h = 8.0
    coord_y = pdf.h - pdf.b_margin - coord_h
    maps_bottom = coord_y - caption_h - 2

    # ---------- maps: chart left, photo right (whichever is present) ----------
    avail_h = maps_bottom - maps_top
    gap = 6.0
    both = wl.chart is not None and wl.photo is not None
    if both:
        each_w = (pw - gap) / 2.0
        side = min(each_w, avail_h)
        total_w = 2 * side + gap
        left = x0 + (pw - total_w) / 2.0
        _place_map(pdf, wl.chart, left, maps_top, side, wl.chart_attribution, font, caption_h)
        _place_map(pdf, wl.photo, left + side + gap, maps_top, side, wl.photo_attribution, font, caption_h)
    else:
        img = wl.chart if wl.chart is not None else wl.photo
        attr = wl.chart_attribution if wl.chart is not None else wl.photo_attribution
        side = min(pw, avail_h)
        left = x0 + (pw - side) / 2.0
        _place_map(pdf, img, left, maps_top, side, attr, font, caption_h)

    # ---------- coordinate strip (bottom-left) ----------
    pdf.set_xy(x0, coord_y)
    pdf.set_font(font, "", 8)
    coords = f"  {_dms(wp.lat, 'N', 'S')}    {_dms(wp.lon, 'E', 'W')}"
    pdf.cell(pw, coord_h, coords, border=0)


def _place_map(pdf, image, x, y, side, attribution, font, caption_h) -> None:
    png = BytesIO()
    image.save(png, format="PNG")
    png.seek(0)
    pdf.image(png, x=x, y=y, w=side, h=side)
    pdf.rect(x, y, side, side)
    if attribution:
        pdf.set_xy(x, y + side + 1)
        pdf.set_font(font, "I", 7)
        pdf.cell(side, 3, attribution, align="C")


def _render_fix_band(pdf, font, x, y, w, wp) -> float:
    """Draw the VOR cross-checks as a horizontal band. Returns its height in mm,
    or 0.0 when the waypoint has no fixes (band collapses, maps get the room)."""
    if not wp.vor_info and not wp.fixes:
        return 0.0

    label_h = 5.0
    pdf.set_xy(x, y)
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 235, 225)
    pdf.cell(w, label_h, "  VOR-Kreuzpeilung  ·  VOR fixes", border=1, fill=True)

    body_y = y + label_h
    if wp.vor_info:
        pdf.set_xy(x, body_y)
        pdf.set_font(font, "B", 12)
        pdf.cell(w, 7, f"  {wp.vor_info}", border="LBR")
        return label_h + 7

    # Computed fixes side by side, one column each.
    fixes = wp.fixes
    cell_w = w / max(1, len(fixes))
    body_h = 12.0
    for k, fx in enumerate(fixes):
        cx = x + k * cell_w
        pdf.set_xy(cx, body_y)
        pdf.set_font(font, "B", 12)
        if fx.overhead:
            head = f"  {fx.vor_ident} {fx.freq}  ↑ overhead"
        else:
            head = f"  {fx.vor_ident} {fx.freq}  R{fx.radial:03d}"
        pdf.cell(cell_w, 6, head, border="LTR" if k == 0 else "TR")
        pdf.set_xy(cx, body_y + 6)
        pdf.set_font(font, "", 8)
        if fx.overhead:
            sub = "  Stationsüberflug"
        else:
            dme = f"DME {fx.dist_nm:.0f} nm" if fx.has_dme else "keine DME"
            sub = f"  {fx.vor_name} · {dme} · {morse(fx.vor_ident)}"
        pdf.cell(cell_w, 6, sub, border="LBR" if k == 0 else "BR")
    return label_h + body_h
