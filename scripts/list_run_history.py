# -*- coding: utf-8 -*-
"""List recent pipeline runs across cases (v1.4 observability).

Read-only: scans runs/**/job_status.json and prints a stats-only table — no LLM,
no pipeline, no customer requirement text. Useful for a PM to see what ran, its
status/stage, provider/model, duration, and whether outputs exist.

Usage:
    python list_run_history.py --runs runs --limit 20
    python list_run_history.py --runs runs --json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

STD_OUTPUTS = ["requirements.json", "requirements_clean.json",
               "requirements_review.xlsx", "compliance_matrix.xlsx"]


def _load(p: Path) -> Optional[Dict[str, Any]]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def _duration(started: str, ended: str) -> str:
    if not started or not ended:
        return "-"
    try:
        secs = int((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds())
        if secs < 0:
            return "-"
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return (f"{h}h{m}m" if h else (f"{m}m{s}s" if m else f"{s}s"))
    except Exception:
        return "-"


def collect(runs_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for js_path in sorted(Path(runs_root).glob("**/job_status.json")):
        d = _load(js_path)
        if not d:
            continue
        run_dir = js_path.parent
        outs = sum(1 for f in STD_OUTPUTS if (run_dir / f).exists())
        rows.append({
            "case_id": d.get("case_id", run_dir.name),
            "status": d.get("status", "?"),
            "stage": d.get("stage", "?"),
            "provider": d.get("provider", ""),
            "model": d.get("model", ""),
            "started_at": d.get("started_at", ""),
            "ended_at": d.get("ended_at") or "",
            "duration": _duration(d.get("started_at", ""), d.get("ended_at") or ""),
            "outputs": f"{outs}/{len(STD_OUTPUTS)}",
            "run_dir": str(run_dir),
            "log_path": d.get("log_path", ""),
        })
    # newest first by ended_at then started_at
    rows.sort(key=lambda r: (r["ended_at"] or r["started_at"]), reverse=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="List recent pipeline runs (read-only, stats only).")
    ap.add_argument("--runs", default="runs", help="Runs root (default: runs)")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of a table")
    args = ap.parse_args()

    rows = collect(Path(args.runs))[: args.limit]
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0

    if not rows:
        print(f"(no job_status.json found under {args.runs})")
        return 0

    hdr = f"{'case_id':<34} {'status':<9} {'stage':<8} {'provider/model':<26} {'started':<19} {'dur':<7} {'out':<4}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        pm = f"{r['provider']}/{r['model']}"[:26]
        print(f"{r['case_id'][:34]:<34} {r['status']:<9} {r['stage']:<8} {pm:<26} "
              f"{(r['started_at'] or '')[:19]:<19} {r['duration']:<7} {r['outputs']:<4}")
    print(f"\n{len(rows)} run(s) shown. Stats only - no requirement text.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
