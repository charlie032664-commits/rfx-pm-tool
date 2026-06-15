# -*- coding: utf-8 -*-
"""Safe LLM smoke test — provider connectivity + tiny synthetic extraction.

Exercises the configured provider (OpenAI or internal) with two tiny calls:
  1. Generation: prompt "Return exactly: OK" (small max_tokens).
  2. Extraction: the real build_prompt -> call_llm_json_with_retry path on a
     SYNTHETIC one-line sample (no RFQ / customer data).

It NEVER runs the full pipeline, sends no customer content, and never prints
secrets (API key values are masked out of any error text). Reports PASS/FAIL and
per-test runtime; exit code 0 = PASS, 1 = FAIL.

Usage (from the scripts/ dir or repo root):
    python scripts/llm_smoke_test.py
    python scripts/llm_smoke_test.py --max-tokens 256
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_loader import load_env, describe_llm_config  # noqa: E402


def _mask(text: object) -> str:
    s = str(text)
    for name, val in os.environ.items():
        if val and len(val) >= 6 and any(h in name.upper() for h in ("KEY", "TOKEN", "SECRET", "PASSWORD")):
            s = s.replace(val, "***")
    return s[:400]


def test_generation(client, model: str, max_tokens: int) -> dict:
    """Tiny 'Return exactly: OK' call. PASS if the call succeeds."""
    t0 = time.time()
    try:
        resp = client.chat.completions.create(
            model=model, temperature=0, max_tokens=max_tokens,
            messages=[{"role": "user", "content": "Return exactly: OK"}],
        )
        txt = (resp.choices[0].message.content or "").strip()
        fin = getattr(resp.choices[0], "finish_reason", None)
        return {"name": "generation", "ok": True, "elapsed": time.time() - t0,
                "detail": f"finish={fin} text={txt[:40] or '(empty)'}"}
    except Exception as e:
        return {"name": "generation", "ok": False, "elapsed": time.time() - t0,
                "detail": f"{type(e).__name__}: {_mask(e)}"}


def test_extraction(client, model: str) -> dict:
    """Synthetic one-line extraction through the real extractor path."""
    t0 = time.time()
    try:
        import extract_requirements_llm as ex
        sample = ("[TABLE 1 ROW 2][ROW_ID=REQ-1] The system shall support at least "
                  "64GB of DDR4 memory. The chassis must include dual redundant power supplies.")
        prompt = ex.build_prompt("smoke_sample.txt", 1, sample)
        data = ex.call_llm_json_with_retry(client, model, prompt, retries=2)
        reqs = data.get("requirements", []) if isinstance(data, dict) else []
        ok = isinstance(data, dict) and len(reqs) >= 1
        return {"name": "extraction", "ok": ok, "elapsed": time.time() - t0,
                "detail": f"json={isinstance(data, dict)} requirements={len(reqs)}"}
    except Exception as e:
        return {"name": "extraction", "ok": False, "elapsed": time.time() - t0,
                "detail": f"{type(e).__name__}: {_mask(e)}"}


def main() -> int:
    ap = argparse.ArgumentParser(description="Safe LLM smoke test (no pipeline, no customer data).")
    ap.add_argument("--max-tokens", type=int, default=256,
                    help="Generation budget (reasoning models need room; default 256).")
    args = ap.parse_args()

    load_env()
    cfg = describe_llm_config()
    print(f"[smoke] provider={cfg.get('provider')} ready={cfg.get('ready')}")
    if not cfg.get("ready"):
        print("[smoke] FAIL: provider not ready (missing required vars). See docs/env_config.md")
        return 1

    import llm_client
    model = llm_client.get_model()
    client = llm_client.get_client()
    print(f"[smoke] model={model}")

    results = [test_generation(client, model, args.max_tokens),
               test_extraction(client, model)]

    all_ok = True
    for r in results:
        flag = "PASS" if r["ok"] else "FAIL"
        all_ok = all_ok and r["ok"]
        print(f"[smoke] {r['name']:<11} {flag}  ({r['elapsed']:.1f}s)  {r['detail']}")

    total = sum(r["elapsed"] for r in results)
    print(f"[smoke] OVERALL {'PASS' if all_ok else 'FAIL'}  total={total:.1f}s")
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
