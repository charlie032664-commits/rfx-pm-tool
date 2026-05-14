# Regression & Release Checklist

Version: 1.0
Baseline: Controlled no-llm baseline (2026-04-29)

---

## How to Use

Run this checklist after any code change to `scripts/`, `rules/`, or `app.py`.
Each section has a command to run and expected output to compare.

---

## 1. Syntax Check

All pipeline scripts must parse without errors.

```bash
cd ai_rfx_streamlit_dev
python -c "
import ast
for f in ['app.py','scripts/llm_client.py','scripts/extract_requirements_llm.py',
          'scripts/run_case.py','scripts/postprocess_requirements.py','scripts/export_excel.py',
          'scripts/responses_manager.py']:
    ast.parse(open(f, encoding='utf-8').read())
print('All 7 files: syntax OK')
"
```

Expected: `All 7 files: syntax OK`

---

## 2. Postprocess Regression (fastest, no LLM needed)

Tests Step 3 only. Uses existing `requirements_enriched.json` as fixed input.

```bash
for case in "2018_SilverPeak_EC-XS" \
            "20210516_Nokia RFP for 3rd Generation Network Services Gateways" \
            "2022_10_12_AtlasRFQ" \
            "20260129_IBM_RFQ" \
            "aa"; do
    python scripts/postprocess_requirements.py \
        --in "runs/$case/requirements_enriched.json" \
        --out_dir "runs/$case" 2>&1 | grep "counts"
done
```

### Expected Output (Controlled no-llm baseline)

| Case | Requirements | Glossary | Notes |
|------|-------------|----------|-------|
| SilverPeak | 149 | 8 | 28 |
| Nokia | 99 | 4 | 5 |
| AtlasRFQ | 28 | 0 | 0 |
| IBM | 182 | 31 | 35 |
| AA | 591 | 6 | 54 |

### Acceptable Variance

- If you changed `classify_item`, `filter_junk`, or `dedup_requirements`: numbers may change.
  Document the delta and reason.
- If you changed `build_rows` or normalize functions: numbers may shift between Req/Note.
  Verify the total (Req+Glo+Note) stays the same.
- If enriched.json was regenerated (e.g., re-ran Step 2): numbers WILL change.
  This is NOT a postprocess regression — re-baseline after verifying enrich changes.

### Bug Indicators

- Any case crashes (KeyError, TypeError, etc.) = **blocker**
- Requirements drops to 0 for a non-empty enriched.json = **blocker**
- Glossary or Notes suddenly contain items that were previously Requirements = **investigate**

---

## 3. Export Regression

Tests Step 4. Uses `requirements_clean.json` produced by Step 2 above.

```bash
for case in "2018_SilverPeak_EC-XS" \
            "20210516_Nokia RFP for 3rd Generation Network Services Gateways" \
            "2022_10_12_AtlasRFQ" \
            "20260129_IBM_RFQ" \
            "aa"; do
    python scripts/export_excel.py \
        --in "$(pwd)/runs/$case/requirements_clean.json" \
        --out "$(pwd)/runs/$case/compliance_matrix.xlsx" 2>&1 | grep -E "OK|Error"
done
```

Expected: 5x `[OK] Sheets: Compliance Matrix, Glossary, Notes`. Zero errors.

### Bug Indicators

- Any `Error` or `Traceback` = **blocker**
- Missing sheets in output = **blocker**

---

## 4. Schema Validation

Verifies `requirements_clean.json` matches the 15-field canonical schema.

```bash
python -c "
import json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path

CANONICAL = {'req_id','orig_req_id','type','must_level','category','owner','stakeholder',
             'status','requirement','risk_tags','risk_note','evidence_needed','next_action',
             'source','derived'}

cases = ['2018_SilverPeak_EC-XS',
         '20210516_Nokia RFP for 3rd Generation Network Services Gateways',
         '2022_10_12_AtlasRFQ', '20260129_IBM_RFQ', 'aa']
ok = True
for case in cases:
    items = json.loads(Path(f'runs/{case}/requirements_clean.json').read_text(encoding='utf-8')).get('items',[])
    if not items:
        print(f'{case[:20]:20s} | 0 items — skip')
        continue
    keys = set(items[0].keys())
    if keys != CANONICAL:
        ok = False
        print(f'{case[:20]:20s} | FAIL missing={CANONICAL-keys} extra={keys-CANONICAL}')
    else:
        print(f'{case[:20]:20s} | OK — 15 fields')
print(f'Overall: {\"PASS\" if ok else \"FAIL\"}')
"
```

Expected: All 5 cases `OK — 15 fields`. Overall `PASS`.

### Bug Indicators

- Missing canonical field = **blocker** (downstream consumers will break)
- Extra field (not in CANONICAL) = **warning** (add to schema.md if intentional)
- `redflag_tags` reappears = **bug** (deprecated field leaked back)

---

## 5. Status & Count Consistency

Verifies UI badge counts match Step 4 review counts.

```bash
python -c "
import json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path
from collections import Counter

cases = ['2018_SilverPeak_EC-XS',
         '20210516_Nokia RFP for 3rd Generation Network Services Gateways',
         '2022_10_12_AtlasRFQ', '20260129_IBM_RFQ', 'aa']
for case in cases:
    items = json.loads(Path(f'runs/{case}/requirements_clean.json').read_text(encoding='utf-8')).get('items',[])
    active = [i for i in items if i.get('status') != 'AUTO_SKIP']
    statuses = Counter(i['status'] for i in active)
    total = len(active)
    new = statuses.get('NEW',0)
    nr = statuses.get('NEED_REVIEW',0)
    label = case[:20]
    print(f'{label:20s} | NEW={new:4d} NR={nr:4d} Total={total:4d}')
"
```

### Expected Output

| Case | NEW | NR | Total |
|------|-----|-----|-------|
| SilverPeak | 128 | 21 | 149 |
| Nokia | 80 | 19 | 99 |
| AtlasRFQ | 0 | 28 | 28 |
| IBM | 118 | 64 | 182 |
| AA | 486 | 105 | 591 |

### Acceptable Variance

- If you changed `normalize_status` or `auto_status`: distribution shifts between NEW/NR.
  Total should remain the same.
- `PENDING` count should be 0 in controlled baseline (mapped to `NEW`).

### Bug Indicators

- `PENDING` appears in active items = **bug** (`normalize_status` not applied)
- NEW + NR != Total active = **bug** (unknown status value not mapped)
- Total active differs from Requirements count in Step 2 = **investigate**

---

## 6. Checklist Detection (AA case specific)

```bash
python -c "
import sys; sys.path.insert(0, 'scripts')
from pathlib import Path
from extract_requirements_llm import has_checklist_sheets

files = [
    ('Should detect',  'inbound/aa/rfq/(C) Quantum LE DC SRS v1.7 Compliance Table.xlsx'),
    ('Should NOT detect', 'inbound/aa/rfq/(D) Quantum RFP - Detailed Quote Template (LE and DC Series) v1.7.xlsx'),
]
for label, fp in files:
    result = has_checklist_sheets(Path(fp))
    expected = 'detect' in label.lower()
    ok = result == expected
    print(f'  {\"OK\" if ok else \"FAIL\"} {label}: {result}')
"
```

Expected: Both `OK`.

---

## 7. Derived Flag Verification

```bash
python -c "
import json, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from pathlib import Path

cases = {'AtlasRFQ': '2022_10_12_AtlasRFQ', 'IBM': '20260129_IBM_RFQ'}
for label, case in cases.items():
    items = json.loads(Path(f'runs/{case}/requirements_clean.json').read_text(encoding='utf-8')).get('items',[])
    derived = sum(1 for i in items if i.get('derived'))
    non_derived = sum(1 for i in items if not i.get('derived'))
    print(f'{label:10s} | derived={derived} non_derived={non_derived}')
"
```

### Expected

| Case | Derived | Non-derived |
|------|---------|-------------|
| AtlasRFQ | 28 | 0 |
| IBM | 0 | 248 |

### Bug Indicators

- IBM has derived > 0 = **bug** (strict extraction should never set derived)
- AtlasRFQ has derived < 28 = **bug** (spec_reference items lost derived flag)

---

## 8. Zero-Requirements Case

```bash
mkdir -p runs/_test_empty
echo '{"meta":{},"requirements":[]}' > runs/_test_empty/requirements_enriched.json
python scripts/postprocess_requirements.py \
    --in runs/_test_empty/requirements_enriched.json \
    --out_dir runs/_test_empty 2>&1
python scripts/export_excel.py \
    --in "$(pwd)/runs/_test_empty/requirements_clean.json" \
    --out "$(pwd)/runs/_test_empty/compliance_matrix.xlsx" 2>&1 | grep -E "OK|Error"
rm -rf runs/_test_empty
```

Expected:
- Postprocess: `counts: Requirements=0, Glossary=0, Notes=0`
- Export: `[OK] Sheets: Compliance Matrix, Glossary, Notes`
- No crash, no traceback.

---

## 9. Pre-Release Summary

Before declaring a release, verify all sections above pass, then fill in:

```
Date:           ____-__-__
Changed files:  ____________
Sections passed: 1[ ] 2[ ] 3[ ] 4[ ] 5[ ] 6[ ] 7[ ] 8[ ]
Baseline match:  [ ] Exact  [ ] Acceptable variance (documented below)
Variance notes:  _______________________________________________
Tested by:       _______________
```

---

## Appendix: Full Controlled Baseline Reference

Source: `docs/schema.md` — "Controlled no-llm baseline (2026-04-29)"

| Case | Req | Glo | Note | Total | NEW | NR | SKIP | Derived |
|------|-----|-----|------|-------|-----|----|------|---------|
| SilverPeak | 149 | 8 | 28 | 185 | 128 | 21 | 36 | 0 |
| Nokia | 99 | 4 | 5 | 108 | 80 | 19 | 9 | 0 |
| AtlasRFQ | 28 | 0 | 0 | 28 | 0 | 28 | 0 | 28 |
| IBM | 182 | 31 | 35 | 248 | 118 | 64 | 66 | 0 |
| AA | 591 | 6 | 54 | 651 | 486 | 105 | 60 | 0 |
