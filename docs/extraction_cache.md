# Incremental Extraction Cache — Design (Phase 8)

Version: 0.1 (scaffold)
Updated: 2026-06-11
Status: **definition only** — no extractor wiring yet

Goal: make requirement extraction incremental. On re-runs, unchanged source
files and unchanged requirements are reused from a prior run instead of being
re-sent to the LLM, cutting cost and runtime. This document defines the data
model and the skip-unchanged decision. The actual wiring into
`extract_requirements_llm.py` is a later, separate step.

Module: `scripts/extraction_cache.py` (pure data + pure functions, no LLM/IO of
RFQ docs). Mirrors the conservative-default style of
`file_selection.load_excluded`: when in doubt, reprocess.

---

## Metadata schema — `ExtractionMeta`

Identity of one extraction unit (one source file under one
parser+prompt+model+config). Persisted alongside cached results so the next run
can compare.

| Field | Type | Description |
|-------|------|-------------|
| `file_hash` | string | SHA-256 of source file bytes. **Truth** for "did the file change". |
| `file_mtime` | string | ISO-8601 mtime. Advisory only (a touch must not invalidate). |
| `file_size` | int | Bytes. Advisory / quick change hint. |
| `source_file_path` | string | Source file path (identity + display). |
| `parser_version` | string | `PARSER_VERSION` at extraction time. |
| `prompt_version` | string | `PROMPT_VERSION` at extraction time. |
| `model_name` | string | LLM model id used (resolved `get_model()`). |
| `requirement_text_hash` | string \| null | Per-requirement text hash; `null` at file level. |
| `extraction_config` | object | Output-affecting knobs (`max_chars`, `group_size`, `retries`, …). |

Two granularities share one schema:

- **File level** (`requirement_text_hash = null`) — decide whether to re-run the
  LLM for a whole file.
- **Requirement level** (`requirement_text_hash` set) — decide whether one
  requirement's downstream work can be reused.

---

## Versioning gates

| Constant | Bump when… | Effect |
|----------|-----------|--------|
| `PARSER_VERSION` | the deterministic doc→blocks→chunks→req_id path changes | same bytes can yield different blocks → cache miss |
| `PROMPT_VERSION` | `build_prompt()` / extraction instructions change | same bytes + parser can yield different requirements → cache miss |

Compared by equality only. Bumping either invalidates every prior cache entry
carrying the old value.

---

## Skip-unchanged decision — `should_skip(prev, cur)`

Required skip basis: **source file hash + requirement text hash + parser version
+ prompt version**. `model_name` and `extraction_config` are folded into the key
by default (a model swap or knob change can change output for identical bytes —
reusing across them would be a correctness bug). Opt out with
`include_model_and_config=False`.

| Outcome | `skip` | `reason` | Meaning |
|---------|--------|----------|---------|
| No prior cache | `False` | `no_cache` | first run / new file |
| Signatures equal | `True` | `unchanged` | reuse cached result |
| Signatures differ | `False` | `changed` | reprocess; `changed_fields` lists what differs |

Equality is computed via `cache_signature(meta)` (a SHA-256 over the identity
fields). `file_mtime` / `file_size` are **not** in the signature, so a touched
but byte-identical file still skips.

---

## UI summary — `CacheSummary`

Drop-in for the existing `st.metric` card row in `app.py` (same shape as the
normalize "This run" summary). `to_metrics()` returns label→value in display
order.

| Card | Source |
|------|--------|
| Total requirements | final result count this run |
| Reused from cache | requirements taken verbatim from a prior run |
| Reprocessed | requirements (re)produced by the LLM this run |
| Skipped unchanged | count of file-level `skip=True` decisions |
| Est. runtime saved | `reused × DEFAULT_SECONDS_PER_REQUIREMENT`, formatted `Xm Ys` (advisory) |

`Est. runtime saved` is a display estimate only, never used for correctness.

---

## Out of scope for this step

- No changes to `extract_requirements_llm.py`, `run_case.py`, or `app.py`.
- Where cache entries are persisted (a sidecar under `runs/<case>/` vs. embedded
  in `requirements.json` `meta`) is decided when wiring, not here.
- Requirement-level reuse plumbing (carrying `requirement_text_hash` through the
  extractor output) is a later step.
