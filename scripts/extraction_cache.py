# -*- coding: utf-8 -*-
"""Incremental extraction cache — metadata, skip-unchanged logic, UI summary.

Phase 8 (feature/rfx-next-functions) scaffold. This module DEFINES the data
model and decision logic that will later let requirement extraction become
incremental: unchanged source files / requirements are reused from a prior run
instead of being re-sent to the LLM.

Scope of THIS step (deliberately narrow):
  - Define the per-extraction metadata schema (``ExtractionMeta``).
  - Define the cache key + skip-unchanged decision (``cache_signature`` /
    ``should_skip``).
  - Define the UI summary model (``CacheSummary``).

NON-goals of this step (do NOT do here):
  - It does NOT call the LLM, read RFQ documents, or change how requirements
    are extracted. ``extract_requirements_llm.py`` is untouched.
  - Wiring this into the extractor / ``run_case.py`` / ``app.py`` is a later,
    separate commit.

Keeping this module pure (data + pure functions, no extraction side effects)
means it can be unit-tested in isolation and reviewed without risk to the
core pipeline. It mirrors the conservative-default style of
``file_selection.load_excluded``: when in doubt, prefer reprocessing (cache
miss) over silently reusing a stale result.

Usage (intended, once wired in a later step):
    from extraction_cache import (
        PARSER_VERSION, PROMPT_VERSION,
        build_file_meta, requirement_text_hash,
        should_skip, CacheSummary,
    )

    cur = build_file_meta(fp, model_name=model, extraction_config=cfg)
    decision = should_skip(prev_meta_by_path.get(str(fp)), cur)
    if decision.skip:
        reqs = cached_reqs_for(fp)        # reuse
    else:
        reqs = extract(fp)               # reprocess (cache miss / changed)

    summary = CacheSummary.from_decisions(decisions, total_requirements=n)
    summary.to_metrics()                  # -> dict for st.metric cards
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------
# These gate the cache: bump a version and every prior cache entry that carries
# the old value is treated as changed (cache miss), forcing a clean reprocess.
#
#   PARSER_VERSION — bump when the deterministic document->blocks->chunks path
#                    changes (read_docx_blocks / read_xlsx_blocks / chunking /
#                    req_id detection). Output shape changes even with the same
#                    bytes and the same prompt.
#   PROMPT_VERSION — bump when build_prompt() / the extraction instructions
#                    change. Same bytes + same parser can yield different
#                    requirements because the LLM was asked differently.
#
# Semantic-ish strings (not enforced); compared by equality only.
PARSER_VERSION: str = "1.0.0"
PROMPT_VERSION: str = "1.0.0"

# Rough cost model for "estimated runtime saved" in the UI summary. This is an
# advisory display number only — never used for correctness. Tune against real
# run logs later; kept conservative so we under-promise.
DEFAULT_SECONDS_PER_REQUIREMENT: float = 1.5


# ---------------------------------------------------------------------------
# Hashing helpers (mirror run_case.sha256_file; duplicated to keep this module
# import-light — run_case pulls in openai/httpx at module load).
# ---------------------------------------------------------------------------
def sha256_bytes(data: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    """SHA-256 hex digest of text, UTF-8 encoded.

    Whitespace is NOT normalized here — callers that want whitespace-insensitive
    matching should normalize before hashing. Kept literal so an edited
    requirement is reliably seen as changed.
    """
    return sha256_bytes((text or "").encode("utf-8"))


def sha256_file(p: Path, buf_size: int = 1024 * 1024) -> str:
    """SHA-256 hex digest of a file, streamed in chunks.

    Matches run_case.sha256_file so a hash computed in either place is
    comparable for the same bytes.
    """
    h = hashlib.sha256()
    with Path(p).open("rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def requirement_text_hash(text: str) -> str:
    """Stable hash of one requirement's text, for per-requirement reuse.

    Thin alias over sha256_text so the requirement-level intent is explicit at
    call sites and the normalization policy lives in one place.
    """
    return sha256_text(text)


def canonical_config(config: Optional[Mapping[str, Any]]) -> str:
    """Deterministic JSON string for an extraction-config dict.

    Sorted keys so logically-equal configs hash identically regardless of key
    order. None / empty -> "{}".
    """
    if not config:
        return "{}"
    return json.dumps(config, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# Metadata schema
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ExtractionMeta:
    """Identity of one extraction unit, persisted alongside cached results.

    A unit is "one source file extracted under one parser+prompt+model+config".
    ``requirement_text_hash`` is optional: at the FILE level (deciding whether
    to re-run the LLM for a file) it is None; at the REQUIREMENT level (deciding
    whether a single requirement's downstream work can be reused) it is set.

    Fields (exactly the metadata enumerated for this step):
      file_hash             SHA-256 of the source file bytes.
      file_mtime            ISO-8601 modification time (advisory; hash is truth).
      file_size             Size in bytes (advisory / quick change hint).
      source_file_path      Path of the source file (string; identity/display).
      parser_version        PARSER_VERSION captured at extraction time.
      prompt_version        PROMPT_VERSION captured at extraction time.
      model_name            LLM model id used (e.g. resolved get_model()).
      requirement_text_hash Per-requirement text hash, or None at file level.
      extraction_config     Knobs that affect output (max_chars, group_size,
                            retries, ...). Compared via canonical_config.
    """

    file_hash: str
    file_mtime: str
    file_size: int
    source_file_path: str
    parser_version: str
    prompt_version: str
    model_name: str
    requirement_text_hash: Optional[str] = None
    extraction_config: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict, JSON-serializable, for persistence in run metadata."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ExtractionMeta":
        """Rebuild from a persisted dict; unknown keys ignored, missing -> defaults."""
        return cls(
            file_hash=str(data.get("file_hash", "")),
            file_mtime=str(data.get("file_mtime", "")),
            file_size=int(data.get("file_size", 0) or 0),
            source_file_path=str(data.get("source_file_path", "")),
            parser_version=str(data.get("parser_version", "")),
            prompt_version=str(data.get("prompt_version", "")),
            model_name=str(data.get("model_name", "")),
            requirement_text_hash=(
                data.get("requirement_text_hash")
                if data.get("requirement_text_hash") is not None
                else None
            ),
            extraction_config=dict(data.get("extraction_config") or {}),
        )


def build_file_meta(
    file_path: Path,
    *,
    model_name: str,
    extraction_config: Optional[Mapping[str, Any]] = None,
    parser_version: str = PARSER_VERSION,
    prompt_version: str = PROMPT_VERSION,
) -> ExtractionMeta:
    """Construct file-level ExtractionMeta by stat-ing + hashing the file.

    requirement_text_hash is left None (not known until extraction produces
    requirement text). Raises FileNotFoundError if the path is missing — a
    missing source is a caller bug, not a silent cache miss.
    """
    p = Path(file_path)
    stat = p.stat()  # propagate FileNotFoundError
    return ExtractionMeta(
        file_hash=sha256_file(p),
        file_mtime=datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
        file_size=stat.st_size,
        source_file_path=str(p),
        parser_version=parser_version,
        prompt_version=prompt_version,
        model_name=model_name,
        requirement_text_hash=None,
        extraction_config=dict(extraction_config or {}),
    )


# ---------------------------------------------------------------------------
# Cache key + skip-unchanged decision
# ---------------------------------------------------------------------------
# The skip basis required for this step: source file hash + requirement text
# hash + parser version + prompt version. model_name and extraction_config are
# folded in by default because a model swap or a knob change can alter output
# for identical bytes — reusing across them would be a correctness bug, not a
# speed win. Callers can drop them via include_model_and_config=False if a
# future policy decides otherwise.
_CORE_FIELDS = ("file_hash", "requirement_text_hash", "parser_version", "prompt_version")


def cache_signature(meta: ExtractionMeta, *, include_model_and_config: bool = True) -> str:
    """Stable signature of an ExtractionMeta's identity.

    Two metas with equal signatures describe the same extraction and are
    eligible for reuse. requirement_text_hash=None participates as the literal
    token "-" so two file-level metas (both None) still compare equal.
    """
    parts: List[str] = [
        f"file_hash={meta.file_hash}",
        f"requirement_text_hash={meta.requirement_text_hash or '-'}",
        f"parser_version={meta.parser_version}",
        f"prompt_version={meta.prompt_version}",
    ]
    if include_model_and_config:
        parts.append(f"model_name={meta.model_name}")
        parts.append(f"extraction_config={canonical_config(meta.extraction_config)}")
    return sha256_text("|".join(parts))


@dataclass(frozen=True)
class SkipDecision:
    """Outcome of comparing a cached meta to the current one.

    skip=True   -> reuse the cached result (cache hit, unchanged).
    skip=False  -> reprocess. ``reason`` is a short machine token
                   ("no_cache" | "changed" | "unchanged"); ``changed_fields``
                   lists which identity fields differ (empty when skip=True).
    """

    skip: bool
    reason: str
    changed_fields: Sequence[str] = ()


def _diff_fields(prev: ExtractionMeta, cur: ExtractionMeta, *, include_model_and_config: bool) -> List[str]:
    fields = list(_CORE_FIELDS)
    if include_model_and_config:
        fields += ["model_name", "extraction_config"]
    changed: List[str] = []
    for name in fields:
        a = getattr(prev, name)
        b = getattr(cur, name)
        if name == "extraction_config":
            a, b = canonical_config(a), canonical_config(b)
        if a != b:
            changed.append(name)
    return changed


def should_skip(
    prev: Optional[ExtractionMeta],
    cur: ExtractionMeta,
    *,
    include_model_and_config: bool = True,
) -> SkipDecision:
    """Decide whether ``cur`` can reuse a cached result described by ``prev``.

    Conservative: a missing prior (None) is always a cache miss. Equality is by
    cache_signature; changed_fields is computed only on a miss for display/debug.
    """
    if prev is None:
        return SkipDecision(skip=False, reason="no_cache", changed_fields=())
    same = cache_signature(prev, include_model_and_config=include_model_and_config) == \
        cache_signature(cur, include_model_and_config=include_model_and_config)
    if same:
        return SkipDecision(skip=True, reason="unchanged", changed_fields=())
    changed = _diff_fields(prev, cur, include_model_and_config=include_model_and_config)
    return SkipDecision(skip=False, reason="changed", changed_fields=tuple(changed))


# ---------------------------------------------------------------------------
# UI summary model
# ---------------------------------------------------------------------------
@dataclass
class CacheSummary:
    """Tallies for the incremental-extraction UI summary.

    Mirrors the existing st.metric card style in app.py (e.g. the normalize
    "This run" row). ``to_metrics`` returns label->value pairs ready to drop
    into columns.

      total_requirements   All requirements in the final result this run.
      reused_from_cache    Requirements taken verbatim from a prior run.
      reprocessed          Requirements (re)produced by the LLM this run.
      skipped_unchanged    Source files skipped entirely (unchanged) — file
                          granularity, distinct from per-requirement reuse.
      estimated_runtime_saved_sec  Advisory: reused * seconds_per_requirement.
    """

    total_requirements: int = 0
    reused_from_cache: int = 0
    reprocessed: int = 0
    skipped_unchanged: int = 0
    estimated_runtime_saved_sec: float = 0.0

    @classmethod
    def from_decisions(
        cls,
        file_decisions: Sequence[SkipDecision],
        *,
        total_requirements: int,
        reused_requirements: int = 0,
        reprocessed_requirements: Optional[int] = None,
        seconds_per_requirement: float = DEFAULT_SECONDS_PER_REQUIREMENT,
    ) -> "CacheSummary":
        """Build a summary from per-file skip decisions + requirement tallies.

        file_decisions drives ``skipped_unchanged`` (count of skip=True). The
        requirement-level split (reused vs reprocessed) is passed in by the
        caller, since only the extractor knows which requirements it reused.
        reprocessed defaults to total - reused when not given.
        """
        skipped = sum(1 for d in file_decisions if d.skip)
        reprocessed = (
            reprocessed_requirements
            if reprocessed_requirements is not None
            else max(0, total_requirements - reused_requirements)
        )
        return cls(
            total_requirements=total_requirements,
            reused_from_cache=reused_requirements,
            reprocessed=reprocessed,
            skipped_unchanged=skipped,
            estimated_runtime_saved_sec=round(
                max(0, reused_requirements) * max(0.0, seconds_per_requirement), 1
            ),
        )

    def to_metrics(self) -> Dict[str, Any]:
        """label -> value, ready for st.metric cards (insertion order = display order)."""
        return {
            "Total requirements": self.total_requirements,
            "Reused from cache": self.reused_from_cache,
            "Reprocessed": self.reprocessed,
            "Skipped unchanged": self.skipped_unchanged,
            "Est. runtime saved": _fmt_seconds(self.estimated_runtime_saved_sec),
        }

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serializable dict for persistence in run metadata."""
        return asdict(self)


def _fmt_seconds(sec: float) -> str:
    """Human-readable duration for the runtime-saved card (e.g. '1m 12s')."""
    s = int(round(sec))
    if s < 60:
        return f"{s}s"
    m, r = divmod(s, 60)
    return f"{m}m {r}s"


# ---------------------------------------------------------------------------
# Smoke self-test (no LLM, no I/O of real RFQ docs). Run:  python extraction_cache.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    base = ExtractionMeta(
        file_hash="aaa",
        file_mtime="2026-06-11T00:00:00",
        file_size=123,
        source_file_path="rfq/a.docx",
        parser_version=PARSER_VERSION,
        prompt_version=PROMPT_VERSION,
        model_name="gpt-4.1-mini",
        extraction_config={"max_chars": 1200, "group_size": 4},
    )

    # 1) Unchanged -> skip.
    assert should_skip(base, base).skip, "identical meta must skip"

    # 2) Different file bytes -> reprocess, file_hash flagged.
    changed_bytes = ExtractionMeta(**{**base.to_dict(), "file_hash": "bbb"})
    d = should_skip(base, changed_bytes)
    assert not d.skip and "file_hash" in d.changed_fields, d

    # 3) Prompt version bump invalidates everything.
    bumped = ExtractionMeta(**{**base.to_dict(), "prompt_version": "1.1.0"})
    assert not should_skip(base, bumped).skip, "prompt bump must reprocess"

    # 4) mtime/size differ but bytes+versions identical -> still skip
    #    (hash is truth; mtime is advisory only).
    touched = ExtractionMeta(**{**base.to_dict(), "file_mtime": "2030-01-01T00:00:00", "file_size": 999})
    assert should_skip(base, touched).skip, "mtime/size alone must not invalidate"

    # 5) No prior cache -> miss.
    assert not should_skip(None, base).skip

    # 6) Config knob change invalidates by default, but not when opted out.
    cfg2 = ExtractionMeta(**{**base.to_dict(), "extraction_config": {"max_chars": 800, "group_size": 4}})
    assert not should_skip(base, cfg2).skip
    assert should_skip(base, cfg2, include_model_and_config=False).skip

    # 7) Summary math + formatting.
    summ = CacheSummary.from_decisions(
        [SkipDecision(True, "unchanged"), SkipDecision(False, "changed", ("file_hash",))],
        total_requirements=100,
        reused_requirements=40,
    )
    assert summ.skipped_unchanged == 1
    assert summ.reprocessed == 60
    assert summ.to_metrics()["Est. runtime saved"] == "1m 0s", summ.to_metrics()

    # 8) round-trip persistence.
    assert ExtractionMeta.from_dict(base.to_dict()) == base

    print("[OK] extraction_cache smoke self-test passed")
