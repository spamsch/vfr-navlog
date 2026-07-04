#!/usr/bin/env python3
"""Compatibility shim. The implementation now lives in vfr_navlog.dfs_charts.

Keeps `python3 dfs_charts.py EDDV` working as documented in the README.
"""
from vfr_navlog.dfs_charts import (  # noqa: F401
    download_section,
    extract_png,
    find_chapter_url,
    is_vfr_relevant_ifr_chart,
    list_charts,
    main,
    safe_name,
)

if __name__ == "__main__":
    main()
