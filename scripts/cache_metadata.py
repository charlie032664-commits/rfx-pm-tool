# -*- coding: utf-8 -*-
"""Compute + persist + report extraction cache metadata for one case.

Phase 8 (feature/rfx-next-functions) — first wiring of the extraction_cache
scaffold into a real run, deliberately as a SEPARATE, read-mostly step.

What this step does:
  1. Read runs/<case>/requirements_clean.json (the canonical, post-normalize
     output) and runs/<case>/manifest.json (per-source-file sha256/size/mtime).
  2. Build a requirement-level ExtractionMeta for each item — requirement_text_hash
     from normalized_requirement, falling back to the original requirement text.
  3. Build a file-level ExtractionMeta per distinct source file (file hash reused
     from the manifest, requirement_text_hash=None).
  4. Compare against the previous runs/<case>/extraction_cache.json if present
     (should_skip per item + per file).
  5. Write the refreshed runs/<case>/extraction_cache.json and print a
     CacheSummary (total / reused / reprocessed / skipped unchanged / est. saved).

What this step does NOT do (by design, this phase):
  - It does NOT skip or reuse any LLM work — pipeline behaviour is unchanged.
    This is pure observability: it records what a future incremental run COULD
    reuse. Acting on it is a later step.
  - It does NOT modify the extractor, run_case, postprocess, normalize, or app.
    extraction_cache is imported here only.

Usage (run from the scripts/ dir, like the other stage scripts):
    python cache_metadata.py --case <case_id> --runs ..\\runs
    python cache_metadata.py --self-test          # no files needed
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# scripts/ on path so the sibling import works when launched as
# `python scripts/cache_metadata.py` from the repo root too.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extraction_cache import (  # noqa: E402
    PARSER_VERSION,
    PROMPT_VERSION,
    CacheSummary,
    ExtractionMeta,
    SkipDecision,
    requirement_text_hash,
    should_skip,
)

CACHE_FILENAME = "extraction_cache.json"


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Source-file resolution
# ---------------------------------------------------------------------------
# The clean.json `source` string is display-only: build_rows() strips the
# extension + an AUTO- prefix and truncates the filename to 40 chars, then
# appends an em-dash locator ("<fname> — 第 N 段"). We replicate that exact
# normalization on the manifest names so each item can be mapped back to its
# real source file (and thus its sha256/size/mtime).
_EXT_RE = re.compile(r"\.(docx|doc|xlsx|xls|pdf|txt|md)$", re.IGNORECASE)
_AUTO_RE = re.compile(r"^AUTO[-_]?", re.IGNORECASE)
_EMDASH = "—"


def _display_prefix(name: str) -> str:
    """Reproduce build_rows()'s filename normalization (ext/AUTO strip, 40 cap)."""
    s = _EXT_RE.sub("", Path(name).name)
    s = _AUTO_RE.sub("", s).strip()
    s = re.sub(r"^(UNKNOWN|UnknownFile)$", "", s, flags=re.IGNORECASE).strip()
    return s[:40]


def _item_source_prefix(source: str) -> str:
    """Filename portion of a clean.json item's `source` string (before the em dash)."""
    return (source or "").split(_EMDASH)[0].strip()


def build_manifest_index(manifest: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map display-prefix -> manifest file entry. Last write wins on collision."""
    index: Dict[str, Dict[str, Any]] = {}
    for entry in (manifest.get("files") or []):
        name = entry.get("name") or Path(entry.get("path", "")).name
        if name:
            index[_display_prefix(name)] = entry
    return index


def resolve_file_entry(
    source: str,
    manifest_index: Dict[str, Dict[str, Any]],
    files: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Best-effort map an item source string to its manifest file entry.

    Order: exact normalized-prefix match -> sole-file shortcut -> None.
    Returning None is fine: the item still gets a requirement_text_hash; only
    the file_* fields stay blank.
    """
    prefix = _item_source_prefix(source)
    if prefix and prefix in manifest_index:
        return manifest_index[prefix]
    if len(files) == 1:
        return files[0]
    return None


# ---------------------------------------------------------------------------
# Meta construction
# ---------------------------------------------------------------------------
def _file_fields(entry: Optional[Dict[str, Any]]) -> Tuple[str, str, int, str]:
    """(file_hash, file_mtime, file_size, source_file_path) from a manifest entry."""
    if not entry:
        return "", "", 0, ""
    return (
        str(entry.get("sha256", "")),
        str(entry.get("mtime", "")),
        int(entry.get("size", 0) or 0),
        str(entry.get("path", "")),
    )


def _item_text(item: Dict[str, Any]) -> str:
    """Normalized requirement text if present, else the original (per chosen policy)."""
    return (item.get("normalized_requirement") or item.get("requirement") or "").strip()


def build_item_meta(
    item: Dict[str, Any],
    entry: Optional[Dict[str, Any]],
    *,
    model_name: str,
    extraction_config: Dict[str, Any],
) -> ExtractionMeta:
    fh, mt, sz, path = _file_fields(entry)
    return ExtractionMeta(
        file_hash=fh,
        file_mtime=mt,
        file_size=sz,
        source_file_path=path,
        parser_version=PARSER_VERSION,
        prompt_version=PROMPT_VERSION,
        model_name=model_name,
        requirement_text_hash=requirement_text_hash(_item_text(item)),
        extraction_config=extraction_config,
    )


def build_file_level_meta(
    entry: Dict[str, Any],
    *,
    model_name: str,
    extraction_config: Dict[str, Any],
) -> ExtractionMeta:
    fh, mt, sz, path = _file_fields(entry)
    return ExtractionMeta(
        file_hash=fh,
        file_mtime=mt,
        file_size=sz,
        source_file_path=path,
        parser_version=PARSER_VERSION,
        prompt_version=PROMPT_VERSION,
        model_name=model_name,
        requirement_text_hash=None,  # file level
        extraction_config=extraction_config,
    )


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------
def load_prev_cache(cache_path: Path) -> Dict[str, Any]:
    """Load the previous extraction_cache.json. Missing/unreadable -> empty (cache miss)."""
    if not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def compute_cache(
    clean: Dict[str, Any],
    manifest: Dict[str, Any],
    prev_cache: Dict[str, Any],
) -> Tuple[Dict[str, Any], CacheSummary, List[Tuple[str, SkipDecision]]]:
    """Pure core: build current metas, diff against prev, return (cache_doc, summary, item_decisions)."""
    meta = clean.get("meta", {}) or {}
    model_name = str(meta.get("model", "") or "")
    extraction_config: Dict[str, Any] = {}  # not recorded in clean.json this phase

    items = clean.get("items") or []
    files = manifest.get("files") or []
    manifest_index = build_manifest_index(manifest)

    prev_items = (prev_cache.get("items") or {}) if isinstance(prev_cache.get("items"), dict) else {}
    prev_files = (prev_cache.get("files") or {}) if isinstance(prev_cache.get("files"), dict) else {}

    # --- requirement-level ---
    cur_items: Dict[str, Any] = {}
    item_decisions: List[Tuple[str, SkipDecision]] = []
    reused = 0
    for item in items:
        if (item.get("type") or "") != "requirement":
            continue  # only actionable requirement rows carry a text hash
        req_id = str(item.get("req_id") or "")
        if not req_id:
            continue
        entry = resolve_file_entry(item.get("source", ""), manifest_index, files)
        cur = build_item_meta(item, entry, model_name=model_name, extraction_config=extraction_config)
        prev = ExtractionMeta.from_dict(prev_items[req_id]) if req_id in prev_items else None
        decision = should_skip(prev, cur)
        if decision.skip:
            reused += 1
        cur_items[req_id] = cur.to_dict()
        item_decisions.append((req_id, decision))

    # --- file-level ---
    cur_files: Dict[str, Any] = {}
    file_decisions: List[SkipDecision] = []
    for entry in files:
        name = entry.get("name") or Path(entry.get("path", "")).name
        if not name:
            continue
        cur = build_file_level_meta(entry, model_name=model_name, extraction_config=extraction_config)
        prev = ExtractionMeta.from_dict(prev_files[name]) if name in prev_files else None
        decision = should_skip(prev, cur)
        cur_files[name] = cur.to_dict()
        file_decisions.append(decision)

    summary = CacheSummary.from_decisions(
        file_decisions,
        total_requirements=len(cur_items),
        reused_requirements=reused,
    )

    cache_doc = {
        "case_id": meta.get("case_id", ""),
        "generated_at": now_iso(),
        "parser_version": PARSER_VERSION,
        "prompt_version": PROMPT_VERSION,
        "model_name": model_name,
        "summary": summary.to_dict(),
        "files": cur_files,
        "items": cur_items,
    }
    return cache_doc, summary, item_decisions


def run(case_id: str, runs_root: Path) -> int:
    run_dir = runs_root / case_id
    clean_path = run_dir / "requirements_clean.json"
    manifest_path = run_dir / "manifest.json"
    cache_path = run_dir / CACHE_FILENAME

    if not clean_path.exists():
        print(f"[ERROR] not found: {clean_path}\n"
              f"        Run the pipeline (Steps 1-3) for this case first.")
        return 2

    clean = json.loads(clean_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    if not manifest_path.exists():
        print(f"[WARN] no manifest.json — file_* metadata will be blank: {manifest_path}")

    prev_cache = load_prev_cache(cache_path)
    had_prev = bool(prev_cache)

    cache_doc, summary, item_decisions = compute_cache(clean, manifest, prev_cache)

    cache_path.write_text(json.dumps(cache_doc, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- report ----
    print(f"[OK] Case: {case_id}")
    print(f"[OK] Wrote: {cache_path}")
    print(f"[INFO] prior cache: {'found' if had_prev else 'none (first run - all reprocessed)'}")
    print(f"[INFO] parser_version={PARSER_VERSION} prompt_version={PROMPT_VERSION} "
          f"model_name={cache_doc['model_name'] or '?'}")
    m = summary.to_metrics()
    print("=== Cache Summary ===")
    for label, value in m.items():
        print(f"  {label:<22}: {value}")
    # A few changed items, to make the diff legible for the PM.
    changed = [(rid, d) for rid, d in item_decisions if not d.skip and d.reason == "changed"]
    if changed:
        print(f"--- changed items ({len(changed)}), first 5: ---")
        for rid, d in changed[:5]:
            print(f"  {rid}: {', '.join(d.changed_fields)}")
    return 0


# ---------------------------------------------------------------------------
# Self-test (no files, no LLM): exercises compute_cache end to end in memory.
# ---------------------------------------------------------------------------
def _self_test() -> int:
    manifest = {
        "files": [{
            "name": "spec_REV C.doc",
            "path": "inbound/x/rfq/spec_REV C.doc",
            "size": 100,
            "mtime": "2026-01-01T00:00:00",
            "sha256": "filehash_v1",
        }]
    }
    clean = {
        "meta": {"case_id": "x", "model": "qwen-test"},
        "items": [
            {"req_id": "AI-001", "type": "requirement", "source": "spec_REV C — 第 1 段",
             "requirement": "Shall be x86.", "normalized_requirement": ""},
            {"req_id": "AI-002", "type": "requirement", "source": "spec_REV C — 第 2 段",
             "requirement": "Shall support 64GB.", "normalized_requirement": "The system shall support 64GB RAM."},
            {"req_id": "G-1", "type": "glossary", "source": "spec_REV C", "requirement": "RAM: memory"},
        ],
    }

    # First run: no prior cache -> everything reprocessed, nothing reused.
    doc1, sum1, _ = compute_cache(clean, manifest, {})
    assert sum1.total_requirements == 2, sum1          # glossary excluded
    assert sum1.reused_from_cache == 0 and sum1.reprocessed == 2, sum1
    assert "AI-001" in doc1["items"] and "G-1" not in doc1["items"]
    assert doc1["items"]["AI-001"]["file_hash"] == "filehash_v1"
    # AI-002 hashed its normalized text, not the original.
    assert doc1["items"]["AI-002"]["requirement_text_hash"] == requirement_text_hash(
        "The system shall support 64GB RAM."
    )

    # Second run, no changes -> both reused, file skipped unchanged.
    doc2, sum2, _ = compute_cache(clean, manifest, doc1)
    assert sum2.reused_from_cache == 2 and sum2.reprocessed == 0, sum2
    assert sum2.skipped_unchanged == 1, sum2           # the one source file unchanged

    # Third run: one requirement edited + source bytes changed.
    clean3 = json.loads(json.dumps(clean))
    clean3["items"][0]["requirement"] = "Shall be ARM."
    manifest3 = json.loads(json.dumps(manifest))
    manifest3["files"][0]["sha256"] = "filehash_v2"
    doc3, sum3, decs3 = compute_cache(clean3, manifest3, doc2)
    assert sum3.reused_from_cache == 0 and sum3.reprocessed == 2, sum3   # both file_hash changed
    assert sum3.skipped_unchanged == 0, sum3
    changed_fields = {f for _, d in decs3 for f in d.changed_fields}
    assert "file_hash" in changed_fields and "requirement_text_hash" in changed_fields, decs3

    print("[OK] cache_metadata self-test passed")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Compute + persist + report extraction cache metadata.")
    ap.add_argument("--case", help="Case id (folder under runs/)")
    ap.add_argument("--runs", default="runs", help="Runs root (default: runs)")
    ap.add_argument("--self-test", action="store_true", help="Run in-memory self-test and exit")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()
    if not args.case:
        ap.error("--case is required (or use --self-test)")

    runs_root = Path(args.runs)
    return run(args.case, runs_root)


if __name__ == "__main__":
    raise SystemExit(main())
