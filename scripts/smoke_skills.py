# -*- coding: utf-8 -*-
"""Phase 4.7A skill-layer smoke test.

Verifies every skill in skills/ loads cleanly:
  - frontmatter parses without error
  - required fields exist (skill_id, input_fields, output_schema, guards, ...)
  - prompt body is non-empty
  - render(payload) succeeds for a representative payload
  - rendered prompt contains the key constraint strings and payload values

Exits with status 0 on success, 1 on any failure.

Usage:
  python scripts/smoke_skills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Allow `import skills` when run as `python scripts/smoke_skills.py` from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from skills import list_skills, load_skill, clear_cache


# ── Per-skill verification specs ─────────────────────────────────────────────
# Each entry says: for this skill, render with this payload and require these
# substrings to appear in the output.

SKILL_CHECKS = {
    "requirement_normalization": {
        "required_frontmatter": [
            "skill_id", "input_fields", "output_schema",
            "guards", "fallback", "needs_review_triggers",
        ],
        "sample_payload": {
            "req_id":               "AI-058",
            "original_requirement": "x86 CPU, AMD or Intel CPU",
            "category":             "Platform",
            "notes":                "",
            "source":               "PRD isetta - chunk 23",
            "doc_schema_format":    "plain_text",
        },
        "must_contain": [
            "嚴格約束",                 # key constraint header
            "Output strict JSON only",  # JSON output marker
            "Now process this input",   # payload anchor
            "x86 CPU, AMD or Intel CPU",  # payload value embedded
            "AI-058",                   # req_id embedded
            "plain_text",               # doc_schema_format embedded
            "lexical",  # not in prompt — should this fail? actually no, "lexical" is in guards
        ],
        "must_NOT_contain": [
            "{payload_json}",  # placeholder must have been substituted
            "{{",              # no f-string escape residue
            "}}",
        ],
    },
}

# Patch: the "lexical" check above tests a guard *metadata* string, not the prompt
# body. Remove it from must_contain to avoid a false-positive failure.
SKILL_CHECKS["requirement_normalization"]["must_contain"].remove("lexical")


def main() -> int:
    failed = 0
    passed = 0

    available = list_skills()
    print(f"[SMOKE] Available skills in skills/: {available}")
    if not available:
        print("[FAIL] No skills found in skills/")
        return 1

    clear_cache()
    for name in available:
        print(f"\n=== Skill: {name} ===")

        # 1. Load
        try:
            skill = load_skill(name)
            print(f"  [OK ] load_skill('{name}')  skill_id={skill.skill_id!r} version={skill.version}")
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] load_skill('{name}') raised: {exc}")
            failed += 1
            continue

        # 2. Body non-empty
        if not skill.prompt_body.strip():
            print(f"  [FAIL] prompt_body is empty")
            failed += 1
            continue
        print(f"  [OK ] prompt_body length = {len(skill.prompt_body)} chars")
        passed += 1

        # 3. Verify against per-skill spec, if present
        spec = SKILL_CHECKS.get(name)
        if spec is None:
            print(f"  [WARN] no per-skill check spec for '{name}' — skipping deep checks")
            continue

        # 3a. Required frontmatter fields
        for f in spec["required_frontmatter"]:
            v = getattr(skill, f, None)
            ok = v not in (None, "", [], {})
            print(f"  [{'OK ' if ok else 'FAIL'}] frontmatter field '{f}' present (value: {v!r})")
            if ok:
                passed += 1
            else:
                failed += 1

        # 3b. Render with sample payload
        try:
            rendered = skill.render(spec["sample_payload"])
            print(f"  [OK ] render() succeeded — output length = {len(rendered)} chars")
            passed += 1
        except Exception as exc:
            print(f"  [FAIL] render() raised: {exc}")
            failed += 1
            continue

        # 3c. Must-contain substrings
        for s in spec["must_contain"]:
            ok = s in rendered
            print(f"  [{'OK ' if ok else 'FAIL'}] must_contain {s!r}")
            if ok:
                passed += 1
            else:
                failed += 1

        # 3d. Must-NOT-contain substrings
        for s in spec["must_NOT_contain"]:
            ok = s not in rendered
            print(f"  [{'OK ' if ok else 'FAIL'}] must_NOT_contain {s!r}")
            if ok:
                passed += 1
            else:
                failed += 1

    print()
    print("=" * 60)
    print(f"SMOKE SUMMARY: {passed} passed, {failed} failed "
          f"(across {len(available)} skill(s))")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
