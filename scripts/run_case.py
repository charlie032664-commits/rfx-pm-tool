# -*- coding: utf-8 -*-
"""
run_case.py (Full, overwrite-ready)

Pipeline:
- Read one case folder: inbound/<case_id>/
- Build manifest.json from inbound/<case_id>/rfq/*
- Load rules from rules/:
  - must_level_map.yaml
  - owner_map.yaml (category_to_owner + keyword_to_owner)
  - redflags.yaml
  - category_map.yaml
- Read requirements.json from runs/<case_id>/requirements.json (produced by extractor)
- Enrich requirements:
  - must_level
  - category
  - owner
  - status
  - redflags
  - glossary skip
- Output: runs/<case_id>/requirements_enriched.json

Usage (from scripts/ai_rfx):
  python run_case.py --case .\\inbound\\20260129_IBM_RFQ --rules .\\rules --runs .\\runs
  python run_case.py --case .\\inbound\\20260129_IBM_RFQ --rules .\\rules --runs .\\runs --use-mock
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

import sys

import httpx
import yaml  # pip install pyyaml

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
from openai import OpenAI

from llm_client import get_client, get_model, is_available


# -----------------------------
# Helpers
# -----------------------------
def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def read_yaml_optional(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, obj: Dict[str, Any]) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256_file(p: Path, buf_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            chunk = f.read(buf_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def build_manifest(rfq_dir: Path) -> Dict[str, Any]:
    files = []
    for p in sorted(rfq_dir.glob("**/*")):
        if p.is_dir():
            continue
        stat = p.stat()
        files.append({
            "path": str(p),
            "name": p.name,
            "size": stat.st_size,
            "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
            "sha256": sha256_file(p),
        })
    return {"generated_at": now_iso(), "file_count": len(files), "files": files}


# -----------------------------
# Must-level logic
# -----------------------------
def detect_must_level(text: str, must_map: Dict[str, Any]) -> str:
    """
    Return one of: MUST/SHOULD/MAY/INFO
    Keyword matching based on must_level_map.yaml
    """
    t = (text or "").lower()
    patterns = (must_map or {}).get("patterns", {})

    for label in ["MUST", "SHOULD", "MAY", "INFO"]:
        rule = patterns.get(label, {})
        any_list = rule.get("any", []) or []
        for kw in any_list:
            if kw.lower() in t:
                return label
    return "INFO"


def default_must_for_table_rows(must_level: str, req_id: str, source: Dict[str, Any]) -> str:
    """
    If this looks like table row IDs (HOST/BIOS/BMC), default MUST even if text lacks must/shall.
    """
    if must_level != "INFO":
        return must_level

    rid = (req_id or "").upper().strip()
    if rid.startswith(("HOST-", "BIOS-", "BMC-")):
        return "MUST"

    src = source or {}
    row_name = str(src.get("row") or "").strip().upper()
    table_name = str(src.get("table") or "").strip().lower()

    if row_name.startswith(("HOST", "BIOS", "BMC")):
        return "MUST"
    if "requirements" in table_name:
        return "MUST"

    return must_level


# -----------------------------
# Glossary / definition filter
# -----------------------------
def is_glossary_definition(requirement: str, notes: str) -> bool:
    """
    Identify acronym/definition lines, e.g. "CRU: Customer Replaceable Unit"
    """
    req_text = (requirement or "").strip()
    n = (notes or "").strip().upper()

    if "GLOSSARY/DEFINITION" in n or "GLOSSARY" in n or "DEFINITION" in n:
        return True

    # Acronym definition: 2~15 chars (letters/numbers/ -/ ) + ":" + something
    # Examples: "IBM: International Business Machines", "FRU: Field Replaceable Unit"
    if re.match(r"^[A-Z0-9][A-Z0-9 \-/]{1,15}\s*:\s*.+", req_text):
        return True

    return False


# -----------------------------
# Category logic (category_map.yaml)
# -----------------------------
def classify_category(req_id: str, text: str, notes: str, cmap: Dict[str, Any]) -> List[str]:
    rid = (req_id or "").strip()
    t = (text or "")
    n = (notes or "")

    # glossary notes hit
    for kw in (cmap.get("glossary_notes_hit") or []):
        if kw.lower() in n.lower():
            return ["Glossary"]

    # id prefix map
    for prefix, cats in (cmap.get("id_prefix_map") or {}).items():
        if rid.startswith(prefix):
            return cats

    # keyword map
    for cat, kws in (cmap.get("keyword_map") or {}).items():
        for kw in kws:
            if kw.lower() in t.lower():
                return [cat]

    return cmap.get("default_category") or ["General"]


# -----------------------------
# Owner logic (owner_map.yaml)
# -----------------------------
def owner_from_category(categories: List[str], owner_rules: Dict[str, Any]) -> str:
    m = (owner_rules or {}).get("category_to_owner", {})
    for c in categories:
        if c in m:
            return m[c]
    return "TBD"


def owner_from_keywords(text: str, owner_rules: Dict[str, Any]) -> str:
    """
    If category mapping fails, use keyword_to_owner to assign.
    owner_map.yaml: keyword_to_owner: { OwnerName: [kw1, kw2, ...] }
    """
    t = (text or "").lower()
    km = (owner_rules or {}).get("keyword_to_owner", {}) or {}
    for owner, kws in km.items():
        for kw in kws:
            if kw.lower() in t:
                return owner
    return ""


# -----------------------------
# Redflags logic (redflags.yaml)
# -----------------------------
def apply_redflags(text: str, notes: str, redflags: Dict[str, Any]) -> Tuple[str, List[str], Dict[str, Any]]:
    """
    Apply redflag rules.
    Overrides are "first-hit wins" to prevent later rules overwriting earlier decisions.
    """
    t = (text or "").lower()
    n = (notes or "").lower()

    messages: List[str] = []
    overrides: Dict[str, Any] = {}

    rules = (redflags or {}).get("rules", []) or []
    status = "AUTO_OK"

    for r in rules:
        match = r.get("match", {})
        kws = match.get("any_keywords", []) or []
        hit = any((kw or "").lower() in t for kw in kws) or any((kw or "").lower() in n for kw in kws)

        if not hit:
            continue

        action = r.get("action", {})
        msg = action.get("message") or f"Redflag hit: {r.get('id', 'UNKNOWN')}"
        messages.append(msg)

        if "force_status" in action:
            status = action["force_status"]

        for k in ["force_owner", "force_category", "require_evidence", "forbid_llm_numbers"]:
            if k in action and k not in overrides:
                overrides[k] = action[k]

    return status, messages, overrides


# -----------------------------
# Mock (optional)
# -----------------------------
def mock_requirements() -> Dict[str, Any]:
    return {
        "meta": {"doc_name": "mock", "extracted_at": now_iso()},
        "requirements": [
            {
                "req_id": "BIOS-4",
                "source": {"section": "Table 3", "table": "BIOS Requirements", "row": "BIOS 4"},
                "requirement": "PXE boot support from management I/O ports. PXE activated by pressing 'L' on serial console during BIOS boot.",
                "notes": "",
                "confidence": 0.9
            },
            {
                "req_id": "HOST-36",
                "source": {"section": "4.3", "table": "Table 1: Host System Requirements", "row": "HOST 36"},
                "requirement": "FIPS 140-2 Level 2 compliance. Visual opacity shall restrict observation through vents.",
                "notes": "",
                "confidence": 0.9
            },
            {
                "req_id": "AUTO-mock-1-001",
                "source": {"file": "mock.docx", "chunk": 1},
                "requirement": "IBM: International Business Machines",
                "notes": "GLOSSARY/DEFINITION",
                "confidence": 0.6
            },
        ]
    }


# -----------------------------
# LLM enrich helpers
# -----------------------------
def build_enrich_prompt(requirement: str, notes: str, rules_context: str) -> str:
    return f"""
你是 RFQ/RFI 文件的需求分析專家。

請分析以下這條 requirement，輸出 JSON（不要任何解釋或 markdown）：

Requirement: {requirement}
Notes: {notes}

判斷規則參考：
{rules_context}

輸出格式：
{{
  "type": "requirement | glossary | note",
  "must_level": "MUST | SHOULD | MAY | INFO",
  "category": "（只能選以下一個）Platform | BMC | BIOS | Storage/HW | Storage/SW | Mechanical | Power | Thermal | Security | Compliance | Reliability | Documentation | Legal | Commercial | General",
  "owner": "（只能選以下一個）EE/Platform | EE/Storage | SW/Storage | SE/Storage | SE/System | AE/Storage | BMC | BIOS | ME | Thermal | Power | QA | Legal | PM | TBD",
  "stakeholder": ["（可多個，從 owner 列表選，不包含 owner 本人）"],
  "redflag": [],
  "redflag_message": "",
  "status": "PENDING | NEED_REVIEW | AUTO_SKIP"
}}

Category 說明：
- Platform：CPU/Memory/PCIe/Chipset/Platform 相關
- BMC：BMC 韌體功能
- BIOS：BIOS 韌體功能
- Storage/HW：儲存硬體（HBA/NVMe/SAS/HDD/cable/tray）
- Storage/SW：儲存軟體（RAID feature/driver/firmware）
- Mechanical：機構/外觀/結構/bezel
- Power：電源/PSU
- Thermal：散熱/fan/airflow
- Security：TPM/安全硬化
- Compliance：認證/法規（CE/FCC/RoHS/FIPS）
- Reliability：可靠度測試（MTBF/HALT/vibration/shock）
- Documentation：文件交付要求
- Legal：法務/IP/保密協議
- Commercial：商務/付款/交期/報價
- General：其他無法分類

Owner 說明：
- EE/Platform：Platform EE 團隊
- EE/Storage：Storage EE 團隊（硬體）
- SW/Storage：Storage SW 團隊（feature/driver）
- SE/Storage：Storage SE（RAID card/cable）
- SE/System：System SE（cable routing/assembly）
- AE/Storage：Storage AE（validation）
- BMC：BMC 韌體團隊
- BIOS：BIOS 韌體團隊
- ME：機構工程團隊
- Thermal：散熱團隊
- Power：電源團隊
- QA：品質/認證團隊
- Legal：法務團隊
- PM：專案管理（Commercial/Documentation/Legal 主責）
- TBD：待指派

Stakeholder 判斷規則：
- 有 redflag 時，PM 必須列為 stakeholder
- status = NEED_REVIEW 時，PM 必須列為 stakeholder
- Compliance/Reliability → stakeholder 加 PM、QA
- Storage/HW → stakeholder 加 ME（tray/cable routing）、SE/Storage
- Storage/SW → stakeholder 加 EE/Storage、SE/Storage
- Power → stakeholder 加 ME（airflow/layout影響）
- Mechanical → stakeholder 加 ME 相關團隊
- BMC 和 BIOS 有交叉時（DMI/platform info）→ 互為 stakeholder
- Commercial/Legal → stakeholder 加 PM、Legal
- 一般技術需求且無風險 → stakeholder 可以為空

type 判斷規則（嚴格）：
- type=note（以下情況）：
  * RFP/RFQ 流程說明（截止日期、提交說明、操作指示）
  * Excel 操作說明（"Do not modify this cell"、"Click Submit"）
  * 純數字、日期、版本號（"1.2"、"05/25/20"）
  * 問卷式問題（"Do you have..."、"Describe your..."）
  * 單句陳述沒有明確要求（"RFP Issued: 5/29/2020"）
- type=glossary：名詞定義、縮寫解釋
- type=requirement：其他有明確可驗證要求的

status 判斷規則（重要）：
- status=PENDING：預設值。一般技術規格，Owner 確認後即可交付，無特殊風險。
  大部分需求都應該是 PENDING。
- status=NEED_REVIEW：只有以下情況才使用，不要濫用：
  * 有 redflag（認證/可靠度/價格/交期/IP 等）
  * 涉及認證或法規（CE/FCC/RoHS/FIPS/UL/GB4943）
  * 涉及可靠度測試（MTBF/HALT/ALT/vibration/shock）
  * 涉及付款條件、交期或罰則
  * 涉及 IP 授權、保密協議
  * 有明顯 gap 或風險（例：要求我方目前無法達到的規格）
- status=AUTO_SKIP：type=glossary 或 type=note

redflag 判斷要保守：
- RF_CERT：明確提到認證標準名稱（FIPS、IEC60950、GB4943、UL、CE、FCC、RoHS）
- RF_RELIABILITY：明確提到可靠度測試（MTBF、HALT、ALT、vibration、shock、humidity、altitude）
- RF_PRICE：明確提到價格、付款條款
- RF_SCHEDULE：明確提到交期、罰則、SLA
- RF_IP：明確提到 IP 授權、source code、保密
- RF_ROADMAP：明確提到 7-year、EOL、lifecycle
- 一般 must/shall 不需要標 redflag
""".strip()


def enrich_with_llm(client, model: str, requirement: str, notes: str, rules_context: str) -> dict:
    import time
    prompt = build_enrich_prompt(requirement, notes, rules_context)
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=model,
                temperature=0.1,
                messages=[
                    {"role": "system", "content": "You output STRICT JSON only."},
                    {"role": "user", "content": prompt},
                ]
            )
            text = (resp.choices[0].message.content or "").strip()
            try:
                result = json.loads(text)
            except Exception:
                m = re.search(r"\{.*\}", text, flags=re.DOTALL)
                if m:
                    result = json.loads(m.group(0))
                else:
                    raise RuntimeError(f"Invalid JSON: {text[:200]}")
            if attempt == 0:
                print(f"[DEBUG] LLM result keys: {list(result.keys())}")
                print(f"[DEBUG] stakeholder: {result.get('stakeholder')}")
            return result
        except Exception as e:
            wait = min(2 ** attempt, 8)
            print(f"[WARN] LLM enrich attempt {attempt+1}/3 failed: {e} -> sleep {wait}s")
            time.sleep(wait)
    return {}


def build_rules_context(must_map, owner_rules, redflags, category_map) -> str:
    lines = []
    lines.append("Category 說明：")
    for cat, kws in (category_map.get("keyword_map") or {}).items():
        lines.append(f"  {cat}: {', '.join(kws[:5])}")
    lines.append("\nRedflag 說明：")
    for rule in (redflags.get("rules") or []):
        kws = rule.get("match", {}).get("any_keywords", [])[:5]
        lines.append(f"  {rule['id']}: {', '.join(kws)}")
    return "\n".join(lines)


# -----------------------------
# Enrich
# -----------------------------
def enrich_requirements(
    req_doc: Dict[str, Any],
    must_map: Dict[str, Any],
    owner_rules: Dict[str, Any],
    redflags: Dict[str, Any],
    category_map: Dict[str, Any],
    no_llm: bool = False,
) -> Dict[str, Any]:

    if no_llm:
        print("[INFO] --no-llm flag set — using keyword matching only")
        use_llm = False
    elif not is_available():
        print("[WARN] LLM not configured — falling back to keyword matching "
              "(set OPENAI_API_KEY, or LLM_PROVIDER=internal with INTERNAL_LLM_*)")
        use_llm = False
    else:
        use_llm = True
    client = None
    model = None
    if use_llm:
        client = get_client()
        model = get_model()
        rules_context = build_rules_context(must_map, owner_rules, redflags, category_map)
        print(f"[INFO] LLM enrich enabled (model={model})")

    out = dict(req_doc)
    enriched: List[Dict[str, Any]] = []

    reqs: List[Dict[str, Any]] = req_doc.get("requirements", []) or []

    for r in reqs:
        req_id = r.get("req_id", "")
        text = r.get("requirement", "")
        notes = r.get("notes", "")
        source = r.get("source") or {}

        # 0) Glossary skip (deterministic)
        if is_glossary_definition(text, notes):
            must_level = "INFO"
            categories = ["Glossary"]
            owner = "TBD"
            status = "AUTO_SKIP"
            msgs = ["名詞解釋/縮寫定義：AUTO_SKIP"]
            overrides = {"auto_skip": True}
            enriched.append({
                **r,
                "must_level": must_level,
                "category": categories,
                "owner": owner,
                "status": status,
                "redflag_messages": msgs,
                "redflag_overrides": overrides,
            })
            continue

        # 1) must level — preserve extract-time value if already set (e.g. checklist Priority)
        existing_ml = str(r.get("must_level") or "").upper()
        if existing_ml in ("MUST", "SHOULD", "MAY"):
            must_level = existing_ml
        else:
            must_level = detect_must_level(text, must_map)
            must_level = default_must_for_table_rows(must_level, req_id, source)

        # 2) category (deterministic via category_map.yaml)
        categories = r.get("category") or classify_category(req_id, text, notes, category_map)

        # 3) owner
        owner = r.get("owner") or owner_from_category(categories, owner_rules)
        if owner in ("TBD", "", None):
            o2 = owner_from_keywords(text, owner_rules)
            if o2:
                owner = o2

        # 4) redflags
        status, msgs, overrides = apply_redflags(text, notes, redflags)
        if status == "AUTO_OK":
            status = "PENDING"

        # apply overrides (first-hit wins already handled)
        if "force_category" in overrides:
            categories = overrides["force_category"]
        if "force_owner" in overrides:
            owner = overrides["force_owner"]

        # ── LLM enrich（優先）────────────────────────────────────────
        if use_llm and client:
            llm_result = enrich_with_llm(client, model, text, notes, rules_context)
            if llm_result:
                must_level  = llm_result.get("must_level") or must_level
                categories  = [llm_result.get("category")] if llm_result.get("category") else categories
                owner       = llm_result.get("owner") or owner
                status      = llm_result.get("status") or status
                rf_tags     = llm_result.get("redflag") or []
                rf_msg      = llm_result.get("redflag_message") or ""
                msgs        = [rf_msg] if rf_msg else []
                overrides   = {"require_evidence": True} if rf_tags else {}
                stakeholders = llm_result.get("stakeholder") or []
                if isinstance(stakeholders, str):
                    stakeholders = [stakeholders]
                enriched.append({
                    **r,
                    "must_level": must_level,
                    "category": categories,
                    "owner": owner,
                    "stakeholder": stakeholders,
                    "status": status,
                    "redflag_messages": msgs,
                    "redflag_overrides": overrides,
                    "llm_enriched": True,
                })
                continue  # 跳過 keyword matching

        # ── Keyword matching fallback ─────────────────────────────────
        enriched.append({
            **r,
            "must_level": must_level,
            "category": categories,
            "owner": owner,
            "status": status,
            "redflag_messages": msgs,
            "redflag_overrides": overrides,
        })

    out["enriched_at"] = now_iso()
    out["requirements"] = enriched
    return out


# -----------------------------
# Main
# -----------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True, help="Path to one case folder, e.g. inbound/20260129_IBM_RFQ")
    ap.add_argument("--rules", required=True, help="Path to rules folder, e.g. rules/")
    ap.add_argument("--runs", required=True, help="Path to runs folder, e.g. runs/")
    ap.add_argument("--use-mock", action="store_true", help="Use mock requirements (no extractor yet)")
    ap.add_argument("--no-llm",  action="store_true", help="Skip LLM enrichment, use keyword matching only")
    args = ap.parse_args()

    case_dir = Path(args.case)
    rules_dir = Path(args.rules)
    runs_root = Path(args.runs)

    rfq_dir = case_dir / "rfq"
    meta_case = case_dir / "meta" / "case.yaml"

    if not rfq_dir.exists():
        raise FileNotFoundError(f"rfq folder not found: {rfq_dir}")

    case_meta = read_yaml_optional(meta_case)
    case_id = case_meta.get("case_id") or case_dir.name

    out_dir = runs_root / case_id
    ensure_dir(out_dir)

    # 1) manifest
    manifest = build_manifest(rfq_dir)
    write_json(out_dir / "manifest.json", manifest)

    # 2) load rules
    must_map = read_yaml_optional(rules_dir / "must_level_map.yaml")
    owner_rules = read_yaml_optional(rules_dir / "owner_map.yaml")
    redflags = read_yaml_optional(rules_dir / "redflags.yaml")
    category_map = read_yaml_optional(rules_dir / "category_map.yaml")

    # 3) requirements input
    req_json_path = out_dir / "requirements.json"

    if args.use_mock:
        req_doc = mock_requirements()
        write_json(req_json_path, req_doc)
    else:
        if req_json_path.exists():
            req_doc = read_json(req_json_path)
        else:
            raise FileNotFoundError(
                f"requirements.json not found: {req_json_path}\n"
                f"Tip: run extractor first:\n"
                f"  python extract_requirements_llm.py --case {case_dir} --runs {runs_root}"
            )

    # 4) enrich + save
    enriched = enrich_requirements(req_doc, must_map, owner_rules, redflags, category_map, no_llm=args.no_llm)
    write_json(out_dir / "requirements_enriched.json", enriched)

    print(f"[OK] Case: {case_id}")
    print(f"[OK] Output: {out_dir}")
    print("[OK] Files: manifest.json, requirements.json, requirements_enriched.json")


if __name__ == "__main__":
    main()
