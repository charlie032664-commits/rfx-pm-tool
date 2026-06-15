# -*- coding: utf-8 -*-
"""Runtime estimator + large-run guard (v1.3).

Estimates how long a Full Pipeline extract would take for a case, BEFORE
launching it, so the UI can warn on expensive runs. Pure analysis: it reads the
RFQ files and reuses the extractor's chunking, but makes NO LLM call.

Per-call seconds are provider-aware (internal reasoning models are much slower
than OpenAI). Risk thresholds: >60 min = warning, >4 h = strong warning.

Usage:
    python runtime_estimator.py --case inbound/<case> [--provider internal]
    python runtime_estimator.py --self-test
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Observed average seconds per LLM call by provider (extract/enrich chunk).
PROVIDER_SECONDS = {"internal": 20.0, "openai": 0.6}
DEFAULT_SECONDS = 5.0

WARN_SECONDS = 60 * 60          # 1 hour  -> warning
STRONG_SECONDS = 4 * 60 * 60    # 4 hours -> strong warning

_EXTS = (".docx", ".doc", ".xlsx", ".xls", ".pdf", ".md", ".txt")


def seconds_per_call(provider: str) -> float:
    return PROVIDER_SECONDS.get((provider or "").strip().lower(), DEFAULT_SECONDS)


def classify_risk(seconds: float) -> str:
    if seconds >= STRONG_SECONDS:
        return "high"        # strong warning
    if seconds >= WARN_SECONDS:
        return "warning"
    return "ok"


def fmt_duration(seconds: float) -> str:
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec}s"
    return f"{sec}s"


def estimate_chunks(case_dir: Path, max_chars: int = 600, group_size: int = 2) -> Dict[str, Any]:
    """Count files, text size, and chunks for a case's rfq/ folder (no LLM)."""
    import extract_requirements_llm as ex
    rfq = Path(case_dir) / "rfq"
    out: Dict[str, Any] = {"file_count": 0, "total_chars": 0, "chunks": 0, "per_file": []}
    if not rfq.exists():
        out["error"] = f"rfq folder not found: {rfq}"
        return out
    per_file: List[Dict[str, Any]] = []
    for fp in sorted(rfq.glob("*")):
        if fp.is_dir() or fp.suffix.lower() not in _EXTS:
            continue
        suf = fp.suffix.lower()
        out["file_count"] += 1
        c, tc = 0, 0
        try:
            if suf == ".docx":
                blocks = ex.read_docx_blocks(fp)
            elif suf == ".doc":
                blocks = ex.read_doc_blocks(fp)
            elif suf in (".xlsx", ".xls"):
                blocks = ex.read_xlsx_blocks(fp, client=None)
            elif suf == ".pdf":
                blocks = ex.read_pdf_blocks(fp)
            else:
                blocks = [fp.read_text(encoding="utf-8", errors="ignore")]
            if suf in (".md", ".txt"):
                ch = ex.split_chunks_generic(blocks[0] if blocks else "", max_chars=max_chars)
            else:
                ch = ex.chunks_from_blocks(blocks, max_chars=max_chars, group_size=group_size)
            c = len(ch)
            tc = sum(len(b) for b in blocks)
        except Exception as e:
            per_file.append({"name": fp.name, "chunks": 0, "chars": 0, "error": type(e).__name__})
            continue
        out["chunks"] += c
        out["total_chars"] += tc
        per_file.append({"name": fp.name, "chunks": c, "chars": tc})
    out["per_file"] = per_file
    return out


def estimate_case(case_dir: Path, provider: str, model: str = "",
                  max_chars: int = 600, group_size: int = 2) -> Dict[str, Any]:
    """Full estimate dict for the UI / CLI."""
    base = estimate_chunks(case_dir, max_chars, group_size)
    per_call = seconds_per_call(provider)
    chunks = int(base.get("chunks", 0))
    # Extract is the dominant cost for large cases; enrich adds ~1 call per
    # produced requirement (unknown pre-run) — flagged separately, not summed.
    est_runtime = chunks * per_call
    return {
        **base,
        "provider": provider,
        "model": model,
        "seconds_per_call": per_call,
        "est_extract_calls": chunks,
        "est_runtime_sec": est_runtime,
        "est_runtime_human": fmt_duration(est_runtime),
        "risk": classify_risk(est_runtime),
        "note": "Extract-only estimate; Enrich adds ~1 call per produced requirement.",
    }


def _self_test() -> int:
    assert classify_risk(10) == "ok"
    assert classify_risk(WARN_SECONDS) == "warning"
    assert classify_risk(STRONG_SECONDS) == "high"
    assert seconds_per_call("internal") == 20.0
    assert seconds_per_call("openai") == 0.6
    assert seconds_per_call("weird") == DEFAULT_SECONDS
    assert fmt_duration(30) == "30s"
    assert fmt_duration(90) == "1m 30s"
    assert fmt_duration(3700) == "1h 1m"
    # 3000 chunks on internal -> ~16.7h -> high
    assert classify_risk(3000 * seconds_per_call("internal")) == "high"
    # 200 chunks on openai -> 2 min -> ok
    assert classify_risk(200 * seconds_per_call("openai")) == "ok"
    print("[OK] runtime_estimator self-test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Estimate pipeline runtime for a case (no LLM).")
    ap.add_argument("--case", help="Inbound case folder (contains rfq/)")
    ap.add_argument("--provider", default="", help="Override provider; else from env")
    ap.add_argument("--model", default="")
    ap.add_argument("--max-chars", type=int, default=600)
    ap.add_argument("--group-size", type=int, default=2)
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.case:
        ap.error("--case is required (or use --self-test)")

    provider = args.provider
    model = args.model
    if not provider:
        try:
            from env_loader import load_env, describe_llm_config
            load_env()
            provider = str(describe_llm_config().get("provider", "") or "")
        except Exception:
            provider = "openai"

    est = estimate_case(Path(args.case), provider, model,
                        args.max_chars, args.group_size)
    print(f"provider={est['provider']} model={est.get('model') or '(default)'}")
    print(f"file_count={est['file_count']} total_chars={est['total_chars']}")
    print(f"est_chunks={est['chunks']} seconds_per_call={est['seconds_per_call']}")
    print(f"est_runtime={est['est_runtime_human']} ({int(est['est_runtime_sec'])}s) risk={est['risk'].upper()}")
    if est["risk"] == "high":
        print("[GUARD] STRONG WARNING: estimated > 4h — confirm before running a large case.")
    elif est["risk"] == "warning":
        print("[GUARD] WARNING: estimated > 1h.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
