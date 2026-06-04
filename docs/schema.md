# RFX Pipeline JSON Schema Specification

Version: 1.1
Updated: 2026-04-29

## Data Flow

```
inbound/<case>/rfq/*
    ↓  extract_requirements_llm.py
runs/<case>/requirements.json           ← Stage 1
    ↓  run_case.py
runs/<case>/requirements_enriched.json  ← Stage 2
    ↓  postprocess_requirements.py
runs/<case>/requirements_clean.json     ← Stage 3 (canonical output)
    ↓  export_excel.py
runs/<case>/compliance_matrix.xlsx      ← Final deliverable
```

---

## Stage 1: requirements.json

Produced by: `extract_requirements_llm.py`
Consumed by: `run_case.py`

### Root

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `meta` | object | Y | Extraction metadata |
| `requirements` | array | Y | Extracted requirement items |

### meta

| Key | Type | Description |
|-----|------|-------------|
| `doc_name` | string | Always `"llm_extracted"` |
| `case_id` | string | Case identifier |
| `extracted_at` | string | ISO timestamp |
| `model` | string | LLM model used |
| `file_count` | integer | Number of files processed |

### requirements[] item

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `req_id` | string | Y | Raw ID (AUTO-xxx, ROW_ID, or table prefix) |
| `requirement` | string | Y | Requirement text |
| `source` | object | Y | `{file, chunk}` or `{file, sheet, row}` |
| `notes` | string | Y | Extraction notes (may contain "GLOSSARY/DEFINITION") |
| `confidence` | float | Y | 0.0–1.0 |
| `derived_requirement` | boolean | N | `true` for spec_reference relaxed extraction |
| `spec_category` | string | N | Only when `derived_requirement=true` |
| `source_short` | string | N | **Deprecated** — not consumed downstream |
| `excerpt` | string | N | **Deprecated** — not consumed downstream |

---

## Stage 2: requirements_enriched.json

Produced by: `run_case.py`
Consumed by: `postprocess_requirements.py`

### Root

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `meta` | object | Y | Inherited from Stage 1 |
| `requirements` | array | Y | Enriched requirement items |
| `enriched_at` | string | Y | ISO timestamp |

### requirements[] item

Inherits all Stage 1 fields, plus:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `must_level` | string | Y | `MUST`, `SHOULD`, `MAY`, `INFO` |
| `category` | list[string] | Y | e.g. `["Compliance"]`, `["General"]` |
| `owner` | string | Y | Responsible team |
| `status` | string | Y | `PENDING`, `NEED_REVIEW`, `AUTO_SKIP` |
| `redflag_messages` | list[string] | Y | Long-form risk descriptions (Chinese/English) |
| `redflag_overrides` | object | Y | `{require_evidence, force_category, force_owner, ...}` |
| `stakeholder` | list[string] | N | Additional involved teams |
| `llm_enriched` | boolean | N | `true` if enriched by LLM (vs keyword matching) |

---

## Stage 3: requirements_clean.json (Canonical Output)

Produced by: `postprocess_requirements.py`
Consumed by: `export_excel.py`, `app.py` (Step 4 Review & Fill)

### Root

| Key | Type | Required | Description |
|-----|------|----------|-------------|
| `meta` | object | Y | Inherited from Stage 1 |
| `postprocess` | object | Y | Processing metadata |
| `summary` | object | Y | Counts and PM notes |
| `items` | array | Y | Final processed items |

### postprocess

| Key | Type | Description |
|-----|------|-------------|
| `input` | string | Input filename |
| `fallback_source_file` | string | Primary source file used for fallback |
| `xlsx` | string | Output Excel filename |
| `columns` | list[string] | Excel column headers |

### summary

| Key | Type | Description |
|-----|------|-------------|
| `total_requirements` | integer | Count of requirement-type items |
| `total_glossary` | integer | Count of glossary-type items |
| `total_notes` | integer | Count of note-type items |
| `pm_note` | string | Only present when 0 requirements extracted |

### items[] item — Canonical Schema (15 fields)

| Field | Type | Required | Description | Values |
|-------|------|----------|-------------|--------|
| `req_id` | string | Y | Final ID: `AI-001` or `RFQ-HOST-001` | — |
| `orig_req_id` | string | Y | Original raw ID from extraction | — |
| `type` | string | Y | Classification | `requirement`, `glossary`, `note`, `junk` |
| `must_level` | string | Y | Priority level | `MUST`, `SHOULD`, `MAY`, `INFO` |
| `category` | string | Y | Single category (flattened from list) | `Compliance`, `Reliability`, `BMC`, `BIOS`, `Platform`, `Security`, `Power`, `Thermal`, `Mechanical`, `Storage`, `PCIe`, `Documentation`, `Commercial`, `Legal`, `Serviceability`, `General`, `Performance`, `Memory`, `Network`, `Wireless` |
| `owner` | string | Y | Responsible team | `BIOS`, `BMC`, `QA`, `ME/ID`, `EE/Platform`, `Legal`, `TBD` |
| `stakeholder` | list[string] | Y | Additional involved teams (may be empty) | — |
| `status` | string | Y | Workflow status | `NEW`, `NEED_REVIEW`, `INTERNAL_ALIGN`, `ASK_CUSTOMER`, `READY_FOR_RESPONSE`, `CLOSED`, `AUTO_SKIP` |
| `requirement` | string | Y | Requirement text | — |
| `risk_tags` | list[string] | Y | Short risk tags (may be empty) | `CERT`, `RELIABILITY`, `PRICING`, `SCHEDULE`, `IP/LEGAL`, `LIFECYCLE`, `GLOSSARY`, `ACCEPTANCE`, `SERVICEABILITY` |
| `risk_note` | string | Y | One-line English risk description (may be empty) | — |
| `evidence_needed` | string | Y | Evidence guidance (may be empty) | — |
| `next_action` | string | Y | Suggested next step (may be empty) | — |
| `source` | string | Y | Formatted source reference | e.g. `"filename — Sheet: Sheet1, 第 2 行"` |
| `derived` | boolean | Y | `true` if derived from spec table (relaxed mode), `false` otherwise | `true`, `false` |

---

## Field Lifecycle

### Canonical (use these)

| Field | Introduced | Stage | Notes |
|-------|-----------|-------|-------|
| `risk_tags` | Stage 3 | Replaces `redflag_messages` / `redflag_tags` | Short tags: CERT, RELIABILITY, etc. |
| `risk_note` | Stage 3 | Short English explanation | One-line risk description |
| `type` | Stage 3 | Replaces inferred classification | requirement / glossary / note / junk |
| `orig_req_id` | Stage 3 | Preserves raw extraction ID | AUTO-xxx or table row ID |
| `derived` | Stage 3 | Relaxed extraction marker | `true` for spec_reference items, `false` otherwise |

### Deprecated (do not add new consumers)

| Field | Stage | Replacement | Notes |
|-------|-------|-------------|-------|
| `redflag_tags` | was in clean.json | `risk_tags` | Removed from clean.json output |
| `redflag_messages` | enriched.json | `risk_tags` (via normalize) | Still produced by run_case.py; normalized in postprocess |
| `source_short` | requirements.json | — | Not consumed by any downstream stage |
| `excerpt` | requirements.json | — | Not consumed by any downstream stage |

### Type Changes Across Stages

| Field | Stage 1–2 | Stage 3 | Why |
|-------|-----------|---------|-----|
| `source` | `dict {file, chunk/sheet/row}` | `string` (formatted) | Human-readable for Excel/UI |
| `category` | `list[string]` | `string` (comma-joined) | Simplified for display |

---

## Status Values

| Value | Meaning | Set By |
|-------|---------|--------|
| `NEW` | Fresh from pipeline, not yet reviewed | postprocess (default) |
| `NEED_REVIEW` | Flagged by rules or LLM for PM review | enrich / postprocess |
| `INTERNAL_ALIGN` | PM is aligning internally | manual (via UI) |
| `ASK_CUSTOMER` | Waiting on customer clarification | manual (via UI) |
| `READY_FOR_RESPONSE` | Response drafted, ready to submit | manual (via UI) |
| `CLOSED` | Fully answered and closed | manual (via UI) |
| `AUTO_SKIP` | Glossary/note — excluded from active list | postprocess |
| `COMPLIANT` | Compliance response filled | manual (via UI responses.json) |
| `PARTIAL` | Partially compliant | manual (via UI responses.json) |
| `NON-COMPLIANT` | Non-compliant | manual (via UI responses.json) |

Note: `COMPLIANT`/`PARTIAL`/`NON-COMPLIANT` are stored in `responses/<case>/responses.json`, not in `requirements_clean.json`.

---

## Extraction Modes

### Strict Mode (default)

- Triggered by: `rfq_format` != `"spec_reference"`, or .doc/.docx main files
- Extraction: LLM-based, looks for shall/must/required language
- Confidence: LLM-assigned (0.0–1.0)
- `derived`: always `false`
- May produce 0 requirements if no explicit requirement language found

### Relaxed Mode (spec_reference)

- Triggered by: `doc_schema.rfq_format == "spec_reference"` AND file is .xlsx/.xls
- Extraction: Direct xlsx parse, no LLM needed
- Each spec row (label + SKU values) becomes one candidate requirement
- `derived`: always `true`
- `status`: always `NEED_REVIEW`
- `must_level`: always `INFO`
- `confidence`: always `0.5`
- Category assigned from `spec_category` (CPU, Memory, Storage, etc.)

### Direct Parse (simple_list)

- Triggered by: `doc_schema.rfq_format == "simple_list"` AND xlsx with identifiable ID/Question/Answer columns
- Extraction: Direct xlsx parse, no LLM needed
- `derived`: `false` (these are actual customer questions)
- Example: Q&A-style requirement spreadsheet

### Checklist Parse (auto-detected)

- Triggered by: appendix xlsx with auto-detected checklist header (columns matching both a label keyword like `requirement`/`model`/`specification` AND a comply keyword like `comply`/`compliance`)
- Extraction: Direct xlsx parse, no LLM needed
- `derived`: `false` (these are explicit customer compliance checklist items)
- `must_level`: mapped from Priority column (M→MUST, H→MUST, L→MAY, blank→INFO)
- `confidence`: `1.0`
- req_id: from Ref# column if available, otherwise AUTO-generated
- Section headers (short text without Priority) are automatically skipped
- Quote templates and non-checklist xlsx files are not affected (header detection rejects them)
- Example: Compliance Table xlsx with 3 sheets → ~219 checklist items

**must_level preserve fix**: `run_case.py` preserves must_level values set during extraction
(e.g., Priority=M→MUST from checklist). Without this fix, keyword-based enrichment would
overwrite MUST→INFO for short checklist items like "CPU Selection", causing them to be
misclassified as notes.

### Mode Coexistence

A single case may use multiple modes:
- **Case A**: .doc main (strict) + .docx appendix (skipped)
- **Case B**: .xlsx appendix (relaxed spec_reference, ~28 derived) + .pdf (skipped)
- **Case C**: .docx main (strict, several hundred reqs) + .xlsx appendix checklist (~219 items) + .xlsx quote template (skipped) + .pdf (skipped)

---

## Regression Baselines

### Controlled no-llm baseline (2026-04-29)

Current authoritative baseline. All cases enriched with `--no-llm` (keyword matching only).
Includes: checklist parser, must_level preserve fix, dedup, junk filter.
Deterministic — two consecutive postprocess runs produce identical output.

| Case | rfq_format | Mode | Req | Glo | Note | Total | NEW | NR | SKIP | Derived |
|------|-----------|------|-----|-----|------|-------|-----|----|------|---------|
| **Case A** | spec_reference | Strict (main .doc) | 149 | 8 | 28 | 185 | 128 | 21 | 36 | 0 |
| **Case B** | simple_list | Direct parse (xlsx Q&A) | 99 | 4 | 5 | 108 | 80 | 19 | 9 | 0 |
| **Case C** | spec_reference | Relaxed (xlsx spec table) | 28 | 0 | 0 | 28 | 0 | 28 | 0 | 28 |
| **Case D** | ibm_matrix | Strict (keyword) | 182 | 31 | 35 | 248 | 118 | 64 | 66 | 0 |
| **Case E** | plain_text | Strict + Checklist | 591 | 6 | 54 | 651 | 486 | 105 | 60 | 0 |

Post-processing effects (this baseline):

| Case | Before Clean | Dedup Removed | Junk Removed | After Clean |
|------|-------------|---------------|--------------|-------------|
| Case A | 193 | 0 | 8 | 185 |
| Case B | 108 | 0 | 0 | 108 |
| Case C | 28 | 0 | 0 | 28 |
| Case D | 265 | 50 | 6 | 209 |
| Case E | 695 | 35 | 54 | 606 |

### Legacy LLM baseline (2026-04-29, before enriched.json overwrite)

Previous baseline recorded before enriched.json files were regenerated with `--no-llm`.
These numbers reflected LLM-enriched results for the same cases above and
are no longer reproducible from current enriched.json files (overwritten).
Kept for reference only.

| Case | rfq_format | Mode | Req | Glo | Note | Notes |
|------|-----------|------|-----|-----|------|-------|
| Case A | spec_reference | Strict (LLM-enriched) | 177 | 8 | 0 | LLM enrichment produced more MUST/SHOULD classifications |
| Case B | simple_list | Direct parse | 104 | 4 | 0 | Minimal LLM effect on direct-parsed items |
| Case C | spec_reference | Relaxed | 28 | 0 | 0 | No change — derived items bypass enrichment |
| Case D | ibm_matrix | Strict (LLM-enriched) | 214 | 34 | 0 | LLM enrichment classified more items as requirement |
| Case E | plain_text | Strict only (no checklist) | 503 | 9 | 0 | Before checklist parser; before must_level fix |

Difference explanation: LLM enrichment assigns MUST/SHOULD more aggressively than keyword matching,
causing more short items to be promoted from note → requirement in postprocess. The `--no-llm` baseline
is more conservative but deterministic and reproducible.

---

## UI Behavior (app.py)

### Top Badge Bar

Displays counts from `requirements_clean.json`, excluding `AUTO_SKIP` items:

```
COMPLIANT: 0 | PARTIAL: 0 | NON-COMPLIANT: 0 | NEW: 138 | NEED_REVIEW: 68 | PENDING: 0 | Total: 206
```

- `COMPLIANT`/`PARTIAL`/`NON-COMPLIANT` come from `responses.json` (PM-filled)
- `NEW`/`NEED_REVIEW`/`PENDING` come from pipeline status
- `AUTO_SKIP` items are excluded from all counts

### Step 4: Pipeline Summary Block

Reads `clean_data.summary` and displays:

```
┌──────────────────────────────────────────────────┐
│ Requirements: 214  │  Glossary: 34  │  Notes: 0  │
└──────────────────────────────────────────────────┘
```

- If `summary.pm_note` exists (0-requirements case), shows a warning banner
- If `summary` key is missing (old clean.json), block is silently skipped

### Step 4: Requirement Cards

Each requirement is shown as an expandable card:

```
▶ AI-032 | Reliability | NEED_REVIEW
```

For derived requirements (relaxed extraction):

```
▶ AI-001 | Memory | NEED_REVIEW [DERIVED]
    Derived from spec table — not an explicit customer requirement. Confirm against design.
    Requirement: DRAM: Mini: 8GB, Lite: 8GB, Small: 16GB, Medium: 16GB
```

- `[DERIVED]` badge appears in the expander title
- A caption line explains the derived nature
- `derived` field read from `clean.json` item; defaults to `false` if missing

### Step 4: Status Filter

Dropdown options: `All`, `NEED_REVIEW`, `NEW`, `PENDING`, `COMPLIANT`, `PARTIAL`, `NON-COMPLIANT`

### 0-Requirements Case

When `items` is empty after filtering `AUTO_SKIP`:

```
ℹ This case has 0 actionable requirements.
  The uploaded files may be spec-reference, datasheet, or checklist documents
  without explicit shall/must requirements.
  You can still download the empty templates from Step 3 above.
```

---

## Phase 4.6 — Optional Normalization Fields

Added by `scripts/normalize_requirements_llm.py` (Phase 4.6A prototype).
These fields are populated only when the normalization script is run for a
case; they exist as empty defaults on every `items[]` entry produced by
`postprocess_requirements.py` so the JSON schema stays uniform.

| Field | Type | Required | Description | Values |
|-------|------|----------|-------------|--------|
| `normalized_requirement` | string | Y | LLM-rewritten standalone form of the requirement, with the same constraints (no new numbers/units/standards beyond original + notes + source). Empty when no rewrite was needed or attempted. | — |
| `rewrite_reason` | string | Y | Why the normalization produced what it did. | `""` (empty = never run) · `already_complete` · `fragment_to_standalone` · `qa_answer_to_requirement` · `ambiguous_needs_review` · `no_rewrite` · `not_attempted` |
| `rewrite_confidence` | float | Y | LLM-reported confidence, then optionally capped at 0.5 by the lexical audit if hallucinated tokens were detected. | `0.0`–`1.0` |
| `needs_rewrite_review` | boolean | Y | True when PM should manually verify the normalized text. Set when the LLM self-reports ambiguity, the audit detects new tokens, or the call failed. | `true` / `false` |

**Hard invariants (the script asserts these):**
- `requirement` (the Original text) is never modified.
- `req_id` is never changed.
- `responses.json` is never touched.
- The script is idempotent: rows with `rewrite_reason` set to anything other
  than `""` or `"not_attempted"` are skipped unless `--force`.

**Audit guard.** If the normalized text contains numbers, units, version
codes, model codes, or standards (e.g., `16GB`, `DDR5`, `PCIe 4.0`, `FCC`,
`TPM 2.0`) that are not present in the original / notes / source pool, the
script forces `needs_rewrite_review=true` and caps `rewrite_confidence` at
`0.5`. The original requirement remains the authoritative reference.

---

## Phase 4.6E — PM Final Requirement & Exclude

### responses.json — three new fields (Phase 4.6E.1)

PM edits captured in Step 4: Review & Fill are persisted into
`responses/<case>/responses.json` keyed by `req_id`. Phase 4.6E.1 added three
new fields alongside the existing `status` / `vendor_comment` / `evidence` /
`gap` / `ai_draft` / `updated_at`:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `final_requirement` | string | `""` | PM-edited final wording. Empty means PM has not overridden, so the export's "Requirement (Final)" column falls through to `normalized_requirement` or the original `requirement`. |
| `exclude_from_matrix` | bool | `false` | True = PM has decided this row should NOT appear in the customer-facing Compliance Matrix. Routes the row into the Excluded sheet instead. |
| `exclude_reason` | string | `""` | Free-text reason for the exclusion. Always saved (so toggling Exclude off and back on restores the reason). Surfaces in the Excluded sheet's "Gap / Notes" column. |

The Step 4 UI shows a `[EXCLUDED]` tag on the expander label when
`exclude_from_matrix` is true, and the Status filter dropdown includes an
"Excluded" option that lists only PM-excluded rows.

### compliance_matrix.xlsx — Requirement (Final) column + Excluded sheet (Phase 4.6E.2)

#### HEADERS — 16 → 17 columns

A new **"Requirement (Final)"** column is inserted at position 7, between
"Compliance Status" and "Requirement (Original)". The fallback chain that
populates it is:

1. `responses[req_id].final_requirement` (non-empty after `.strip()`) → use PM edit
2. `item.normalized_requirement` (non-empty) → use LLM-normalized text
3. `item.requirement` → use the original extraction

The Original column is never mutated — it remains the source of truth.

#### New "Excluded" sheet

Placed after `Skipped` (position 12 overall). Schema is **uniform with every
other data sheet** (17 columns). The `exclude_reason` is **not** a separate
column; instead it is prefixed onto the Gap / Notes column for rows in this
sheet:

- With reason: `[EXCLUDED: <reason>] <original gap text>`
- Without reason: `[EXCLUDED]`

The prefix is applied once during response merge (re-running export reads
fresh values from responses.json, so the tag does not accumulate).

#### Routing precedence in `split_sheets()`

PM exclude beats all other routing — even if a row would otherwise have been
classified as glossary / note / junk / AUTO_SKIP, an explicit
`exclude_from_matrix=true` sends it to the Excluded sheet:

```
0. exclude_from_matrix == true → Excluded   ← Phase 4.6E.2
1. type == "glossary" OR risk_tags ⊇ {GLOSSARY} → Glossary
2. type == "note"                              → Notes
3. type == "junk" OR status == "AUTO_SKIP"     → Skipped
4. (everything else)                            → Compliance Matrix (main)
```

#### Row-count invariants

- `Compliance Matrix rows == Σ(3. Hardware ... 8. Others rows)` — both
  derived from `main_reqs` which has PM-excluded items already removed.
- `By_Category_Summary` Total row equals `Compliance Matrix rows` — Summary
  is built from the same `main_reqs` and therefore does NOT count excluded.
- `Excluded sheet rows == count(responses where exclude_from_matrix=true)` —
  the Excluded sheet is a 1:1 reflection of PM decisions.

#### Backward compatibility

- Old `responses.json` files without these fields: all three default to
  empty / false, so behaviour is unchanged.
- Cases that have never run Step 3.5 Normalize: `normalized_requirement` is
  empty, so "Requirement (Final)" falls through to Original.
- Excluded sheet is present even when empty (header row only), so the
  workbook schema is consistent across cases.

---

## Phase 4.6D — Pipeline lock file (advisory)

A case-level advisory lock prevents two Streamlit sessions from mutating
the same case at the same time. The lock is purely cooperative — it has
no kernel-level enforcement; it works because every entry point in
`app.py` consults `read_lock_info()` before running.

### Path

```
runs/<case_id>/.pipeline.lock
```

Excluded from version control (`runs/` is in `.gitignore`).

### Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `case_id` | string | — | Echo of the case ID for sanity checks. |
| `started_at` | ISO 8601 string | — | When `acquire_lock()` ran. Drives the 2 h stale rule. |
| `pid` | int | — | Acquiring process's PID. Informational; PID liveness does **not** override the stale rule. |
| `host` | string | — | `socket.gethostname()` at acquire time. Informational. |
| `user` | string | — | `getpass.getuser()` at acquire time. Informational. |
| `start_step` | int | — | Pipeline step index the run began from (0 = full pipeline, 1 = enrich+format+export, 0 also for normalize). Insufficient on its own to distinguish run types — see `operation`. |
| `operation` | string | `"unknown"` | **Phase 4.6D**. What kind of run is holding the lock. See allowed values below. |

### `operation` allowed values

| Value | Meaning | Acquired by |
|-------|---------|-------------|
| `"pipeline"` | Full Pipeline or Enrich+Format+Export is running. | Phase 2 — Run Full Pipeline / ⚡ Enrich + Format + Export |
| `"normalize"` | Step 3.5 Normalize is running. | Step 3.5 — Run Normalize |
| `"unknown"` | Anything else (caller did not specify, or specified an unrecognized value, or it is an old lock file from before Phase 4.6D). | Fallback in `acquire_lock()` |

`acquire_lock(case_id, start_step, operation=...)` normalizes any
unrecognized string (`None`, `""`, typos, future values) to `"unknown"`
so a typo in a future caller cannot corrupt the schema.

### UI rendering (Phase 4.6D)

The active-lock banner under Step 2 picks a headline by `operation`:

| `operation` | Headline shown to PM |
|-------------|----------------------|
| `"pipeline"` | `🔒 This case is currently locked by a pipeline run.` |
| `"normalize"` | `🔒 This case is currently locked by a normalize run.` |
| `"unknown"` or missing | `🔒 This case is currently locked by another session.` |

Both the active-lock banner and the stale-lock banner additionally
display `operation:` in their metadata line so debugging is easy when a
lock looks suspicious.

### Stale rule (unchanged)

A lock is **stale** when *any* of the following is true:

- The file is unparseable (invalid JSON / non-object).
- `started_at` is missing or unparseable.
- `now - started_at > 2 hours` (`PIPELINE_LOCK_STALE_HOURS`).

PID liveness is **not** consulted by the stale rule — it appears in the
banner as a hint only. Phase 4.6D does not change this.

### Backward compatibility

- Lock files written before Phase 4.6D have no `operation` field.
  `read_lock_info()` parses them unchanged; the UI falls back to the
  "another session" headline and shows `operation: unknown` in the
  metadata line.
- All callers explicitly pass `operation=` after Phase 4.6D, so freshly
  written lock files always carry the field.
- The stale rule is identical, so any operational tooling that checks
  age continues to work.

---

## Phase 4.6F — Progress log contract (UI streaming)

Phase 4.6F.1 introduces live progress widgets in **Step 2** (Full Pipeline
/ Enrich+Format+Export) and **Step 3.5** (Normalize). The widgets are
driven by parsing each subprocess's stdout line by line. The format below
is the contract between the scripts that produce these lines and the UI
parser in `app.py` (`_parse_progress_line`).

### Recognized event lines

| Line shape | Emitted by | UI effect |
|---|---|---|
| `[INFO] <file>: <N> chunks` | `extract_requirements_llm.py` per file | Reset chunk slot to `0 / N`, set current file |
| `[PROGRESS] <file> chunk <i>/<N>` | `extract_requirements_llm.py` before each chunk's LLM call | Advance chunk slot to `i / N`; recompute ETA |
| `[SKIP] <file> chunk <i>/<N> already done` | `extract_requirements_llm.py` resume path | Same as `[PROGRESS]` — advances counter |
| `  [<i>/<N>] <req_id> …` | `normalize_requirements_llm.py` per item | Advance item slot to `i / N · req_id` |
| `[PROGRESS] enrich item <i>/<N> req_id=<id>` | `run_case.py` per item | Advance item slot to `i / N · req_id` |
| `[WARN] LLM call failed (attempt <i>/<N>) … -> sleep <s>s` | `extract_requirements_llm.py` retry | Show retry warning |
| `[WARN] LLM enrich attempt <i>/<N> … -> sleep <s>s` | `run_case.py` retry | Show retry warning |

Any line that doesn't match any pattern is appended to the per-step log
expander unchanged. A step that emits **no** events still works — the
UI shows the spinner, an "elapsed: 0s (running…)" placeholder, and the
full log on completion.

### Buffering

`run_step_streaming()` injects `PYTHONUNBUFFERED=1` into the subprocess
environment so Python's stdout flushes line by line into the pipe.
Scripts therefore do **not** need `flush=True` on individual `print()`
calls; the existing prints stream as-is.

### Stderr

The streaming runner merges `stderr` into `stdout`
(`stderr=subprocess.STDOUT`), so `[WARN]` retry lines arrive in the same
event stream and are parsed alongside `[PROGRESS]` lines.

### ETA

ETA is computed as `elapsed × (total − done) / done`. It is shown only
when `done ≥ 2` and `done < total`, and is labelled "(rough)" — chunk
sizes are uneven, retry sleeps distort the rate, and the linear
extrapolation does not model these. Requiring `done ≥ 2` dampens the
wildly misleading first-sample ETA that a single warm-up call would
produce.

### Scope of Phase 4.6F.1

**In scope:**
- Streaming infrastructure (`run_step_streaming`)
- Extract chunk progress
- Normalize per-item progress
- LLM retry warning surface

**Out of scope:**
- Persisted run history / per-run timeline export

---

## Phase 4.6G — Bad chunk soft-fail

Before Phase 4.6G a single chunk that the LLM could not return parseable
JSON for (after the built-in 3 retries) would `raise RuntimeError`,
exiting the extract subprocess and stopping the whole pipeline. For very
large documents (e.g. a 2979-chunk specification) one unlucky chunk was
enough to block the entire case, because the failure was deterministic
on resume — same chunk text + same model = same parse failure.

Phase 4.6G changes the chunk loop so that retry-exhausted chunks are
**recorded and skipped**, with a threshold gate that still aborts when
failures look systemic.

### `runs/<case>/extract_errors.jsonl`

Append-only across runs (history of every soft-failed chunk). One JSON
object per line:

| Field | Type | Description |
|-------|------|-------------|
| `file` | string | Source filename whose chunk failed (matches `requirements.partial.jsonl`'s `file`). |
| `chunk` | int | 1-indexed chunk number within `file`. |
| `total` | int | Total chunks in `file` at the time of failure. Useful when chunk counts change between runs. |
| `error` | string | First 500 chars (ASCII-replace) of the `RuntimeError` raised by `call_llm_json_with_retry`. Typically includes a preview of the raw LLM response. |
| `model` | string | `args.model` at the time of failure (matches the model name in `runs/_debug/llm_raw_*.txt` filenames). |
| `ts` | ISO 8601 string | `now_iso()` at the time the soft-fail was recorded. |
| `chunk_chars` | int | `len(chunk)` — useful for spotting "always the long chunks" patterns. |

### `requirements.partial.jsonl` — new optional `failed_chunk` field

Records written by `append_partial(..., failed=True)` carry an extra
`"failed_chunk": true` field. The `requirements` array is empty (`[]`).
A legitimately empty chunk (e.g. boilerplate, signature page) is also
written with `requirements: []` but **without** the `failed_chunk` field,
so the two cases are distinguishable.

### Threshold gate (CLI flags on `scripts/extract_requirements_llm.py`)

| Flag | Default | Meaning |
|------|---------|---------|
| `--max-failed-chunks` | `20` | Abort the run if the absolute count of failed chunks in this run exceeds this. `0` aborts on the first failure. |
| `--max-failed-pct` | `5.0` | Abort if `failed_chunks / attempted_chunks` in this run exceeds this percentage. Only applies once `--min-attempted-for-pct` chunks have been attempted. |
| `--min-attempted-for-pct` | `50` | Disable the `--max-failed-pct` gate until this many chunks have been attempted in this run. Prevents small-sample noise (e.g. `1/16 = 6.25%`) from tripping the gate. |
| `--retry-failed-chunks` | off | On resume, re-attempt chunks previously marked `failed_chunk=true`. Default: skip them like any other completed chunk. |

`attempted_chunks` excludes resume-skipped chunks — only chunks that
went through `call_llm_json_with_retry` in this run count.

The gate uses an OR: either an absolute-count overflow or a percentage
overflow aborts. When the gate trips, the run still raises
`RuntimeError`, but the failed chunks that have already been recorded
(in `extract_errors.jsonl` and as `failed_chunk=true` rows in
`partial.jsonl`) remain on disk for inspection.

### Resume behavior

| Scenario | `failed_chunk=true` row in partial | Behavior |
|----------|------------------------------------|----------|
| Default resume | yes | Treated as done — re-run skips the chunk. |
| `--retry-failed-chunks` | yes | Removed from `done_keys` — re-run re-attempts the chunk. If it fails again, a new `extract_errors.jsonl` row is appended (history is preserved). |
| Legitimately empty chunk (no `failed_chunk` field) | no | Treated as done in both modes — never re-attempted. |

### UI surfacing (Streamlit)

- `extract_errors.jsonl` appears in **Step 3 → Advanced Outputs** alongside the other intermediate artifacts.
- When `extract_errors.jsonl` exists with one or more rows, **Step 3** shows a yellow warning banner above the download cards with the count and instructions for retry.

### End-of-run stdout

The extract script prints a summary line that the UI streams through
verbatim:

```
[OK] Output: runs/<case>/requirements.json
[OK] Requirements count: <N> (skipped <K> chunk(s))   ← parenthetical only when K > 0
[OK] Partial saved: runs/<case>/requirements.partial.jsonl
[WARN] <K> chunk(s) failed extraction; see runs/<case>/extract_errors.jsonl   ← only when K > 0
```

---

## Phase 4.6I — Cancel / interrupt button

Long extract runs on big documents (a 2979-chunk spec, an LLM provider
that is timing out, etc.) used to require the PM to find and `taskkill`
the Python subprocess by hand. Phase 4.6I exposes a **Cancel** button in
the UI that kills the running subprocess, cleans up the lock, and
preserves all partial outputs.

### Subprocess PID sidecar — `runs/<case_id>/.pipeline.subproc_pid`

A short-lived companion to `.pipeline.lock`. Contains a single line: the
PID of the currently-running extract / enrich / normalize child.

- **Written** by `run_step_streaming(..., case_id=...)` immediately after
  `subprocess.Popen` returns. The PID is the child process, **not** the
  Streamlit master.
- **Removed** by `run_step_streaming`'s `finally` block when the child
  exits (normal completion, error, or cancel).
- **Not** part of the lock schema — `acquire_lock` does not touch it, and
  `is_lock_stale` does not consult it. The lock and the sidecar have
  different lifetimes: the lock spans a whole pipeline (Extract → Enrich
  → Format → Export), while the sidecar tracks **one step at a time**.

### Cancel button — UI

The Cancel button is rendered next to the active-lock banner in Step 2.
It is visible **only** when both of:
- `lock.host == socket.gethostname()`
- `lock.user == getpass.getuser()`

are true — a lock held by another host or user belongs to that operator
and is not ours to cancel.

Because Streamlit serializes script execution per session, the tab that
clicked **Run Full Pipeline** is busy inside `run_step_streaming` and
cannot process its own Cancel click. To cancel a run, **refresh the tab
or open a second tab** for the same case — the fresh script run reads the
lock, renders the Cancel button, and the click takes effect there.

### Cancel behavior

`cancel_pipeline(case_id)` performs the following, in order:

1. Read the lock; if `host` or `user` does not match the current process,
   refuse and return `{killed: False, reason: "refuse: ..."}` without
   touching anything.
2. Read the child PID from `.pipeline.subproc_pid` (if present).
3. Call `_kill_process_tree(pid)` — on Windows this runs
   `taskkill /F /T /PID <pid>` so the child and any grandchildren are
   killed together; on POSIX it uses `os.killpg` then falls back to
   `os.kill(pid, 9)`.
4. Remove `.pipeline.subproc_pid`.
5. Call `release_lock(case_id)` to remove `.pipeline.lock`.

Safety guarantees in `_kill_process_tree`:
- Returns `False` (no-op) for an invalid PID, our own PID, or a PID that
  is already gone — so a Cancel click after the subprocess has finished
  naturally is harmless.
- Never raises — best-effort signalling.

### What is preserved on cancel

- `requirements.partial.jsonl` — every chunk that completed before the
  cancel is still in the file. Re-running with `--resume` (the default)
  skips them.
- `extract_errors.jsonl` — any soft-failed chunk records survive.
- `requirements.json` / `requirements_enriched.json` / `compliance_matrix.xlsx`
  — only updated at the very end of their respective steps, so a
  mid-step cancel leaves the previous version (if any) untouched.

### UX feedback

After cancel, the tab that clicked Cancel re-renders without the lock
banner and shows:

```
⏹ Pipeline cancelled by user. killed PID <N> and descendants. Partial progress is preserved.
```

The tab that was running the pipeline sees its `run_step_streaming` loop
exit naturally (the subprocess pipe closed), the streaming step records
a non-zero `returncode`, the per-step `st.status` settles into the error
state, and `release_lock` runs as a no-op (the lock is already gone).

---

## Phase 4.6J — Persisted run history

Before Phase 4.6J the only record of a pipeline run was the in-memory
`pipeline_step_results` shown in the current Streamlit session. Once the
session closed or another run started, the previous result was gone —
PMs had no way to audit "did a particular case finish on Tuesday? did
it fail or succeed?" without re-running.

Phase 4.6J writes one append-only JSONL file per case that records
every Step 2 pipeline run, every Step 3.5 normalize run, and every
Cancel event.

### Path

```
runs/<case_id>/run_history.jsonl
```

Excluded from version control (`runs/` is in `.gitignore`). UTF-8 without
BOM — `append_run_history()` uses `open(..., "a", encoding="utf-8")`
which never prepends a BOM. (PowerShell's `Out-File -Encoding utf8` does,
which would break `read_run_history` — see Phase 4.6I post-mortem.)

### Per-record schema

One JSON object per line. Two records per normal run (start + terminal):

| Field | Type | Description |
|-------|------|-------------|
| `run_id` | string | 12-char hex (from `uuid.uuid4().hex[:12]`). The "started" and the matching terminal record share this id. |
| `case_id` | string | Echoes `runs/<case_id>/`. |
| `operation` | string | One of `"pipeline"` (Run Full Pipeline, `start_step=0`), `"enrich_format_export"` (`start_step=1`), `"normalize"` (Step 3.5), `"unknown"` (cancel against an unrecognized lock). |
| `status` | string | `"started"`, `"success"`, `"failed"`, or `"cancelled"`. |
| `started_at` | ISO 8601 | When `acquire_lock()` returned. |
| `ended_at` | ISO 8601 \| null | Filled on terminal/cancel records; `null` on `"started"`. |
| `duration_sec` | int \| null | `time.monotonic()` delta on terminal records; `null` on `"started"`. For cancel, computed from lock's `started_at` if parseable, else `null`. |
| `user` | string | `getpass.getuser()` at write time. |
| `host` | string | `socket.gethostname()` at write time. |
| `pid` | int | Streamlit master's `os.getpid()` — useful for cross-tab attribution. |
| `subproc_pid` | int \| null | Only set on cancel records — the child PID that was killed (`null` if no sidecar). |
| `start_step` | int \| null | The `pipeline_start_step` for pipeline/enrich runs; `0` for normalize; `null` for cancel against unknown ops. |
| `steps` | list \| null | On terminal records, a compact summary `[{label, ok, rc}, …]` per step. `null` on `"started"` and `"cancelled"`. |
| `return_code` | int \| null | Last subprocess `returncode` on terminal records. |
| `message` | string \| null | One-line summary. On success: the step's `ok_msg`. On failure: `"failed at: <label>"`. On cancel: the `cancel_pipeline()` reason. |

### Operations classification

| `start_step` | Operation written |
|---|---|
| `0` | `"pipeline"` |
| `1` | `"enrich_format_export"` |
| (normalize button) | `"normalize"` |

Cancel records pull `operation` from the lock file's `operation` field
(set by `acquire_lock` per Phase 4.6D), so a cancelled pipeline shows
`"pipeline"` and a cancelled normalize shows `"normalize"`.

### Cancel-induced duplicate terminal records

A cancel from another tab and the original tab's own teardown both write
terminal records:

1. Tab 1 acquires lock, writes `"started"`, runs subprocess.
2. Tab 2 clicks Cancel → `cancel_pipeline` kills subprocess, writes
   `"cancelled"` (with the lock's `operation`).
3. Tab 1's `run_step_streaming` loop returns (the pipe was closed by the
   kill), the step records `rc != 0`, the `finally:` block releases the
   (already-gone) lock, and Tab 1 writes `"failed"`.

The history file therefore contains `started` + `cancelled` + `failed`
for one user-perceived run. Both terminal records are honest — one
describes the operator intent, the other the subprocess exit. The UI's
"Recent runs" expander surfaces them in newest-first order; PMs can read
the `cancelled` record to understand why the `failed` record is there.

### Safety / best-effort

`append_run_history()` swallows every exception. A failure to write the
history must never break the pipeline. Likewise `read_run_history()`
returns `[]` on missing file, decode error, or any other failure, and
silently skips unparseable lines so a partially corrupt file still
yields the valid records.

### UI surfacing

- **Step 3 → "Recent runs (N)" expander** (collapsed by default). Lists
  the latest 5 records, newest first, as one-line bullets:
  ```
  ✅ `2026-06-04T15:32:11` · **enrich_format_export** · success · 142s — Compliance matrix exported and ready for distribution.
  ⏹ `2026-06-04T14:48:30` · **pipeline** · cancelled · 312s — killed PID 12428 and descendants
  ❌ `2026-06-04T14:11:02` · **pipeline** · failed · 47s — failed at: Extract — 讀取 RFQ，AI 提取需求條目
  ```
- **Step 3 → Advanced Outputs**: `run_history.jsonl` listed alongside
  the other intermediate artifacts.

### Backward compat

A case with no history file simply shows no expander — old cases are not
broken. The first run on a fresh case creates the file.

---

## Phase 4.6H — Runtime guard

After PM trials it became clear that the most common cost-burning
mistakes were:
- Pressing **Run Full Pipeline** on a case that already had a finished
  `requirements.json`, accidentally re-running Extract.
- Operating on the wrong case after a long browser session (the sidebar
  selectbox is easy to miss when scrolled to Step 2).
- Re-extracting a doc that the prior run already taught us was huge —
  the 2979-chunk specification document case is the canonical example.

Phase 4.6H adds in-flight UI prompts in Step 2 to make these mistakes
explicit and confirmable.

### Current case banner

Top of Step 2:

```
Current case: <case_id>
```

Rendered as a one-line `st.markdown` in `#0D47A1` (deep blue) so the
active case is visually unambiguous next to the Pipeline buttons.

### `requirements.json` exists recommendation

When `runs/<case>/requirements.json` is present and non-trivial:

```
ℹ️ requirements.json already exists. Recommended: use ⚡ Enrich + Format + Export
unless you intentionally want to re-run extraction.
```

The Enrich+Format+Export button also gets a `(Recommended)` suffix in
the same condition so the choice is reinforced at the click target.

### Size-based warnings (from prior partial.jsonl)

`_estimate_chunks_from_partial(case_id)` reads
`runs/<case>/requirements.partial.jsonl` and sums **max chunk index per
file** (not line count — resumed runs and Phase 4.6G's failed-chunk
marker rows can double-count if we count lines). The result is treated
as the doc's chunk count from the previous extract.

| Estimated chunks | UI |
|---|---|
| `> 1000` | ⚠ **Very large document** warning (yellow) |
| `> 300` and `≤ 1000` | ⚠ **Large extraction history** warning (yellow) |
| `≤ 300` | No size warning |

### Confirmation checkbox

Run Full Pipeline is **disabled until checked** whenever either:
- `requirements.json` exists for the case, OR
- `_estimate_chunks_from_partial(case_id) > 300`.

```
☐ I understand Full Pipeline will re-run extraction and may take a long time.
```

The checkbox state is keyed by `selected_case`, so toggling cases
discards the confirmation (so the operator confirms per case, not
globally).

### Always-shown notice above Full Pipeline

```
ℹ️ Full Pipeline will re-run Extract and may take a long time.
```

Shown unconditionally so a PM with a fresh case still sees the cost
notice before clicking.

### What does NOT change

- **⚡ Enrich + Format + Export** is unaffected and remains clickable
  whenever `requirements.json` exists. Its label gets the
  `(Recommended)` suffix when prior extraction output is present, but it
  has no checkbox guard.
- **Run Full Pipeline** is still clickable normally on a fresh case
  (no `requirements.json`, no partial, or `≤ 300` estimated chunks).
- Step 3.5 Normalize is unaffected.
- Lock / sidecar / cancel behavior unchanged.
