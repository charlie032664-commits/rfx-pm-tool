# -*- coding: utf-8 -*-
"""
Export compliance matrix Excel (3 sheets) from requirements_clean.json
(output of postprocess_requirements.py). Falls back to requirements_enriched.json (legacy).

Usage:
  python export_excel.py --case 20260129_IBM_RFQ
  python export_excel.py --in runs/20260129_IBM_RFQ/requirements_clean.json --out runs/20260129_IBM_RFQ/compliance_matrix.xlsx
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.formatting.rule import FormulaRule


HEADERS = [
    "Req ID",
    "Priority",
    "Category",
    "Responsible Team",
    "Compliance Status",
    "Requirement (Original)",
    "Risk Tags",
    "Our Response",
    "Gap / Notes",
    "Evidence",
    "Source",
]

HIDDEN_COLS: set = set()


def read_json(p: Path) -> Dict[str, Any]:
    return json.loads(p.read_text(encoding="utf-8"))


def _category_str(category) -> str:
    if isinstance(category, list):
        return ", ".join(map(str, category))
    return str(category) if category is not None else ""


def _source_ref(r: Dict[str, Any]) -> str:
    import re
    s = r.get("source")

    def _clean_name(f):
        f = re.sub(r'\.(docx|doc|xlsx|xls|pdf|txt|md)$', '', str(f), flags=re.IGNORECASE)
        f = re.sub(r'^AUTO[-_]?', '', f, flags=re.IGNORECASE)
        f = re.sub(r'^(UNKNOWN|UnknownFile)$', '', f, flags=re.IGNORECASE)
        return f.strip()[:40]

    # dict 格式（requirements_enriched.json）
    if isinstance(s, dict):
        f = _clean_name(s.get("file") or "")
        if not f:
            return ""
        sheet = s.get("sheet") or ""
        row = s.get("row")
        table = s.get("table")
        table_row = s.get("table_row")
        page = s.get("page")
        chunk = s.get("chunk")
        if sheet and row is not None:
            return f"{f} \u2014 Sheet: {sheet}, \u7b2c {row} \u884c"
        if sheet:
            return f"{f} \u2014 Sheet: {sheet}"
        if table is not None and table_row is not None:
            return f"{f} \u2014 Table {table}, Row {table_row}"
        if page is not None:
            return f"{f} \u2014 \u7b2c {page} \u9801"
        if chunk is not None:
            return f"{f} \u2014 \u7b2c {chunk} \u6bb5"
        return f

    # 字串格式（requirements_clean.json）
    # 格式可能是：
    #   "TSR 2U 2S... — Table 2, Row 4"  (含位置資訊，直接使用)
    #   "TSR 2U 2S...#chunk5"             (舊格式，需解析)
    #   "TSR 2U 2S..."                    (只有檔名)
    if isinstance(s, str) and s:
        # 若已含有 — 分隔符，表示是格式化好的字串，直接使用
        if ' \u2014 ' in s:
            return s[:80]
        # 舊的 #chunk 格式
        if '#' in s:
            file_part, _, chunk_part = s.partition('#')
            file_part = _clean_name(file_part)
            if not file_part:
                return ""
            chunk_num = re.search(r'\d+', chunk_part)
            n = chunk_num.group() if chunk_num else chunk_part
            return f"{file_part} \u2014 \u7b2c {n} \u6bb5"
        # 只有檔名
        return _clean_name(s)

    return ""


def _load_reqs(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Accept both requirements_clean.json (items) and requirements_enriched.json (requirements)."""
    if "items" in data:
        out = []
        for item in (data["items"] or []):
            r = dict(item)
            # Normalize: ensure risk_tags is the canonical field
            if "risk_tags" not in r:
                r["risk_tags"] = r.get("redflag_tags") or r.get("redflag_messages") or []
            out.append(r)
        return out
    # Enriched format: redflag_messages → risk_tags
    out = []
    for req in (data.get("requirements", []) or []):
        r = dict(req)
        if "risk_tags" not in r:
            r["risk_tags"] = r.get("redflag_tags") or r.get("redflag_messages") or []
        out.append(r)
    return out


def split_sheets(reqs: List[Dict[str, Any]]) -> Tuple[List, List, List]:
    glossary, notes, main = [], [], []
    for r in reqs:
        status = str(r.get("status") or "")
        cat_str = _category_str(r.get("category", []))
        owner = str(r.get("owner") or "")
        must_level = str(r.get("must_level") or "").upper()
        req_text = str(r.get("requirement") or "")

        is_glossary = (status == "AUTO_SKIP") or ("glossary" in cat_str.lower())
        if is_glossary:
            glossary.append(r)
            continue

        msgs = r.get("risk_tags") or r.get("redflag_tags") or r.get("redflag_messages") or []
        has_redflag = bool(msgs and str(msgs) not in ("[]", ""))
        req_type = str(r.get("type") or "")

        is_note = (
            must_level in ("INFO", "")
            and status != "NEED_REVIEW"
            and not has_redflag
            and req_type in ("note", "junk")
        )
        if is_note:
            notes.append(r)
        else:
            main.append(r)

    return main, glossary, notes


def apply_sheet_style(ws) -> None:
    header_font  = Font(bold=True, color="FFFFFF", name="Arial")
    header_fill  = PatternFill("solid", fgColor="1F4E79")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    wrap = Alignment(wrap_text=True, vertical="top")
    top  = Alignment(vertical="top")
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)

    for c in range(1, ws.max_column + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = header_align
        cell.border = border

    ws.freeze_panes = "A2"

    wrap_cols = {
        "Requirement (Original)",
        "Risk Tags",
        "Our Response",
        "Gap / Notes",
        "Evidence",
    }

    for row in range(2, ws.max_row + 1):
        for col in range(1, ws.max_column + 1):
            cell = ws.cell(row=row, column=col)
            cell.border = border
            h = ws.cell(row=1, column=col).value
            cell.alignment = wrap if h in wrap_cols else top

    ws.auto_filter.ref = f"A1:{get_column_letter(ws.max_column)}{ws.max_row}"

    col_width = {h: 18 for h in HEADERS}
    col_width.update({
        "Req ID": 15,
        "Priority": 12,
        "Category": 15,
        "Responsible Team": 14,
        "Compliance Status": 18,
        "Requirement (Original)": 60,
        "Risk Tags": 22,
        "Our Response": 50,
        "Gap / Notes": 35,
        "Evidence": 35,
        "Source": 40,
    })
    for idx, h in enumerate(HEADERS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = col_width.get(h, 18)
        if h in HIDDEN_COLS:
            ws.column_dimensions[get_column_letter(idx)].hidden = True

    # Conditional formatting by Status
    if ws.max_row >= 2:
        last_col = get_column_letter(ws.max_column)
        data_range = f"A2:{last_col}{ws.max_row}"
        sc = HEADERS.index("Compliance Status") + 1
        sl = get_column_letter(sc)
        _status_colors = [
            ("NEED_REVIEW",        "FFF2CC"),  # warm yellow
            ("NEW",                "E3F2FD"),  # light blue
            ("INTERNAL_ALIGN",     "E8EAF6"),  # light indigo
            ("ASK_CUSTOMER",       "FFF3E0"),  # light orange
            ("READY_FOR_RESPONSE", "EAF3DE"),  # light green
            ("CLOSED",             "F5F5F5"),  # light grey
            ("AUTO_SKIP",          "E7E6E6"),  # grey
        ]
        for _sv, _clr in _status_colors:
            ws.conditional_formatting.add(data_range, FormulaRule(
                formula=[f'${sl}2="{_sv}"'],
                fill=PatternFill("solid", fgColor=_clr)
            ))
        mc = HEADERS.index("Priority") + 1
        ml = get_column_letter(mc)
        ws.conditional_formatting.add(
            f"{ml}2:{ml}{ws.max_row}",
            FormulaRule(formula=[f'${ml}2="MUST"'], fill=PatternFill("solid", fgColor="F8CBAD"))
        )


def write_sheet(ws, reqs: List[Dict[str, Any]]) -> None:
    ws.append(HEADERS)

    for r in reqs:
        req_id      = r.get("req_id", "")
        must_level  = r.get("must_level", "")
        category    = _category_str(r.get("category", []))
        owner       = r.get("owner", "")
        status      = r.get("status", "")
        req_text    = r.get("requirement", "")
        source      = _source_ref(r)

        risk_tags = r.get("risk_tags") or r.get("redflag_tags") or r.get("redflag_messages") or []
        risk_str = ", ".join(risk_tags) if isinstance(risk_tags, list) else str(risk_tags)

        our_response = r.get("vendor_comment") or r.get("our_response") or ""
        gap_notes    = r.get("gap") or ""
        evidence     = r.get("evidence_needed") or r.get("evidence") or ""

        ws.append([
            req_id, must_level, category, owner, status,
            req_text, risk_str,
            our_response, gap_notes, evidence,
            source,
        ])

    apply_sheet_style(ws)

    # Colour-code Req ID: RFQ- green, AI- blue
    for row in range(2, ws.max_row + 1):
        cell = ws.cell(row=row, column=1)
        val = str(cell.value or "")
        if val.startswith("RFQ-"):
            cell.fill = PatternFill("solid", fgColor="EAF3DE")
            cell.font = Font(name="Arial", size=10, color="27500A")
        elif val.startswith("AI-"):
            cell.fill = PatternFill("solid", fgColor="E6F1FB")
            cell.font = Font(name="Arial", size=10, color="0C447C")


def export_excel(data: Dict[str, Any], out_xlsx: Path, responses_path: Optional[Path] = None) -> None:
    # Load responses.json (owner-filled data)
    responses: Dict[str, Any] = {}
    if responses_path and Path(responses_path).exists():
        try:
            responses = json.loads(Path(responses_path).read_text(encoding="utf-8"))
            print(f"[INFO] Loaded {len(responses)} responses from {responses_path}")
        except Exception as e:
            print(f"[WARN] Failed to load responses: {e}")

    wb = Workbook()
    wb.remove(wb.active)

    reqs = _load_reqs(data)
    main_reqs, glossary_reqs, notes_reqs = split_sheets(reqs)

    # Merge owner responses into main_reqs
    for r in main_reqs:
        rid = r.get("req_id", "")
        resp = responses.get(rid, {})
        if resp:
            if resp.get("status"):
                r["status"] = resp["status"]
            if resp.get("vendor_comment"):
                r["vendor_comment"] = resp["vendor_comment"]
            if resp.get("gap"):
                r["gap"] = resp["gap"]
            if resp.get("ai_draft"):
                r["ai_draft"] = resp["ai_draft"]

    # ── PM 排序：NEED_REVIEW 先 > 有 Redflag 先 > MUST 先 > Category ──────────
    _status_order = {"NEED_REVIEW": 0, "INTERNAL_ALIGN": 1, "ASK_CUSTOMER": 2,
                     "NEW": 3, "READY_FOR_RESPONSE": 4, "CLOSED": 5, "AUTO_SKIP": 6}
    _must_order   = {"MUST": 0, "SHOULD": 1, "MAY": 2, "INFO": 3}
    _cat_order    = {
        "Compliance": 0, "Reliability": 1, "Security": 2,
        "Platform": 3, "BMC": 4, "BIOS": 5, "Storage": 6,
        "PCIe": 7, "Power": 8, "Thermal": 9, "Mechanical": 10, "General": 99,
    }
    def _sort_key(r):
        msgs = r.get("risk_tags") or r.get("redflag_tags") or r.get("redflag_messages") or []
        has_rf = 0 if (msgs and str(msgs) not in ("[]", "")) else 1
        return (
            _status_order.get(str(r.get("status", "")), 9),
            has_rf,
            _must_order.get(str(r.get("must_level", "")), 9),
            _cat_order.get(_category_str(r.get("category", [])).split(",")[0].strip(), 50),
        )
    main_reqs = sorted(main_reqs, key=_sort_key)

    write_sheet(wb.create_sheet("Compliance Matrix"), main_reqs)
    write_sheet(wb.create_sheet("Glossary"),          glossary_reqs)
    write_sheet(wb.create_sheet("Notes"),             notes_reqs)

    out_xlsx.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_xlsx)


def find_latest_clean(runs_dir: Path) -> Optional[Path]:
    for pattern in ("**/requirements_clean.json", "**/requirements_enriched.json"):
        candidates = list(runs_dir.glob(pattern))
        if candidates:
            candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return candidates[0]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in",        dest="in_path",       default=None)
    ap.add_argument("--out",       dest="out_path",      default=None)
    ap.add_argument("--case",      dest="case_id",       default=None)
    ap.add_argument("--responses", dest="responses_path", default=None)
    args = ap.parse_args()

    base_dir = Path(__file__).resolve().parent
    runs_dir = base_dir / "runs"

    in_path: Optional[Path] = None
    if args.in_path:
        in_path = Path(args.in_path)
        if not in_path.is_absolute():
            in_path = (base_dir / in_path).resolve()
    elif args.case_id:
        clean    = runs_dir / args.case_id / "requirements_clean.json"
        enriched = runs_dir / args.case_id / "requirements_enriched.json"
        in_path  = clean if clean.exists() else enriched
    else:
        in_path = find_latest_clean(runs_dir)

    if not in_path or not in_path.exists():
        raise FileNotFoundError(
            f"requirements_clean.json not found.\n"
            f"Run postprocess_requirements.py first, or specify --in <path>"
        )

    out_path = Path(args.out_path) if args.out_path else in_path.parent / "compliance_matrix.xlsx"
    if not out_path.is_absolute():
        out_path = (base_dir / out_path).resolve()

    data = read_json(in_path)
    responses_path = Path(args.responses_path) if args.responses_path else None
    export_excel(data, out_path, responses_path=responses_path)

    print(f"[OK] Input : {in_path}")
    print(f"[OK] Output: {out_path}")
    print("[OK] Sheets: Compliance Matrix, Glossary, Notes")


if __name__ == "__main__":
    main()
