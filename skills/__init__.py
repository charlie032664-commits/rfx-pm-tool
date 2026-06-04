# -*- coding: utf-8 -*-
"""Skill layer (Phase 4.7A prototype).

A Skill is a prompt template + metadata stored as a Markdown file with a
YAML frontmatter block. The body of the file is rendered into a final
prompt string by substituting `{payload_json}` with the caller's payload
serialised as indented JSON, then `.strip()`-ed (matching the legacy
in-line f-string behaviour byte-for-byte).

Skill schema (frontmatter):
    skill_id              — unique short id, must match the basename
    description           — one-line summary
    version               — int; bump on prompt changes
    applies_to            — free-form dict (rfq_format, trigger, ...)
    input_fields          — list of payload field names
    output_schema         — dict describing the LLM's JSON response
    llm                   — model / temperature / max_tokens hints
    guards                — list of post-LLM safety checks the caller MUST run
    fallback              — what to do on LLM error or invalid JSON
    needs_review_triggers — when callers should set needs_review=True

The frontmatter parser is hand-rolled (no python-frontmatter dependency);
YAML parsing uses pyyaml's safe_load (already a project dependency).
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

SKILLS_DIR = Path(__file__).resolve().parent


@dataclass
class Skill:
    """A prompt template plus declarative metadata.

    The render() method substitutes `{payload_json}` with json.dumps(payload,
    ensure_ascii=False, indent=2) and strips leading/trailing whitespace.
    This matches the byte-for-byte output of the legacy inline f-string.
    """
    skill_id:              str
    description:           str = ""
    version:               int = 1
    applies_to:            Dict[str, Any] = field(default_factory=dict)
    input_fields:          List[str] = field(default_factory=list)
    output_schema:         Dict[str, Any] = field(default_factory=dict)
    llm:                   Dict[str, Any] = field(default_factory=dict)
    guards:                List[str] = field(default_factory=list)
    fallback:              Dict[str, Any] = field(default_factory=dict)
    needs_review_triggers: List[str] = field(default_factory=list)
    prompt_body:           str = ""
    source_path:           str = ""

    def render(self, payload: Dict[str, Any]) -> str:
        """Substitute payload JSON into the prompt template and strip()."""
        payload_json = _json.dumps(payload, ensure_ascii=False, indent=2)
        return self.prompt_body.replace("{payload_json}", payload_json).strip()


# ── frontmatter parser ───────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """Hand-roll parser for '---\\n<yaml>\\n---\\n<body>'.

    Returns (meta, body). If the document has no opening '---' or no closing
    '---', returns ({}, original_text).
    """
    if not text.startswith("---"):
        return {}, text
    # Split off the first line ('---') from the rest.
    parts = text.split("\n", 1)
    if len(parts) < 2:
        return {}, text
    rest = parts[1]
    # Find the closing '---' delimiter (must be on its own line).
    closer_idx = rest.find("\n---")
    if closer_idx < 0:
        return {}, text
    yaml_block = rest[:closer_idx]
    body = rest[closer_idx + len("\n---"):]
    # Skip the single newline that follows the closing '---', if present.
    if body.startswith("\n"):
        body = body[1:]
    try:
        meta = yaml.safe_load(yaml_block) or {}
        if not isinstance(meta, dict):
            raise ValueError(f"frontmatter is not a mapping: {type(meta).__name__}")
    except yaml.YAMLError as exc:
        raise ValueError(f"Skill frontmatter is not valid YAML: {exc}") from exc
    return meta, body


# ── public loader (with simple cache) ────────────────────────────────────────

_CACHE: Dict[str, Skill] = {}


def load_skill(name: str) -> Skill:
    """Load skills/<name>.md and return a Skill instance.

    Caches by name; the same Skill object is returned on repeated calls.
    Raises FileNotFoundError if the .md file is missing and ValueError
    if the frontmatter is invalid or the prompt body is empty.
    """
    if name in _CACHE:
        return _CACHE[name]

    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"Skill not found: {path}")
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_frontmatter(raw)

    skill = Skill(
        skill_id              = str(meta.get("skill_id") or "").strip(),
        description           = str(meta.get("description") or "").strip(),
        version               = int(meta.get("version") or 1),
        applies_to            = meta.get("applies_to") or {},
        input_fields          = list(meta.get("input_fields") or []),
        output_schema         = meta.get("output_schema") or {},
        llm                   = meta.get("llm") or {},
        guards                = list(meta.get("guards") or []),
        fallback              = meta.get("fallback") or {},
        needs_review_triggers = list(meta.get("needs_review_triggers") or []),
        prompt_body           = body,
        source_path           = str(path),
    )

    if not skill.skill_id:
        raise ValueError(f"Skill at {path} missing 'skill_id' in frontmatter")
    if skill.skill_id != name:
        raise ValueError(
            f"Skill at {path}: frontmatter skill_id={skill.skill_id!r} "
            f"does not match filename {name!r}"
        )
    if not skill.prompt_body.strip():
        raise ValueError(f"Skill at {path} has empty prompt body")

    _CACHE[name] = skill
    return skill


def list_skills() -> List[str]:
    """Return sorted list of available skill names (.md basenames)."""
    return sorted(p.stem for p in SKILLS_DIR.glob("*.md"))


def clear_cache() -> None:
    """Drop the in-process skill cache. Mainly for tests."""
    _CACHE.clear()


__all__ = ["Skill", "load_skill", "list_skills", "clear_cache", "SKILLS_DIR"]
