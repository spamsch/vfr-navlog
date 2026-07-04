"""Per-waypoint briefing pages: an OFM chart excerpt beside the VOR fix block.

One landscape page per waypoint that has a prepared map, in route order,
inserted directly after the nav table. The images are fetched in cli.run()'s
fetch stage and carried on RenderContext.wp_maps; this module only lays them out.
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
        wpm = wp_maps[i] if i < len(wp_maps) else None
        if wpm is None:
            continue
        _render_one(pdf, font, ctx, i, wp, wpm, plan, legs, n)
        added += 1
    return added


def _render_one(pdf, font, ctx, i, wp, wpm, plan, legs, n) -> None:
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

    body_top = y0 + 11
    left_w = 60.0
    # ---------- left column: VOR fix block + coordinates ----------
    _render_fix_block(pdf, font, x0, body_top, left_w, wp)

    # ---------- coordinates (GPS cross-check) ----------
    coord_y = pdf.h - pdf.b_margin - 20
    pdf.set_xy(x0, coord_y)
    pdf.set_font(font, "B", 8)
    pdf.set_fill_color(230, 230, 230)
    pdf.cell(left_w, 5, "  Koordinaten  ·  Position", border=1, fill=True)
    pdf.ln(5)
    pdf.set_font(font, "", 9)
    pdf.set_x(x0)
    pdf.cell(left_w, 5, "  " + _dms(wp.lat, "N", "S"), border=1)
    pdf.ln(5)
    pdf.set_x(x0)
    pdf.cell(left_w, 5, "  " + _dms(wp.lon, "E", "W"), border=1)

    # ---------- right: the annotated map excerpt ----------
    map_x = x0 + left_w + 4
    map_w = x0 + pw - map_x
    map_side = min(map_w, pdf.h - pdf.b_margin - body_top - 6)
    map_x += (map_w - map_side) / 2.0
    png = BytesIO()
    wpm.image.save(png, format="PNG")
    png.seek(0)
    pdf.image(png, x=map_x, y=body_top, w=map_side, h=map_side)
    pdf.rect(map_x, body_top, map_side, map_side)

    # ---------- footer: OFM attribution with the cycle actually used ----------
    pdf.set_xy(x0, pdf.h - pdf.b_margin - 4)
    pdf.set_font(font, "I", 7)
    pdf.cell(0, 3,
             f"© open flightmaps — OFMA General Users' License — AIRAC {wpm.cycle}",
             align="C")


def _render_fix_block(pdf, font, x, y, w, wp) -> None:
    pdf.set_xy(x, y)
    pdf.set_font(font, "B", 9)
    pdf.set_fill_color(225, 235, 225)
    pdf.cell(w, 5, "  VOR-Kreuzpeilung  ·  VOR fixes", border=1, fill=True)
    pdf.ln(5)

    if wp.vor_info:
        # Manual reference text overrides computed fixes (same precedence as the table).
        pdf.set_x(x)
        pdf.set_font(font, "B", 11)
        pdf.multi_cell(w, 6, wp.vor_info, border=1)
    elif wp.fixes:
        for fx in wp.fixes:
            pdf.set_x(x)
            if fx.overhead:
                pdf.set_font(font, "B", 11)
                pdf.cell(w, 6, f"  {fx.vor_ident} {fx.freq}", border="LTR")
                pdf.ln(6)
                pdf.set_x(x)
                pdf.set_font(font, "", 8)
                pdf.cell(w, 5, "  ↑ overhead (Stationsüberflug)", border="LBR")
                pdf.ln(5)
            else:
                pdf.set_font(font, "B", 12)
                line = f"  {fx.vor_ident} {fx.freq}  R{fx.radial:03d}"
                pdf.cell(w, 7, line, border="LTR")
                pdf.ln(7)
                pdf.set_x(x)
                pdf.set_font(font, "", 8)
                dme = f"DME {fx.dist_nm:.0f} nm" if fx.has_dme else "keine DME"
                pdf.cell(w * 0.55, 5, f"  {fx.vor_name}", border="LB")
                pdf.cell(w * 0.45, 5, f"{dme}  ", border="RB", align="R")
                pdf.ln(5)
            # Morse identification line.
            pdf.set_x(x)
            pdf.set_font(font, "", 8)
            pdf.cell(w, 4.5, f"  {morse(fx.vor_ident)}", border="LBR")
            pdf.ln(5.5)
    else:
        pdf.set_x(x)
        pdf.set_font(font, "I", 8)
        pdf.cell(w, 5, "  keine VOR-Kreuzpeilung", border=1)
        pdf.ln(5)
