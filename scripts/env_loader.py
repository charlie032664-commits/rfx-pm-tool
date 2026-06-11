# -*- coding: utf-8 -*-
"""Load a local .env into os.environ — safe, optional, no hard dependency.

Why this exists: app.py launches the pipeline scripts as subprocesses that
inherit os.environ. Nothing in the project ever called load_dotenv, so the
LLM_* / OPENAI_* vars had to be exported in whatever shell started Streamlit.
After the public-release sanitize blanked the internal launcher values, a
Full Pipeline run could reach the extractor with no LLM config and fail at
is_available(). This loader makes a repo-root .env an optional single source of
truth while keeping every existing mechanism intact.

Guarantees:
  - OS environment ALWAYS wins. A var already present in os.environ is never
    overwritten by .env (so launchers / setx / CI keep precedence). Backward
    compatible: with no .env file this is a no-op.
  - No secrets are printed. Diagnostics report presence (bool) only.
  - python-dotenv is used when installed; otherwise a minimal KEY=VALUE parser
    handles the common cases (surrounding quotes, optional `export ` prefix,
    # comments, blank lines).
  - Idempotent and safe to call from multiple entry points.

Usage:
    from env_loader import load_env, describe_llm_config
    load_env()                       # populate os.environ from repo-root .env
    cfg = describe_llm_config()      # {'provider', 'required', 'present', 'ready'}
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional

# repo root = parent of scripts/
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_ENV = _REPO_ROOT / ".env"

# Set by load_env() to the file it read (for non-secret diagnostics). None until
# a .env is actually found + parsed.
_loaded_from: Optional[str] = None

# Substrings that mark a key as secret — such values are redacted in any output.
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD")

_LLM_KEYS_BY_PROVIDER = {
    "internal": ["INTERNAL_LLM_BASE_URL", "INTERNAL_LLM_API_KEY", "INTERNAL_LLM_MODEL"],
    "openai": ["OPENAI_API_KEY"],
}


def _parse_env_text(text: str) -> Dict[str, str]:
    """Minimal .env parser (fallback when python-dotenv is not installed)."""
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        # Strip a single pair of matching surrounding quotes.
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        out[key] = val
    return out


def load_env(path: Optional[Path] = None, *, override: bool = False) -> List[str]:
    """Load .env into os.environ. Returns the list of keys actually set.

    OS environment takes precedence unless override=True (default False), so an
    already-exported var is preserved. A missing file is a no-op (returns []).
    Never raises on a malformed line — best effort.
    """
    global _loaded_from
    env_path = Path(path) if path else _DEFAULT_ENV
    if not env_path.exists():
        return []

    pairs: Dict[str, str] = {}
    try:
        # Prefer python-dotenv when available (handles more edge cases).
        from dotenv import dotenv_values  # type: ignore
        pairs = {k: v for k, v in dotenv_values(str(env_path)).items() if v is not None}
    except Exception:
        try:
            pairs = _parse_env_text(env_path.read_text(encoding="utf-8"))
        except Exception:
            return []

    set_keys: List[str] = []
    for key, val in pairs.items():
        if override or key not in os.environ:
            os.environ[key] = val
            set_keys.append(key)
    _loaded_from = str(env_path)
    return set_keys


def describe_llm_config() -> Dict[str, object]:
    """Non-secret snapshot of LLM config: provider + which required vars exist.

    Returns booleans / names only — never actual values. Mirrors the provider
    logic in llm_client.is_available() so the diagnostic matches reality.
    """
    provider = (os.environ.get("LLM_PROVIDER") or "openai").strip().lower()
    required = _LLM_KEYS_BY_PROVIDER.get(provider, _LLM_KEYS_BY_PROVIDER["openai"])
    present = {k: bool(os.environ.get(k)) for k in required}
    return {
        "provider": provider,
        "required": required,
        "present": present,
        "ready": all(present.values()),
        "loaded_from": _loaded_from,
    }


if __name__ == "__main__":
    # Safe diagnostic: prints presence only, never secret values.
    load_env()
    cfg = describe_llm_config()
    print(f"[env] loaded_from = {cfg['loaded_from'] or '(no .env found - using OS env only)'}")
    print(f"[env] provider    = {cfg['provider']}")
    for _k in cfg["required"]:                       # type: ignore[union-attr]
        _ok = cfg["present"][_k]                      # type: ignore[index]
        print(f"[env] {_k:<22} = {'present' if _ok else 'MISSING'}")
    print(f"[env] ready       = {cfg['ready']}")
