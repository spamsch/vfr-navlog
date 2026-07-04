#!/usr/bin/env python3
"""Thin entry-point shim. The implementation lives in the vfr_navlog package.

Kept so `python3 navlog.py …` keeps working; the installed `vfr-navlog`
console script calls the same vfr_navlog.cli:main.
"""
from vfr_navlog.cli import main

if __name__ == "__main__":
    main()
