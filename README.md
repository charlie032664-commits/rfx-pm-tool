# AI RFX PM Tool

RFQ/RFP requirements extraction, enrichment, and compliance matrix generation.
Streamlit UI with LLM + rule-based pipeline. Supports OpenAI and internal (OpenAI-compatible) LLM endpoints.

---

## Directory Structure

```
ai_rfx_streamlit_dev/
├── app.py                           # Streamlit main entry (single-file UI)
├── run_rfx.bat                      # Start Streamlit (OpenAI mode)
├── run_rfx_internal.bat             # Start Streamlit (Internal LLM mode)
├── stop_rfx.bat                     # Kill Streamlit process on port 8501
├── .streamlit/
│   └── config.toml                  # headless=true, theme=light
├── scripts/                         # Pipeline scripts (called as subprocess)
│   ├── llm_client.py                # Shared LLM client wrapper (OpenAI / internal)
│   ├── extract_requirements_llm.py  # Step 1: Extract requirements from RFQ files
│   ├── run_case.py                  # Step 2: Enrich (category, owner, risk flags)
│   ├── postprocess_requirements.py  # Step 3: Clean, dedup, normalize, export review.xlsx
│   ├── export_excel.py              # Step 4: Export compliance_matrix.xlsx
│   └── responses_manager.py         # PM response CRUD (used by app.py)
├── rules/                           # Rule definitions (YAML)
│   ├── must_level_map.yaml          # MUST / SHOULD / MAY / INFO patterns
│   ├── owner_map.yaml               # category -> owner mapping
│   ├── category_map.yaml            # keyword -> category classification
│   └── redflags.yaml                # Risk flag rules (RF_CERT, RF_RELIABILITY, ...)
├── inbound/                         # Case input data
│   └── <case_id>/
│       ├── meta/case.yaml           # Case metadata
│       ├── meta/doc_schema.json     # Document format analysis (auto-generated)
│       └── rfq/                     # Uploaded RFQ files (.docx/.xlsx/.pdf)
├── runs/                            # Pipeline outputs (auto-generated)
│   └── <case_id>/
│       ├── manifest.json
│       ├── requirements.json            # Step 1 output
│       ├── requirements.partial.jsonl   # Step 1 resume checkpoint
│       ├── requirements_enriched.json   # Step 2 output
│       ├── requirements_clean.json      # Step 3 output (canonical)
│       ├── requirements_review.xlsx     # Step 3 output (PM review)
│       └── compliance_matrix.xlsx       # Step 4 output (final deliverable)
├── responses/                       # PM responses (per case)
│   └── <case_id>/
│       └── responses.json           # Status, vendor_comment, gap, evidence
├── docs/
│   └── schema.md                    # JSON schema specification
├── _legacy/                         # Old versions (not used in production)
└── kb/                              # Knowledge base (reserved, currently unused)
```

---

## Startup

**Internal LLM (recommended):**
```
run_rfx_internal.bat
```
Sets `LLM_PROVIDER=internal` + model + base_url, then calls `run_rfx.bat`.

**OpenAI:**
```
run_rfx.bat
```
Requires `OPENAI_API_KEY` environment variable.

**Manual:**
```bash
cd ai_rfx_streamlit_dev
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501
```

Open browser: `http://localhost:8501`

---

## Pipeline

```
inbound/<case_id>/rfq/*.docx|xlsx|pdf
        |
        v
[Step 1] extract_requirements_llm.py  -->  requirements.json
        |    Modes: LLM strict / direct parse (simple_list) / relaxed (spec_reference) / checklist (auto-detected)
        v
[Step 2] run_case.py + rules/*.yaml   -->  requirements_enriched.json
        |    Assigns: must_level, category, owner, risk flags, status
        v
[Step 3] postprocess_requirements.py  -->  requirements_clean.json + requirements_review.xlsx
        |    Dedup, junk filter, normalize risk tags, assign req_id (AI-/RFQ-)
        v
[Step 4] export_excel.py              -->  compliance_matrix.xlsx
             Merges responses.json for final deliverable
```

UI provides two run modes:
- **Run Full Pipeline** — Steps 1-4
- **Enrich + Format + Export** — Steps 2-4 (skip LLM extraction)

---

## Extraction Modes

| Mode | Trigger | LLM | Example |
|------|---------|-----|---------|
| **Strict** | Default for all formats | Yes | IBM RFQ (shall/must requirements) |
| **Direct parse** | `rfq_format=simple_list` + xlsx | No | Nokia Q&A spreadsheet |
| **Relaxed** | `rfq_format=spec_reference` + xlsx | No | AtlasRFQ spec table |
| **Checklist** | Appendix xlsx with auto-detected compliance header | No | AA Compliance Table |

Relaxed mode produces **derived requirements** (`derived: true`) marked `NEED_REVIEW`.
Checklist mode parses compliance checklists (Ref# / Requirement / Priority / Comply columns) as regular requirements with priority mapping (M→MUST, H→MUST, L→MAY).
Strict mode may produce 0 requirements for spec-only documents — this is a valid outcome.

---

## LLM Configuration

| Variable | Description |
|----------|-------------|
| `LLM_PROVIDER` | `openai` (default) or `internal` |
| `OPENAI_API_KEY` | OpenAI API key |
| `OPENAI_MODEL` | OpenAI model (default: gpt-4.1-mini) |
| `INTERNAL_LLM_BASE_URL` | Internal endpoint URL |
| `INTERNAL_LLM_API_KEY` | Internal API key |
| `INTERNAL_LLM_MODEL` | Internal model name |
| `LLM_TIMEOUT_SECONDS` | HTTP timeout (default: 60) |

---

## Status Values

Pipeline statuses (in `requirements_clean.json`):
`NEW`, `NEED_REVIEW`, `AUTO_SKIP`

Extended statuses (set via UI):
`INTERNAL_ALIGN`, `ASK_CUSTOMER`, `READY_FOR_RESPONSE`, `CLOSED`

Response statuses (in `responses.json`):
`COMPLIANT`, `PARTIAL`, `NON-COMPLIANT`

---

## JSON Schema

See [docs/schema.md](docs/schema.md) for the full specification of:
- `requirements.json` (Stage 1)
- `requirements_enriched.json` (Stage 2)
- `requirements_clean.json` (Stage 3 — canonical, 15 fields)

## Regression Testing

See [docs/regression_checklist.md](docs/regression_checklist.md) for the step-by-step verification procedure.
Baseline: Controlled no-llm baseline (2026-04-29), 5 cases.

---

## Dependencies

```
streamlit
openai
httpx
python-docx
openpyxl
pyyaml
pandas
```

Install:
```bash
pip install streamlit openai httpx python-docx openpyxl pyyaml pandas
```
