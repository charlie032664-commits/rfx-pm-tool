# -*- coding: utf-8 -*-
"""Shared helper for Step 1.5 file selection (Phase 7 — enforcement).

Reads inbound/<case_id>/meta/file_selection.json (written by app.py Step 1.5
in Phase 3) and returns the set of filenames the PM marked include=False.

Conservative defaults — falls back to empty set (= process everything) when:
  - the JSON file does not exist
  - the file is unreadable or not valid JSON
  - the top-level value is not a dict
  - an entry's `include` field is missing, None, or anything other than
    a literal False

This preserves backward compatibility with case baselines captured
before file_selection.json existed.

Usage:
    from file_selection import load_excluded
    excluded = load_excluded(case_dir)              # set[str]
    files = [fp for fp in files if fp.name not in excluded]
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Set


def load_excluded(case_dir: Path) -> Set[str]:
    """Return set of filenames marked include=False in this case.

    Empty set means "no exclusions" or "no selection file" — caller MUST
    default to processing every file in that case.
    """
    p = Path(case_dir) / "meta" / "file_selection.json"
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(data, dict):
        return set()
    excluded: Set[str] = set()
    for name, info in (data.get("selections") or {}).items():
        # Only a literal False excludes. Missing / None / truthy → include.
        if isinstance(info, dict) and info.get("include") is False:
            excluded.add(str(name))
    return excluded
