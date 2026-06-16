# -*- coding: utf-8 -*-
"""No-AI mock requirement extraction CLI for fast UI flow testing.

This tool intentionally uses only local, simple heuristics. It does not call an
LLM and should never be treated as final RFQ extraction quality.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, List


KEYWORD_RE = re.compile(
    r"\b(shall|must|required|support|comply|requirement|should)\b",
    re.IGNORECASE,
)
LIST_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[\.)]|[A-Za-z][\.)]|[-*])\s+\S+"
)
TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".rst",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".html",
    ".htm",
    ".xml",
}
SKIP_DIRS = {"__pycache__", ".git", ".venv", "venv"}
NOTE = "No-AI mock extraction for UI flow testing only"
WARNING = "For UI flow testing only; not final RFQ extraction quality"


def iter_input_files(case_dir: Path) -> Iterable[Path]:
    for path in sorted(case_dir.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_EXTENSIONS:
            yield path


def read_lines(path: Path) -> List[str]:
    for encoding in ("utf-8-sig", "utf-8", "cp950", "latin-1"):
        try:
            return path.read_text(encoding=encoding).splitlines()
        except UnicodeDecodeError:
            continue
    return []


def clean_candidate(line: str) -> str:
    text = re.sub(r"\s+", " ", line).strip()
    text = re.sub(r"^[\s>*-]+", "", text).strip()
    return text


def is_candidate(line: str) -> bool:
    text = line.strip()
    if len(text) < 8:
        return False
    if KEYWORD_RE.search(text):
        return True
    return bool(LIST_RE.match(text) and len(text.split()) >= 4)


def build_requirement(req_id: str, text: str, source_file: str, line_no: int) -> dict:
    return {
        "req_id": req_id,
        "text": text,
        "requirement": text,
        "category": "General",
        "must_level": "NEED_REVIEW",
        "status": "NEED_REVIEW",
        "owner": "",
        "source_file": source_file,
        "source_page": None,
        "source": {"file": source_file, "line": line_no},
        "notes": NOTE,
        "confidence": 0.0,
    }


def extract(case_dir: Path, limit: int) -> tuple[list[dict], int]:
    items: list[dict] = []
    seen: set[str] = set()
    files_scanned = 0

    for path in iter_input_files(case_dir):
        files_scanned += 1
        rel = path.relative_to(case_dir).as_posix()
        for line_no, raw_line in enumerate(read_lines(path), start=1):
            if not is_candidate(raw_line):
                continue
            text = clean_candidate(raw_line)
            key = text.casefold()
            if not text or key in seen:
                continue
            seen.add(key)
            req_id = f"MOCK-{len(items) + 1:03d}"
            items.append(build_requirement(req_id, text, rel, line_no))
            if len(items) >= limit:
                return items, files_scanned

    return items, files_scanned


def write_output(case_dir: Path, out_path: Path, requirements: list[dict], files_scanned: int) -> None:
    payload = {
        "meta": {
            "doc_name": "no_ai_mock_extracted",
            "case_id": case_dir.name,
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
            "model": "no_ai_mock",
            "file_count": files_scanned,
            "analysis_mode": "no_ai_mock",
            "generated_by": "mock_extract_requirements.py",
            "warning": WARNING,
        },
        "requirements": requirements,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a no-AI mock requirements JSON for fast UI flow testing."
    )
    parser.add_argument("--case-dir", required=True, help="Inbound case folder to scan.")
    parser.add_argument("--out", required=True, help="Output JSON path.")
    parser.add_argument("--limit", type=int, default=50, help="Maximum candidates to emit. Default: 50.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    case_dir = Path(args.case_dir)
    out_path = Path(args.out)
    limit = max(0, int(args.limit))

    if not case_dir.exists() or not case_dir.is_dir():
        raise SystemExit(f"case-dir not found or not a directory: {case_dir}")

    requirements, files_scanned = extract(case_dir, limit)
    write_output(case_dir, out_path, requirements, files_scanned)

    print(f"Files scanned: {files_scanned}")
    print(f"Requirements written: {len(requirements)}")
    print(f"Output: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
