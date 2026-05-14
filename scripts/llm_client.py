# -*- coding: utf-8 -*-
"""
Shared LLM client wrapper.

Supports:
  - LLM_PROVIDER=openai   (default) : uses OpenAI API
  - LLM_PROVIDER=internal           : uses an OpenAI-compatible internal endpoint

Env vars:
  LLM_PROVIDER             openai | internal   (default: openai)
  OPENAI_API_KEY           OpenAI key
  OPENAI_MODEL             OpenAI model (default: gpt-4.1-mini)
  INTERNAL_LLM_BASE_URL    e.g. https://llm.internal.company/v1
  INTERNAL_LLM_API_KEY     Internal gateway key
  INTERNAL_LLM_MODEL       Internal model name (required when provider=internal)
  LLM_TIMEOUT_SECONDS      HTTP timeout seconds (default: 60)
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import httpx
from openai import OpenAI


_DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
_DEFAULT_TIMEOUT = 60.0


def _provider() -> str:
    return (os.getenv("LLM_PROVIDER") or "openai").strip().lower()


def _timeout() -> float:
    raw = os.getenv("LLM_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        print(f"[LLM][WARN] invalid LLM_TIMEOUT_SECONDS={raw!r}, using {_DEFAULT_TIMEOUT}")
        return _DEFAULT_TIMEOUT


def is_available() -> bool:
    """Return True if the selected provider has all required env vars."""
    if _provider() == "internal":
        return bool(os.getenv("INTERNAL_LLM_BASE_URL")) and bool(os.getenv("INTERNAL_LLM_API_KEY"))
    return bool(os.getenv("OPENAI_API_KEY"))


def get_model(default: Optional[str] = None) -> str:
    """Resolve model name. CLI --model (passed as default) wins only when env is unset."""
    if _provider() == "internal":
        m = os.getenv("INTERNAL_LLM_MODEL")
        if not m:
            raise RuntimeError(
                "LLM_PROVIDER=internal but INTERNAL_LLM_MODEL is not set."
            )
        return m
    return os.getenv("OPENAI_MODEL") or default or _DEFAULT_OPENAI_MODEL


def get_client(timeout: Optional[float] = None) -> OpenAI:
    """Return an OpenAI-compatible client based on LLM_PROVIDER. Logs provider/base_url/model (no api key)."""
    provider = _provider()
    t = float(timeout) if timeout is not None else _timeout()

    if provider == "internal":
        base_url = os.getenv("INTERNAL_LLM_BASE_URL")
        api_key = os.getenv("INTERNAL_LLM_API_KEY")
        model = os.getenv("INTERNAL_LLM_MODEL") or "(unset)"
        if not base_url or not api_key:
            raise RuntimeError(
                "LLM_PROVIDER=internal requires INTERNAL_LLM_BASE_URL and INTERNAL_LLM_API_KEY."
            )
        print(f"[LLM] provider=internal base_url={base_url} model={model} timeout={t}s")
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=t,
            http_client=httpx.Client(verify=False),
        )

    # default: openai
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set (LLM_PROVIDER=openai).")
    model = os.getenv("OPENAI_MODEL") or _DEFAULT_OPENAI_MODEL
    print(f"[LLM] provider=openai base_url=(default) model={model} timeout={t}s")
    return OpenAI(
        api_key=api_key,
        timeout=t,
        http_client=httpx.Client(verify=False),
    )


def _strip_think(s: str) -> str:
    """Remove <think>...</think> blocks (Qwen3 reasoning output)."""
    return re.sub(r"<think>.*?</think>", "", s, flags=re.DOTALL).strip()


def _strip_fences(s: str) -> str:
    """Remove markdown code fences (```json ... ``` or ``` ... ```)."""
    s = s.strip()
    m = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)```\s*$", s, flags=re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _fix_template_placeholders(s: str) -> str:
    """Fix LLM outputting prompt template literals as bare values.

    Common patterns:
      "chunk": <chunk>   ->  "chunk": 0
      "chunk": CHUNK     ->  "chunk": 0
    These are not valid JSON — the bare words/angle-brackets break parsing.
    String values like "<file>" are already valid JSON and left untouched.
    """
    s = re.sub(r':\s*<chunk>', ': 0', s)
    s = re.sub(r':\s*CHUNK\b', ': 0', s)
    return s


def _safe_preview(s: str, max_len: int = 800) -> str:
    """Return an ASCII-safe preview for logging (avoids UnicodeEncodeError on cp950 consoles)."""
    return s[:max_len].encode("ascii", errors="replace").decode("ascii")


def _dump_debug(raw_text: str, model: str = "") -> None:
    """Save full LLM raw response to a debug file for post-mortem analysis."""
    try:
        from datetime import datetime
        from pathlib import Path
        debug_dir = Path(__file__).parent.parent / "runs" / "_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        model_tag = re.sub(r"[/\\: ]", "_", model)[:30] if model else "unknown"
        path = debug_dir / f"llm_raw_{ts}_{model_tag}.txt"
        path.write_text(raw_text, encoding="utf-8")
        print(f"[DEBUG] Full raw response saved to {path}")
    except Exception as e:
        print(f"[WARN] Could not save debug dump: {e}")


def _repair_truncated_json(s: str) -> Optional[Any]:
    """Try to salvage a truncated JSON by finding the last complete array item.

    Works for the common pattern: {"requirements": [{...}, {... <- cut here
    Strategy: find the last complete object in the array, close ]} after it.
    """
    # Find start of requirements array
    m = re.search(r'("requirements"\s*:\s*\[)', s)
    if not m:
        return None

    prefix = s[:m.end()]
    rest = s[m.end():]

    # Collect complete objects by scanning for balanced braces
    objects_text = ""
    depth = 0
    last_complete_end = 0
    for i, ch in enumerate(rest):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                last_complete_end = i + 1

    if last_complete_end == 0:
        return {"requirements": []}

    trimmed = rest[:last_complete_end]
    candidate = s[:m.start()] + '"requirements": [' + trimmed + ']}'
    # ensure it starts with {
    idx = candidate.find('{')
    if idx > 0:
        candidate = candidate[idx:]
    try:
        obj = json.loads(candidate)
        n = len(obj.get("requirements", []))
        print(f"[WARN] Repaired truncated JSON: salvaged {n} requirements from incomplete output")
        return obj
    except json.JSONDecodeError:
        pass

    return None


def parse_json_response(text: str, model: str = "") -> Any:
    """Parse an LLM text response into JSON.

    Handles: <think> blocks, code fences, extra data after JSON, concatenated objects.
    Logs raw response preview on failure. Raise RuntimeError with clear message.
    """
    if text is None or not str(text).strip():
        raise RuntimeError("LLM response is empty.")
    s = _strip_think(str(text).strip())
    s = _strip_fences(s)
    s = _fix_template_placeholders(s)

    # 1) Direct parse
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # 2) raw_decode: parse the FIRST valid JSON value, ignore trailing data
    decoder = json.JSONDecoder()
    # find the first { or [
    for i, ch in enumerate(s):
        if ch in ('{', '['):
            try:
                obj, _ = decoder.raw_decode(s, i)
                return obj
            except json.JSONDecodeError:
                break

    # 3) Regex fallback
    m = re.search(r"(\{.*\})", s, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # 4) Truncated JSON repair: if output was cut mid-stream, try closing brackets
    if "{" in s:
        truncated = _repair_truncated_json(s)
        if truncated is not None:
            return truncated

    # 5) All parsing failed — dump full response to debug file
    preview = _safe_preview(s)
    model_hint = f" (model={model})" if model else ""
    _dump_debug(text, model)
    raise RuntimeError(
        f"LLM output is not valid JSON{model_hint}.\n"
        f"---RAW RESPONSE (first 800 chars)---\n{preview}\n---END---"
    )
