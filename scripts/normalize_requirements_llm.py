# -*- coding: utf-8 -*-
"""
Phase 4.6A — RFQ requirement normalization (prototype).

Reads runs/<case>/requirements_clean.json and, for items of type="requirement",
asks the LLM to rewrite fragment-style text into a complete, verifiable,
standalone requirement. The result is written back IN PLACE into the same
clean.json with four new fields:

    normalized_requirement   : the rewritten text (empty if no rewrite)
    rewrite_reason           : already_complete | fragment_to_standalone
                              | qa_answer_to_requirement | ambiguous_needs_review
                              | no_rewrite | not_attempted
    rewrite_confidence       : 0.0–1.0
    needs_rewrite_review     : bool — PM should manually check this row

Hard invariants (enforced by assertion):
    - `requirement` (Original) is NEVER modified.
    - `req_id` is NEVER changed.
    - `responses.json` is never touched.

Safety guards:
    - Lexical audit: if normalized text contains numbers / units / model codes /
      standards absent from original + notes + source, confidence is capped at
      0.5 and needs_rewrite_review := True.
    - LLM call failures land the row as rewrite_reason="not_attempted",
      needs_rewrite_review=True, normalized="".
    - Idempotent: rows with a non-empty rewrite_reason (other than
      "not_attempted") are skipped unless --force.

Usage:
    python scripts/normalize_requirements_llm.py --case <case_id>             # sample 10
    python scripts/normalize_requirements_llm.py --case <case_id> --sample 5
    python scripts/normalize_requirements_llm.py --case <case_id> --items AI-058,AI-059
    python scripts/normalize_requirements_llm.py --case <case_id> --all       # full run
    python scripts/normalize_requirements_llm.py --case <case_id> --dry-run   # no LLM, no write
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Reuse the existing LLM wrapper — same OPENAI_API_KEY / LLM_PROVIDER=internal
# environment that extract / enrich already use.
from llm_client import get_client, get_model, is_available, parse_json_response

# Phase 4.7A: load prompt from skills/ instead of inline f-string.
# Add the project root (parent of scripts/) to sys.path so `import skills` works.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skills import load_skill


# ── Paths ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent.parent  # ai_rfx_streamlit_dev/


def _clean_path(case_id: str) -> Path:
    return BASE_DIR / "runs" / case_id / "requirements_clean.json"


def _doc_schema_path(case_id: str) -> Path:
    return BASE_DIR / "inbound" / case_id / "meta" / "doc_schema.json"


# ── Lexical audit ────────────────────────────────────────────────────────────
# Tokens we consider "significant" — they must never appear in normalized text
# unless they are also present in the original / notes / source pool.

_NUMBER_UNIT = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:GB|MB|TB|G|M|K|GHz|MHz|kHz|Hz|V|W|A|"
    r"mm|cm|inch|in|kg|lbs?|°C|°F|RU|U|core|cores|pin|pins|"
    r"ohm|Ω|bps|kbps|Mbps|Gbps|Tbps|x|X)\b",
    re.IGNORECASE,
)
_BARE_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_VERSION_MODEL = re.compile(
    r"\b(?:v|V)?\d+(?:\.\d+){1,3}\b"          # 2.0, v1.2, 1.2.3
    r"|\b[A-Z]{2,}[\-]?\d+[A-Z0-9\-]*\b"      # ST33KTPMQ, USB3, DDR5
)
_STANDARDS = re.compile(
    r"\b(?:"
    r"FCC|CE\s+mark|RoHS|REACH|UL|CSA|"
    r"FIPS(?:\s*1\d{2})?(?:\s*Level\s*\d)?|"
    r"IEC\s*\d+|IEEE\s*\d+|"
    r"DDR[1-9]|PCIe\s*[1-9](?:\.\d)?|"
    r"TPM\s*[1-9](?:\.\d)?|"
    r"IPMI|Redfish|HBA|NVMe|SATA(?:III|II)?|SAS|"
    r"M\.2|SFP\+?|RJ-?45|"
    r"BASE-T|GBASE-T|GbE"
    r")\b",
    re.IGNORECASE,
)


def _extract_significant_tokens(text: str) -> set:
    """Return a set of lower-cased, whitespace-normalized significant tokens."""
    if not text:
        return set()
    tokens: set = set()
    for pat in (_NUMBER_UNIT, _BARE_NUMBER, _VERSION_MODEL, _STANDARDS):
        for m in pat.finditer(text):
            tok = re.sub(r"\s+", "", m.group(0)).lower()
            tokens.add(tok)
    return tokens


def lexical_audit(
    normalized: str, original: str, notes: str, source: str
) -> Tuple[bool, List[str]]:
    """Return (passed, new_tokens). Passed = no significant token in normalized
    is absent from the original / notes / source pool."""
    if not normalized:
        return True, []
    norm_tokens = _extract_significant_tokens(normalized)
    allowed = _extract_significant_tokens(
        " ".join([original or "", notes or "", source or ""])
    )
    new_tokens = sorted(norm_tokens - allowed)
    return (not new_tokens, new_tokens)


# ── Prompt ───────────────────────────────────────────────────────────────────

_SYSTEM = (
    "You are a requirement-normalization assistant for RFQ/RFP documents. "
    "Output STRICT JSON only. No markdown, no prose, no code fences."
)

# Phase 4.7A: prompt body now lives in skills/requirement_normalization.md.
# Loaded once per process (cached); guards / safeguards / JSON parsing /
# LLM call / CLI behaviour are unchanged from the inline-f-string era.
_SKILL = None


def _build_prompt(payload: Dict[str, Any]) -> str:
    global _SKILL
    if _SKILL is None:
        _SKILL = load_skill("requirement_normalization")
    return _SKILL.render(payload)


# ── Per-item processing ──────────────────────────────────────────────────────

_VALID_REASONS = {
    "already_complete",
    "fragment_to_standalone",
    "qa_answer_to_requirement",
    "ambiguous_needs_review",
    "no_rewrite",
}


def _should_skip(item: Dict[str, Any], force: bool) -> bool:
    """Idempotent guard. Skip rows already normalized unless --force."""
    if force:
        return False
    reason = (item.get("rewrite_reason") or "").strip()
    if reason and reason != "not_attempted":
        return True
    if (item.get("normalized_requirement") or "").strip():
        return True
    return False


def _is_eligible_type(item: Dict[str, Any]) -> bool:
    """Only normalize real requirements; skip glossary/note/junk."""
    return (item.get("type") or "").lower() == "requirement"


def normalize_item(
    client, model: str, item: Dict[str, Any], doc_schema: Dict[str, Any]
) -> Dict[str, Any]:
    """Call LLM + safety audit. Returns the 4 new field values (+ optional _llm_error)."""
    original = (item.get("requirement") or "").strip()
    # Best-effort "context" pool — postprocess doesn't preserve raw extract notes,
    # so we fall back to evidence_needed / next_action. For Q&A cases this is
    # imperfect; full Q&A context support is deferred to Phase 4.6B.
    ctx_parts = [
        (item.get("evidence_needed") or "").strip(),
        (item.get("next_action") or "").strip(),
        (item.get("risk_note") or "").strip(),
    ]
    notes = " ".join(p for p in ctx_parts if p).strip()
    src_str = (item.get("source") or "").strip()
    payload = {
        "req_id":               item.get("req_id", ""),
        "original_requirement": original,
        "category":             item.get("category", ""),
        "notes":                notes,
        "source":               src_str,
        "doc_schema_format":    (doc_schema or {}).get("rfq_format", ""),
    }
    prompt = _build_prompt(payload)
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0.1,
            max_tokens=1024,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user",   "content": prompt},
            ],
        )
        raw = (resp.choices[0].message.content or "").strip()
        result = parse_json_response(raw, model=model)
        if not isinstance(result, dict):
            raise RuntimeError(f"LLM returned non-dict JSON: {type(result).__name__}")
    except Exception as e:
        err = str(e).encode("ascii", errors="replace").decode("ascii")[:200]
        return {
            "normalized_requirement": "",
            "rewrite_reason":         "not_attempted",
            "rewrite_confidence":     0.0,
            "needs_rewrite_review":   True,
            "_llm_error":             err,
        }

    normalized = str(result.get("normalized_requirement") or "").strip()
    reason     = str(result.get("rewrite_reason") or "no_rewrite").strip()
    if reason not in _VALID_REASONS:
        reason = "no_rewrite"
    try:
        confidence = float(result.get("rewrite_confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    needs_review = bool(result.get("needs_rewrite_review"))

    # If LLM said already_complete, drop any text it may have written
    if reason == "already_complete":
        normalized = ""

    # Lexical audit (only when there's actual rewrite content)
    audit_new_tokens: List[str] = []
    if normalized:
        passed, audit_new_tokens = lexical_audit(normalized, original, notes, src_str)
        if not passed:
            confidence = min(confidence, 0.5)
            needs_review = True
            if reason in ("fragment_to_standalone", "qa_answer_to_requirement"):
                reason = "ambiguous_needs_review"

    out: Dict[str, Any] = {
        "normalized_requirement": normalized,
        "rewrite_reason":         reason,
        "rewrite_confidence":     round(confidence, 2),
        "needs_rewrite_review":   needs_review,
    }
    if audit_new_tokens:
        out["_audit_new_tokens"] = audit_new_tokens
    return out


# ── Sampling / selection ─────────────────────────────────────────────────────

def select_items(items: List[Dict[str, Any]], args) -> List[int]:
    """Return indices into items[] to process, respecting --items/--all/--sample."""
    eligible_idx = [i for i, it in enumerate(items) if _is_eligible_type(it)]
    if args.items:
        wanted = {x.strip() for x in args.items.split(",") if x.strip()}
        return [i for i in eligible_idx if items[i].get("req_id") in wanted]
    if args.all:
        return eligible_idx
    n = max(1, int(args.sample))
    return eligible_idx[:n]


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Phase 4.6A — RFQ requirement normalization (prototype)"
    )
    ap.add_argument("--case",    required=True, help="Case folder name under runs/")
    ap.add_argument("--sample",  type=int, default=10,
                    help="Sample N eligible items from the top (default 10)")
    ap.add_argument("--items",   default="",
                    help="Comma-separated req_ids to process (overrides --sample)")
    ap.add_argument("--all",     action="store_true",
                    help="Process ALL eligible items (must be explicit)")
    ap.add_argument("--force",   action="store_true",
                    help="Re-normalize rows that already have rewrite_reason set")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would happen; do not call LLM, do not write")
    args = ap.parse_args()

    clean_p = _clean_path(args.case)
    if not clean_p.exists():
        raise SystemExit(f"clean.json not found: {clean_p}")

    data = json.loads(clean_p.read_text(encoding="utf-8"))
    items = data.get("items", [])
    if not items:
        print(f"[NORMALIZE] {args.case}: 0 items, nothing to do")
        return

    ds_p = _doc_schema_path(args.case)
    doc_schema: Dict[str, Any] = {}
    if ds_p.exists():
        try:
            doc_schema = json.loads(ds_p.read_text(encoding="utf-8"))
        except Exception:
            pass

    selected_idx = select_items(items, args)
    if not selected_idx:
        print("[NORMALIZE] No eligible items match selection.")
        return

    if not args.all and not args.items:
        print(f"[NORMALIZE] sample mode: processing first {len(selected_idx)} eligible items")
        print(f"            (use --all for full run, --items 'AI-1,AI-2' for specific, "
              f"or --sample N to change size)")

    client = model = None
    if not args.dry_run:
        if not is_available():
            raise SystemExit(
                "LLM not configured. Set OPENAI_API_KEY (provider=openai) or "
                "LLM_PROVIDER=internal with INTERNAL_LLM_BASE_URL/INTERNAL_LLM_API_KEY/"
                "INTERNAL_LLM_MODEL."
            )
        client = get_client()
        model = get_model()
        print(f"[NORMALIZE] LLM model={model}  doc_schema.rfq_format="
              f"{doc_schema.get('rfq_format', '?')!r}")

    stats: Dict[str, int] = {
        "total_selected":          len(selected_idx),
        "skipped_idempotent":      0,
        "processed":               0,
        "already_complete":        0,
        "fragment_to_standalone":  0,
        "qa_answer_to_requirement": 0,
        "ambiguous_needs_review":  0,
        "no_rewrite":              0,
        "not_attempted":           0,
        "needs_review_set":        0,
        "low_confidence_lt_06":    0,
        "audit_flagged_hallucination": 0,
        "req_id_changes":          0,
        "original_changes":        0,
    }
    audit_log: List[Dict[str, Any]] = []

    for idx in selected_idx:
        item = items[idx]
        rid_before = item.get("req_id", "")
        orig_before = item.get("requirement", "")

        if _should_skip(item, args.force):
            stats["skipped_idempotent"] += 1
            continue

        if args.dry_run:
            print(f"  [DRY] {rid_before:14}  type={item.get('type','?'):11}  "
                  f"orig={orig_before[:70]!r}")
            stats["processed"] += 1
            continue

        result = normalize_item(client, model, item, doc_schema)

        # SAFETY INVARIANTS — assertion failures will print and abort the run
        if item.get("req_id", "") != rid_before:
            stats["req_id_changes"] += 1
            raise SystemExit(f"INVARIANT: req_id changed for {rid_before!r}")
        if item.get("requirement", "") != orig_before:
            stats["original_changes"] += 1
            raise SystemExit(f"INVARIANT: original requirement changed for {rid_before!r}")

        item["normalized_requirement"] = result["normalized_requirement"]
        item["rewrite_reason"]         = result["rewrite_reason"]
        item["rewrite_confidence"]     = result["rewrite_confidence"]
        item["needs_rewrite_review"]   = result["needs_rewrite_review"]

        stats["processed"] += 1
        rr = result["rewrite_reason"]
        stats[rr] = stats.get(rr, 0) + 1
        if result["needs_rewrite_review"]:
            stats["needs_review_set"] += 1
        if 0 < result["rewrite_confidence"] < 0.6:
            stats["low_confidence_lt_06"] += 1
        if result.get("_audit_new_tokens"):
            stats["audit_flagged_hallucination"] += 1

        audit_log.append({
            "req_id":     rid_before,
            "original":   orig_before,
            "normalized": result["normalized_requirement"],
            "reason":     rr,
            "confidence": result["rewrite_confidence"],
            "review":     result["needs_rewrite_review"],
            "audit_tokens": result.get("_audit_new_tokens", []),
            "err":        result.get("_llm_error", ""),
        })

        norm_preview = ("<empty>" if not result["normalized_requirement"]
                        else result["normalized_requirement"][:70] + "...")
        print(f"  [{stats['processed']}/{len(selected_idx)}] {rid_before:14}  "
              f"reason={rr:24}  conf={result['rewrite_confidence']:.2f}  "
              f"review={str(result['needs_rewrite_review']):5}  "
              f"norm={norm_preview}")

    if not args.dry_run and stats["processed"] > 0:
        clean_p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n[OK] Wrote {clean_p}")

    print()
    print("=== Summary ===")
    for k, v in stats.items():
        print(f"  {k:30}: {v}")

    # Hallucination details (if any)
    flagged = [a for a in audit_log if a["audit_tokens"]]
    if flagged:
        print()
        print(f"=== Hallucination audit: {len(flagged)} row(s) flagged ===")
        for a in flagged:
            print(f"  {a['req_id']}: new tokens {a['audit_tokens']}")
            print(f"    original  : {a['original'][:100]!r}")
            print(f"    normalized: {a['normalized'][:100]!r}")

    # Errors (if any)
    errs = [a for a in audit_log if a["err"]]
    if errs:
        print()
        print(f"=== LLM errors: {len(errs)} row(s) ===")
        for a in errs:
            print(f"  {a['req_id']}: {a['err']}")

    if stats["req_id_changes"] != 0 or stats["original_changes"] != 0:
        print("\n⚠ INVARIANT VIOLATION detected.")


if __name__ == "__main__":
    main()
