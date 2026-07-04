"""PDF rendering: the render() orchestrator and the individual page builders."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ..model import RenderContext
from .base import NavlogPDF, install_fonts
from .charts import _append_dfs_charts
from .destination import render_destination_page
from .navlog_page import render_navlog_page
from .phraseology import render_phraseology
from .waypoint_pages import render_waypoint_pages
from .weather_page import render_weather_page


def render(ctx: RenderContext, out: Path) -> None:
    pdf = NavlogPDF()
    font = install_fonts(pdf)
    date_str = datetime.now().strftime("%Y-%m-%d")

    render_navlog_page(pdf, font, ctx, date_str)

    # Departure airport charts start right after the nav table (page 3): the
    # plates needed first in flight come first in the stack.
    dep_icao = ctx.plan.waypoints[0].ident.upper()
    dest_icao = ctx.plan.waypoints[-1].ident.upper()
    if ctx.with_dfs_charts and dep_icao != dest_icao:
        _append_dfs_charts(pdf, dep_icao)

    # Per-waypoint chart pages follow (kneeboard order).
    render_waypoint_pages(pdf, font, ctx)

    render_phraseology(pdf, font, ctx.plan, ctx.aircraft, ctx.vatsim, ctx.fir_icaos or [])

    if ctx.dest_info is not None:
        render_destination_page(pdf, font, ctx.dest_info, ctx.vatsim, ctx.navaids)

    if ctx.weather is not None:
        render_weather_page(pdf, font, ctx.weather, fir_icaos=ctx.fir_icaos, vatsim=ctx.vatsim)

    # Destination airport charts close the stack.
    if ctx.with_dfs_charts:
        _append_dfs_charts(pdf, dest_icao)

    pdf.output(str(out))
