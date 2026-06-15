# Guarded Extraction-Cache Reuse — Design (v1.3)

Status: design only. **Do not implement true reuse yet** (correctness risk).
Related: `scripts/extraction_cache.py`, `scripts/cache_metadata.py`,
`docs/extraction_cache.md`.

## Current cache behavior (today)

- `scripts/extraction_cache.py` defines the metadata (`ExtractionMeta`: file
  hash, mtime, size, path, parser/prompt version, model, requirement_text_hash,
  extraction_config), a `cache_signature` / `should_skip` decision, and a
  `CacheSummary`.
- `scripts/cache_metadata.py` reads `requirements_clean.json` + `manifest.json`,
  builds per-item + per-file metas, compares to a prior
  `runs/<case>/extraction_cache.json`, writes the refreshed cache, and prints a
  `CacheSummary` (total / reused / reprocessed / skipped-unchanged / est. saved).
- The Streamlit "Incremental Cache Summary" expander displays this.

### What is report-only today
**Everything.** No LLM work is skipped and no prior result is reused. The cache
is pure observability: it measures what a future incremental run *could* reuse.
`should_skip` is computed and counted, but nothing acts on it. The
"estimated runtime saved" figure is advisory, not realized.

## What must change for true reuse

True reuse means: during extraction, for a source unit whose signature is
unchanged vs the cached run, **skip the LLM call and copy the prior requirements**
instead of re-extracting. Concretely:

1. The extractor (`extract_requirements_llm.py`) must, per chunk/file, compute
   the signature and look it up in a cache of prior **requirement objects**
   (not just metadata). Today the cache stores metadata only — it does not store
   the extracted requirements keyed by signature. A reuse cache must persist the
   actual prior `requirements[]` per unit.
2. A new cache store: `runs/<case>/extraction_reuse.json` (or reuse partial.jsonl
   keyed by signature) mapping signature → requirement objects.
3. A flag to opt in (default OFF): `--use-extraction-cache`.
4. On a cache hit, emit the cached requirements and mark them `reused=true` so
   the UI/report can show real reuse counts.

## Correctness risks (why this is guarded)

| Change | Risk if reused blindly | Mitigation (must be in the signature) |
|--------|------------------------|----------------------------------------|
| Changed requirement text | source edited but reused → wrong content | source **file hash** in signature (already present) |
| Changed prompt | new instructions, old output reused → drift | `prompt_version` in signature (already present) |
| Changed model | different extraction quality reused | `model_name` in signature (already present) |
| Changed chunking (`max_chars`/`group_size`) | unit boundaries differ → mismatched reuse | `extraction_config` in signature (already present) |
| Changed extraction schema / `doc_schema` | req_id rules / routing differ | **ADD** doc_schema hash to signature (NOT present today) |
| Stale req_ids | reused ids collide with newly-numbered ones | reuse must preserve ids verbatim AND re-run dedup |

Key gap: the current signature does **not** include the `doc_schema` (req_id
rules, file routing). True reuse must add a `doc_schema_hash` so a schema change
invalidates the cache.

## Proposed guarded implementation (future)

- **Default OFF.** Reuse only when `--use-extraction-cache` is passed (and a
  matching UI toggle, default unchecked).
- **Signature** = sha256 over: source file hash + chunk/requirement text hash +
  `parser_version` + `prompt_version` + `model_name` + canonical
  `extraction_config` + **`doc_schema_hash`** (new). Any mismatch → cache miss →
  re-extract (safe default).
- **Store** prior requirement objects per signature in a dedicated reuse file;
  never reuse across cases.
- **Reporting**: extend `CacheSummary` to count *realized* reuse vs reprocessed,
  and show it distinctly from today's advisory estimate. The UI caption must
  switch from "Report-only. No LLM work is skipped yet." to an explicit
  "Reuse ON — N requirements reused" only when the flag is active.
- **Verification before shipping**: a model-output comparison (see
  `scripts/compare_model_outputs.py`) between a clean run and a reuse run on the
  same case must show identical counts / distributions / ids.

## Decision

Keep cache **report-only** for now. Implement true reuse only after: (1) the
`doc_schema_hash` is added to the signature, (2) a reuse store is designed, and
(3) a clean-vs-reuse comparison shows zero drift. This is deliberately deferred
as not-yet-low-risk.
