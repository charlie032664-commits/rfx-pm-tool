import getpass
import json
import os
import re
import socket
import subprocess
import sys
import pandas as pd
import streamlit as st
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from scripts.responses_manager import ResponsesManager

BASE_DIR       = Path(__file__).parent
INBOUND_DIR    = BASE_DIR / "inbound"
RUNS_DIR       = BASE_DIR / "runs"
RESPONSES_DIR  = BASE_DIR / "responses"
AI_RFX_DIR     = BASE_DIR / "scripts"
PYTHON      = sys.executable                 # same venv that runs this app

OUTPUT_FILES = {
    # Priority files first (downloadable)
    "requirements_review.xlsx":   "Review Excel",
    "compliance_matrix.xlsx":     "Compliance Matrix",
    # Intermediate files
    "requirements.json":          "Requirements (raw)",
    "requirements_enriched.json": "Requirements (enriched)",
    "requirements_clean.json":    "Requirements (clean)",
    "requirements.partial.jsonl": "Requirements (partial / resume)",
    "manifest.json":              "Manifest",
}

STATUS_ICON = {"READY": "🟢", "IN_PROGRESS": "🔵", "DONE": "✅", "DRAFT": "⚪"}

st.set_page_config(page_title="RFX PM Tool", layout="wide")

# ── Global style injection ────────────────────────────────────────────────────
st.markdown("""
<style>
/* Primary action buttons — professional blue (overrides default red) */
button[kind="primary"] {
    background-color: #1565C0 !important;
    border-color:     #1565C0 !important;
    color:            white   !important;
    font-size:        1rem    !important;
    font-weight:      600     !important;
}
button[kind="primary"]:hover:not(:disabled) {
    background-color: #0D47A1 !important;
    border-color:     #0D47A1 !important;
}
button[kind="primary"]:disabled {
    background-color: #90A4AE !important;
    border-color:     #90A4AE !important;
    color:            #ECEFF1 !important;
}

/* Step subheaders — larger, darker */
h2 {
    font-size:   1.55rem  !important;
    font-weight: 700      !important;
    color:       #1A237E  !important;
}

/* Alert / info / success text — slightly larger */
.stAlert p { font-size: 1rem !important; }

/* Metric labels (Customer / Status / Language / RFQ Files) */
[data-testid="stMetricLabel"] {
    font-size:   1.0rem  !important;
    font-weight: 600     !important;
    color:       #455A64 !important;
}

/* Metric values */
[data-testid="stMetricValue"] > div {
    font-size:   1.9rem !important;
    font-weight: 700    !important;
}
</style>
""", unsafe_allow_html=True)

# session state init
if "pipeline_done"         not in st.session_state: st.session_state.pipeline_done         = False
if "pipeline_running"      not in st.session_state: st.session_state.pipeline_running      = False
if "pipeline_should_run"   not in st.session_state: st.session_state.pipeline_should_run   = False
if "pipeline_step_results" not in st.session_state: st.session_state.pipeline_step_results = []
if "pipeline_start_step"   not in st.session_state: st.session_state.pipeline_start_step   = 0
if "responses_manager"     not in st.session_state: st.session_state.responses_manager     = None


# ── Helpers ──────────────────────────────────────────────────────────────────

def list_cases():
    return sorted([d.name for d in INBOUND_DIR.iterdir() if d.is_dir()])


def create_case(case_id, customer, language, status):
    rfq_dir  = INBOUND_DIR / case_id / "rfq"
    meta_dir = INBOUND_DIR / case_id / "meta"
    rfq_dir.mkdir(parents=True, exist_ok=True)
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "case_id":  case_id,
        "customer": customer,
        "status":   status,
        "language": language,
        "use_kb":   {"product_specs": False, "past_rfq_answers": False},
    }
    with open(meta_dir / "case.yaml", "w", encoding="utf-8") as f:
        yaml.dump(meta, f, allow_unicode=True, sort_keys=False)


def save_uploaded_files(case_id, uploaded_files):
    rfq_dir = INBOUND_DIR / case_id / "rfq"
    rfq_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for uf in uploaded_files:
        dest = rfq_dir / uf.name
        dest.write_bytes(uf.getvalue())
        saved.append(uf.name)
    return saved


def read_run_counts(case_id: str) -> dict:
    """Read requirement counts from run output JSON files."""
    run_dir = RUNS_DIR / case_id
    counts  = {}
    if not run_dir.exists():
        return counts

    def _load(fname):
        p = run_dir / fname
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    enriched = _load("requirements_enriched.json")
    if enriched:
        reqs = enriched.get("requirements", [])
        counts["main"]        = len([r for r in reqs if r.get("status") != "AUTO_SKIP"])
        counts["need_review"] = len([r for r in reqs if r.get("status") == "NEED_REVIEW"])

    clean = _load("requirements_clean.json")
    if clean:
        items = clean.get("items", [])
        counts["glossary"] = len([i for i in items if i.get("type") == "glossary"])

    counts["files"] = sum(1 for f in run_dir.iterdir() if f.is_file())
    return counts


def read_progress_counts(case_id: str) -> dict:
    """Merge pipeline statuses with saved responses to produce progress counts."""
    counts = {"COMPLIANT": 0, "PARTIAL": 0, "NON-COMPLIANT": 0,
              "NEW": 0, "PENDING": 0, "NEED_REVIEW": 0, "total": 0}

    # Load responses.json (owner-filled data)
    responses: dict = {}
    resp_path = RESPONSES_DIR / case_id / "responses.json"
    if resp_path.exists():
        try:
            responses = json.loads(resp_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Load pipeline output — prefer clean, fall back to enriched
    reqs: list = []
    run_dir = RUNS_DIR / case_id
    for fname in ("requirements_clean.json", "requirements_enriched.json"):
        p = run_dir / fname
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                reqs = data.get("items") or data.get("requirements") or []
            except Exception:
                pass
            break

    if not reqs:
        return counts

    # Exclude AUTO_SKIP (glossary/notes) — same as Step 4
    active = [r for r in reqs if str(r.get("status") or "").upper() != "AUTO_SKIP"]
    counts["total"] = len(active)
    for r in active:
        req_id = str(r.get("req_id") or "")
        resp = responses.get(req_id, {})
        resp_status = resp.get("status", "") if isinstance(resp, dict) else ""
        if resp_status in ("COMPLIANT", "PARTIAL", "NON-COMPLIANT"):
            counts[resp_status] += 1
        else:
            pipeline_status = str(r.get("status") or "NEW").upper()
            key = pipeline_status if pipeline_status in counts else "NEW"
            counts[key] += 1

    return counts


def run_step(cmd: list) -> tuple[int, str]:
    """Run a subprocess and return (returncode, combined stdout+stderr)."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout + result.stderr).strip()
    return result.returncode, output


# ── File selection (Step 1.5: PM marks include/exclude before pipeline) ─────
# First version is ADVISORY: writes inbound/<case_id>/meta/file_selection.json
# but does NOT change which files the extractor actually processes.
# Phase 7 will wire enforcement into extract_requirements_llm.py.

def load_file_selection(case_id: str) -> dict:
    """Read inbound/<case>/meta/file_selection.json. Returns {} if missing
    or unreadable. Schema: {case_id, updated_at, updated_by, selections}."""
    p = INBOUND_DIR / case_id / "meta" / "file_selection.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_file_selection(case_id: str, selections: dict) -> None:
    """Write file_selection.json with nested-dict schema:
    selections = { filename: {"include": bool, "reason": str} }."""
    p = INBOUND_DIR / case_id / "meta" / "file_selection.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "case_id":    case_id,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "updated_by": getpass.getuser(),
        "selections": selections,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _get_file_role_confidence(doc_schema_info: dict | None, filename: str) -> tuple[str, str]:
    """Look up a file's role + confidence from doc_schema.json.

    Returns ("?", "—") when:
      - doc_schema is missing / not yet generated, OR
      - the file isn't found in either the new (per-file list) or old
        (main_files/appendix_files) doc_schema formats.
    """
    if not doc_schema_info:
        return ("?", "—")
    # New format: files: [{file, role, confidence, ...}]
    for f in (doc_schema_info.get("files") or []):
        if f.get("file") == filename:
            role = f.get("role", "?")
            conf = f.get("confidence")
            conf_str = f"{int(conf * 100)}%" if isinstance(conf, (int, float)) else "—"
            return (role, conf_str)
    # Old format: main_files / appendix_files lists with single overall confidence
    overall = doc_schema_info.get("confidence")
    conf_str = f"{int(overall * 100)}%" if isinstance(overall, (int, float)) else "—"
    appendix = doc_schema_info.get("appendix_files") or []
    main     = doc_schema_info.get("main_files") or []
    if filename in appendix or any(filename.startswith(af[:20]) for af in appendix if af):
        return ("appendix", conf_str)
    if filename in main or any(filename.startswith(mf[:20]) for mf in main if mf):
        return ("main_requirement", conf_str)
    return ("?", "—")


# ── Pipeline lock (case-level, advisory) ─────────────────────────────────────
# Lock file lives at: runs/<case_id>/.pipeline.lock
# Stale rule: file age > PIPELINE_LOCK_STALE_HOURS (2h).
# PID-dead is supplementary info only; it does NOT override the 2h rule.

PIPELINE_LOCK_STALE_HOURS = 2


def _lock_path(case_id: str) -> Path:
    return RUNS_DIR / case_id / ".pipeline.lock"


def _pid_alive(pid: int) -> bool:
    """Best-effort cross-platform PID liveness check.

    Used only for *displaying* informational warnings in the UI — never
    consulted when deciding whether a lock is stale (the 2-hour rule wins).
    Windows PID reuse may produce false positives; acceptable for an
    advisory single-machine lock.
    """
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes
            PROCESS_QUERY_INFO = 0x0400
            STILL_ACTIVE = 259
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_QUERY_INFO, False, pid_int)
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                ok = k.GetExitCodeProcess(h, ctypes.byref(code))
                return bool(ok and code.value == STILL_ACTIVE)
            finally:
                k.CloseHandle(h)
        else:
            os.kill(pid_int, 0)
            return True
    except Exception:
        return False


def read_lock_info(case_id: str) -> dict | None:
    """Return the parsed lock dict, or None if no lock file, or
    {"_invalid": True, ...} if the file exists but cannot be parsed."""
    p = _lock_path(case_id)
    if not p.exists():
        return None
    try:
        info = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(info, dict):
            return {"_invalid": True, "reason": "not a JSON object"}
        return info
    except Exception as e:
        return {"_invalid": True, "reason": f"unreadable: {e}"}


def is_lock_stale(info: dict | None) -> bool:
    """A lock is stale when either:
      - it is invalid/corrupt, OR
      - started_at is missing/unparseable, OR
      - age > PIPELINE_LOCK_STALE_HOURS.
    PID liveness is NOT consulted here (informational only).
    """
    if not info:
        return False
    if info.get("_invalid"):
        return True
    started_at = info.get("started_at", "")
    try:
        age = datetime.now() - datetime.fromisoformat(started_at)
    except Exception:
        return True
    return age > timedelta(hours=PIPELINE_LOCK_STALE_HOURS)


def acquire_lock(case_id: str, start_step: int) -> dict | None:
    """Atomically create the .pipeline.lock for case_id.

    If an existing lock is stale/invalid, it is replaced. If an existing
    lock is active, returns None (caller must abort). On success returns
    the freshly-written lock dict.
    """
    p = _lock_path(case_id)
    p.parent.mkdir(parents=True, exist_ok=True)

    if p.exists():
        existing = read_lock_info(case_id)
        if existing and not is_lock_stale(existing):
            return None  # active lock — refuse
        # Stale or invalid → safe to replace
        try:
            p.unlink()
        except Exception:
            pass

    info = {
        "case_id":    case_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "pid":        os.getpid(),
        "host":       socket.gethostname(),
        "user":       getpass.getuser(),
        "start_step": int(start_step),
    }
    try:
        # "x" mode = create-exclusive; fails if another writer raced us
        with open(p, "x", encoding="utf-8") as f:
            f.write(json.dumps(info, ensure_ascii=False, indent=2))
        return info
    except FileExistsError:
        return None


def release_lock(case_id: str) -> None:
    """Delete .pipeline.lock if present. Idempotent and never raises."""
    p = _lock_path(case_id)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass  # best-effort; another release path will catch it next time


# ── Sidebar ──────────────────────────────────────────────────────────────────

st.sidebar.title("RFX PM Tool")
mode = st.sidebar.radio("Mode", ["Select Existing Case", "Create New Case"])

if mode == "Select Existing Case":
    cases = list_cases()
    if not cases:
        st.sidebar.warning("No cases found in inbound/")
        st.stop()
    selected_case = st.sidebar.selectbox("Select Case", cases)
    creating = False

else:
    st.sidebar.markdown("---")
    new_case_id  = st.sidebar.text_input("Case ID",  placeholder="e.g. 20260319_Dell_RFQ")
    new_customer = st.sidebar.text_input("Customer", placeholder="e.g. Dell")
    new_language = st.sidebar.selectbox("Language", ["en", "zh", "ja", "ko", "de", "fr"])
    new_status   = st.sidebar.selectbox("Status",   ["READY", "DRAFT", "IN_PROGRESS", "DONE"])
    create_btn   = st.sidebar.button("Create Case", type="primary")
    creating     = True
    selected_case = new_case_id.strip()

    if create_btn:
        if not selected_case:
            st.sidebar.error("Case ID is required.")
        elif selected_case in list_cases():
            st.sidebar.error(f"Case '{selected_case}' already exists.")
        else:
            create_case(selected_case, new_customer.strip(), new_language, new_status)
            st.sidebar.success(f"Case '{selected_case}' created.")
            st.rerun()


# ── Main area ─────────────────────────────────────────────────────────────────

st.title("RFX PM Tool")

if creating and not selected_case:
    st.info("Fill in the Case ID on the left to create a new case.")
    st.stop()

# ── ResponsesManager init ─────────────────────────────────────────────────────
rm = ResponsesManager(RESPONSES_DIR, selected_case)
st.session_state.responses_manager = rm

# ── Progress statistics bar ───────────────────────────────────────────────────
_prog = read_progress_counts(selected_case)
if _prog["total"] > 0:
    _badge_css = (
        "display:inline-block;padding:2px 10px;border-radius:3px;"
        "font-size:0.9rem;font-weight:600;margin-right:6px;"
    )
    _parts = [
        (f"COMPLIANT: {_prog['COMPLIANT']}",     "#EAF3DE", "#27500A"),
        (f"PARTIAL: {_prog['PARTIAL']}",          "#FFF9C4", "#795B00"),
        (f"NON-COMPLIANT: {_prog['NON-COMPLIANT']}", "#FDECEA", "#B71C1C"),
        (f"NEW: {_prog['NEW']}",                  "#E3F2FD", "#0D47A1"),
        (f"NEED_REVIEW: {_prog['NEED_REVIEW']}",  "#FFF2CC", "#7B5B00"),
        (f"PENDING: {_prog['PENDING']}",          "#F5F5F5", "#455A64"),
        (f"Total: {_prog['total']}",              "#E3F2FD", "#0C447C"),
    ]
    badges_html = "".join(
        f'<span style="{_badge_css}background:{bg};color:{fg};">{label}</span>'
        for label, bg, fg in _parts
    )
    st.markdown(
        f'<div style="margin-bottom:8px;">{badges_html}</div>',
        unsafe_allow_html=True,
    )

# Case metadata
meta_path = INBOUND_DIR / selected_case / "meta" / "case.yaml"
if meta_path.exists():
    with open(meta_path, encoding="utf-8") as f:
        meta = yaml.safe_load(f)

    status_val = meta.get("status", "—")
    rfq_files  = list((INBOUND_DIR / selected_case / "rfq").glob("*"))
    rfq_count  = len([f for f in rfq_files if Path(f).is_file()])

    with st.container(border=True):
        st.markdown(
            f"<p style='font-size:1.05rem;font-weight:700;color:#1A237E;margin-bottom:6px;'>"
            f"Case: {selected_case}</p>",
            unsafe_allow_html=True,
        )
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Customer",  meta.get("customer", "—"))
        col2.metric("Status",    f"{STATUS_ICON.get(status_val, '⚪')} {status_val}")
        col3.metric("Language",  meta.get("language", "—"))
        col4.metric("RFQ Files", rfq_count)

        use_kb = meta.get("use_kb", {})
        if use_kb:
            def _kb_badge(label, enabled):
                bg  = "#E3F2FD" if enabled else "#ECEFF1"
                fg  = "#1565C0" if enabled else "#546E7A"
                txt = "on"      if enabled else "off"
                return (
                    f'<span style="display:inline-block;background:{bg};color:{fg};'
                    f'padding:3px 10px;border-radius:4px;font-size:0.95rem;'
                    f'font-weight:600;margin-right:6px;">{label}: {txt}</span>'
                )
            badges = (
                '<span style="font-size:0.95rem;font-weight:600;color:#455A64;margin-right:8px;">Knowledge Base</span>'
                + _kb_badge("product_specs",     use_kb.get("product_specs",     False))
                + _kb_badge("past_rfq_answers",  use_kb.get("past_rfq_answers",  False))
            )
            st.markdown(f'<div style="margin-top:8px;">{badges}</div>', unsafe_allow_html=True)
elif creating and selected_case:
    st.info(f"Case '{selected_case}' not yet created. Fill in the fields and click **Create Case**.")
    st.stop()
else:
    st.warning(f"case.yaml not found: {meta_path}")

st.divider()

# ── Upload RFQ files ──────────────────────────────────────────────────────────

st.subheader("Step 1: Upload RFQ Files")
rfq_dir = INBOUND_DIR / selected_case / "rfq"
uploaded = st.file_uploader(
    "Upload DOCX / XLSX / PDF files",
    type=["docx", "doc", "xlsx", "xls", "pdf", "txt", "md"],
    accept_multiple_files=True,
)
if uploaded:
    if st.button("Save Uploaded Files"):
        saved = save_uploaded_files(selected_case, uploaded)
        st.success(f"Saved {len(saved)} file(s): {', '.join(saved)}")

if rfq_dir.exists():
    existing = [f.name for f in sorted(rfq_dir.iterdir()) if f.is_file()]
    if existing:
        files_html = "  |  ".join(f"<code>{n}</code>" for n in existing)
        st.markdown(
            f"<p style='font-size:1.0rem;color:#455A64;margin-top:4px;'>📁 Files in rfq/:  {files_html}</p>",
            unsafe_allow_html=True,
        )

# ── Step 1.5: Review & Select RFQ Files (advisory; gated by Select Existing Case) ──
if mode == "Select Existing Case" and rfq_dir.exists():
    _rfq_files = sorted([f for f in rfq_dir.iterdir() if f.is_file()])
    if _rfq_files:
        st.subheader("Step 1.5: Review & Select RFQ Files")

        # Load doc_schema.json for role/confidence lookup; missing is OK
        _schema_path = INBOUND_DIR / selected_case / "meta" / "doc_schema.json"
        _doc_schema_for_table: dict | None = None
        if _schema_path.exists():
            try:
                _doc_schema_for_table = json.loads(_schema_path.read_text(encoding="utf-8"))
            except Exception:
                _doc_schema_for_table = None

        _SUPPORTED_EXT = {".docx", ".doc", ".xlsx", ".xls", ".pdf", ".txt", ".md"}
        _loaded = load_file_selection(selected_case)
        _loaded_sel = _loaded.get("selections") or {}

        _rows = []
        for _f in _rfq_files:
            _ext = _f.suffix.lower()
            try:
                _size = _f.stat().st_size
            except Exception:
                _size = 0
            if _size < 1024:
                _size_str = f"{_size} B"
            elif _size < 1024 * 1024:
                _size_str = f"{_size / 1024:.1f} KB"
            else:
                _size_str = f"{_size / 1024 / 1024:.2f} MB"
            _role, _conf_str = _get_file_role_confidence(_doc_schema_for_table, _f.name)
            _prev = _loaded_sel.get(_f.name, {})
            if not isinstance(_prev, dict):
                _prev = {}
            _rows.append({
                "Include":    bool(_prev.get("include", True)),
                "File":       _f.name,
                "Type":       _ext if _ext else "—",
                "Size":       _size_str,
                "Supported":  "✓" if _ext in _SUPPORTED_EXT else "⚠️",
                "Role":       _role,
                "Confidence": _conf_str,
                "Reason":     str(_prev.get("reason") or ""),
            })

        _total = len(_rows)
        _incl  = sum(1 for r in _rows if r["Include"])
        _excl  = _total - _incl
        _unsup = sum(1 for r in _rows if r["Supported"] != "✓")
        st.markdown(
            f"<p style='font-size:0.95rem;color:#455A64;margin-bottom:8px;'>"
            f"<b>{_total}</b> file(s) &nbsp;|&nbsp; included: <b>{_incl}</b> &nbsp;|&nbsp; "
            f"excluded: <b>{_excl}</b> &nbsp;|&nbsp; unsupported: <b>{_unsup}</b></p>",
            unsafe_allow_html=True,
        )

        _df = pd.DataFrame(_rows)
        _edited = st.data_editor(
            _df,
            column_config={
                "Include":    st.column_config.CheckboxColumn(
                    "Include", default=True,
                    help="Mark whether this file should feed the pipeline "
                         "(advisory only in Phase 3 — does not yet skip files)",
                ),
                "File":       st.column_config.TextColumn("File",       disabled=True),
                "Type":       st.column_config.TextColumn("Type",       disabled=True, width="small"),
                "Size":       st.column_config.TextColumn("Size",       disabled=True, width="small"),
                "Supported":  st.column_config.TextColumn("Supported",  disabled=True, width="small"),
                "Role":       st.column_config.TextColumn("Role",       disabled=True),
                "Confidence": st.column_config.TextColumn("Confidence", disabled=True, width="small"),
                "Reason":     st.column_config.TextColumn(
                    "Reason",
                    help="Optional note explaining why this file is excluded",
                ),
            },
            hide_index=True,
            use_container_width=True,
            key=f"file_selection_editor_{selected_case}",
        )

        # Compare edited widget state to the freshly-loaded df to detect unsaved edits
        _changed = not _edited.equals(_df)
        if _changed:
            st.warning("⚠️  Unsaved changes — click **Save Selection** to persist.")

        _save_label = "💾 Save Selection ∗" if _changed else "💾 Save Selection"
        if st.button(_save_label, key=f"save_file_selection_{selected_case}"):
            _new_sel: dict = {}
            for _, _row in _edited.iterrows():
                _new_sel[str(_row["File"])] = {
                    "include": bool(_row["Include"]),
                    "reason":  str(_row.get("Reason") or "").strip(),
                }
            save_file_selection(selected_case, _new_sel)
            st.success(f"Saved to inbound/{selected_case}/meta/file_selection.json")
            st.rerun()

        if _doc_schema_for_table is None:
            st.caption(
                "ℹ️ `doc_schema.json` not generated yet — Role / Confidence will "
                "appear after the first pipeline run."
            )

st.divider()

# ── Run Pipeline ──────────────────────────────────────────────────────────────

if mode == "Select Existing Case":
    st.subheader("Step 2: Run Pipeline")

    case_inbound = INBOUND_DIR / selected_case
    case_runs    = RUNS_DIR / selected_case

    _llm_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    has_api_key = bool(os.environ.get("OPENAI_API_KEY")) or _llm_provider == "internal"

    use_llm_enrich = st.checkbox(
        "Use LLM for Step 2 \u2014 Enrich (category / owner / stakeholder / redflag)",
        value=True,
        disabled=not has_api_key,
        help="勾選才呼叫 LLM 判斷，不勾則用 keyword matching（速度快，不花費用）",
    )

    cmd_step2 = [
        PYTHON,
        str(AI_RFX_DIR / "run_case.py"),
        "--case",  str(case_inbound),
        "--rules", str(BASE_DIR / "rules"),
        "--runs",  str(RUNS_DIR),
    ]
    if not use_llm_enrich:
        cmd_step2.append("--no-llm")

    cmd_step4 = [
        PYTHON,
        str(AI_RFX_DIR / "export_excel.py"),
        "--in",  str(case_runs / "requirements_clean.json"),
        "--out", str(case_runs / "compliance_matrix.xlsx"),
        "--responses", str(RESPONSES_DIR / selected_case / "responses.json"),
    ]

    PIPELINE_STEPS = [
        {
            "label": "Extract \u2014 \u8b80\u53d6 RFQ\uff0cAI \u63d0\u53d6\u9700\u6c42\u689d\u76ee",
            "ok_msg": "Requirements extracted from RFQ documents and saved.",
            "cmd": [
                PYTHON,
                str(AI_RFX_DIR / "extract_requirements_llm.py"),
                "--case",  str(case_inbound),
                "--runs",  str(RUNS_DIR),
                "--resume",
                "--max-chars", "600",
                "--group-size", "2",
            ],
            "requires_api_key": True,
        },
        {
            "label": "Enrich \u2014 AI \u5206\u6790\u5206\u985e / \u8ca0\u8cac\u4eba / \u98a8\u96aa",
            "ok_msg": "Category, owner, and must-level assigned to all requirements.",
            "cmd": cmd_step2,
            "requires_api_key": use_llm_enrich,
        },
        {
            "label": "Format \u2014 \u6574\u7406\u6392\u5e8f",
            "ok_msg": "Requirements classified and review sheet generated.",
            "cmd": [
                PYTHON,
                str(AI_RFX_DIR / "postprocess_requirements.py"),
                "--in",      str(case_runs / "requirements_enriched.json"),
                "--out_dir", str(case_runs),
            ],
            "requires_api_key": False,
        },
        {
            "label": "Export \u2014 \u8f38\u51fa Compliance Matrix",
            "ok_msg": "Compliance matrix exported and ready for distribution.",
            "cmd": cmd_step4,
            "requires_api_key": False,
        },
    ]

    if not has_api_key:
        st.warning("LLM not configured — set OPENAI_API_KEY or LLM_PROVIDER=internal. Steps 2–4 can still run if requirements.json already exists.")

    st.markdown(
        "<p style='font-size:1.0rem;color:#455A64;margin-bottom:8px;'>"
        "ℹ️ Step 1 may take several minutes for multiple files. "
        "Partial results are written during processing and the run can be resumed if interrupted.</p>",
        unsafe_allow_html=True,
    )

    # ── Step 1 status ──
    req_json_path = case_runs / "requirements.json"
    req_json_exists = req_json_path.exists() and req_json_path.stat().st_size > 100
    partial_path = case_runs / "requirements.partial.jsonl"
    partial_exists = partial_path.exists()

    if req_json_exists:
        try:
            _rj = json.loads(req_json_path.read_text(encoding="utf-8"))
            _rcount = len(_rj.get("requirements", []))
        except Exception:
            _rcount = "?"
        st.markdown(
            f"<p style='font-size:0.95rem;color:#2E7D32;margin-bottom:4px;'>"
            f"✅ requirements.json exists ({_rcount} requirements) — "
            f"use <b>Enrich + Format + Export</b> to continue, or Full Pipeline to re-extract</p>",
            unsafe_allow_html=True,
        )
    elif partial_exists:
        partial_lines = len([l for l in partial_path.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()])
        st.markdown(
            f"<p style='font-size:0.95rem;color:#1565C0;margin-bottom:4px;'>"
            f"📄 Partial exists — Step 1 will resume ({partial_lines} chunks done)</p>",
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            "<p style='font-size:0.95rem;color:#546E7A;margin-bottom:4px;'>"
            "⬜ No partial — Step 1 will run from scratch</p>",
            unsafe_allow_html=True,
        )

    # ── doc_schema 狀態顯示 ──
    schema_path = None
    doc_schema_info = None
    if selected_case:
        schema_path = BASE_DIR / "inbound" / selected_case / "meta" / "doc_schema.json"
        if schema_path.exists():
            try:
                doc_schema_info = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception:
                pass

    if doc_schema_info:
        confidence = doc_schema_info.get("confidence", 0)
        customer = doc_schema_info.get("customer", "")
        files_list = doc_schema_info.get("files", [])
        notes = doc_schema_info.get("notes", "")

        if files_list:
            # 新格式：per-file schema
            min_conf = min(f.get("confidence", 0) for f in files_list)
            if min_conf >= 0.7:
                st.success(
                    f"\U0001f4cb \u6587\u4ef6\u683c\u5f0f\u5df2\u8b58\u5225"
                    + (f"\uff08{customer}\uff09" if customer else "")
                    + f" | \u6700\u4f4e\u4fe1\u5fc3\u5ea6\uff1a{int(min_conf*100)}%"
                )
            else:
                st.warning(
                    f"\u26a0\ufe0f \u90e8\u5206\u6a94\u6848\u4fe1\u5fc3\u5ea6\u504f\u4f4e\uff08{int(min_conf*100)}%\uff09\uff0c"
                    f"\u5efa\u8b70\u78ba\u8a8d `inbound/{selected_case}/meta/doc_schema.json`"
                )

            # 顯示每個檔案的角色
            role_emoji = {
                "main_requirement": "\U0001f4c4",
                "commercial_requirement": "\U0001f4bc",
                "spec_reference": "\U0001f4ca",
                "questionnaire": "\U0001f4dd",
                "appendix": "\U0001f4ce",
            }
            role_label = {
                "main_requirement": "\u4e3b\u9700\u6c42",
                "commercial_requirement": "\u5546\u52d9/\u6cd5\u52d9",
                "spec_reference": "\u898f\u683c\u53c3\u8003",
                "questionnaire": "\u554f\u5377",
                "appendix": "\u9644\u4ef6\uff08\u8df3\u904e\uff09",
            }
            with st.expander("\U0001f4c1 \u5404\u6a94\u6848\u8655\u7406\u7b56\u7565", expanded=False):
                for f in files_list:
                    fname = f.get("file", "")
                    role = f.get("role", "unknown")
                    fmt = f.get("format", "")
                    rule = f.get("req_id_rule", "")
                    conf = f.get("confidence", 0)
                    emoji = role_emoji.get(role, "\u2753")
                    label = role_label.get(role, role)
                    st.markdown(
                        f"{emoji} **{fname}** \u2014 {label} `{fmt}`"
                        + (f" | req_id: `{rule}`" if rule and rule != "AI auto" else "")
                        + f" | \u4fe1\u5fc3\u5ea6 {int(conf*100)}%"
                    )
            if notes:
                st.info(f"\U0001f4dd PM \u5099\u8a3b\uff1a{notes}")
        else:
            # 舊格式：單一 schema
            fmt = doc_schema_info.get("rfq_format", "unknown")
            rule = doc_schema_info.get("req_id_rule", "")
            if confidence >= 0.7:
                st.success(
                    f"\U0001f4cb \u6587\u4ef6\u683c\u5f0f\u5df2\u8b58\u5225\uff1a**{fmt}**"
                    + (f"\uff08{customer}\uff09" if customer else "")
                    + f" | \u4fe1\u5fc3\u5ea6\uff1a{int(confidence*100)}%"
                    + (f" | req_id \u898f\u5247\uff1a{rule}" if rule else "")
                )
            else:
                st.warning(
                    f"\u26a0\ufe0f \u6587\u4ef6\u683c\u5f0f\u4fe1\u5fc3\u5ea6\u504f\u4f4e\uff08{int(confidence*100)}%\uff09\uff1a{fmt}"
                )
            if notes:
                st.info(f"\U0001f4dd PM \u5099\u8a3b\uff1a{notes}")
    else:
        st.info("\U0001f4a1 \u5c1a\u672a\u5206\u6790\u6587\u4ef6\u683c\u5f0f\uff0cRun Full Pipeline \u6642\u6703\u81ea\u52d5\u5206\u6790\u4e26\u5132\u5b58\u81f3 meta/doc_schema.json")

    # ── Step 1.5 advisory: warn if PM marked files excluded (not yet enforced) ──
    _selection_data = load_file_selection(selected_case)
    _excluded_files = [
        _name for _name, _info in (_selection_data.get("selections") or {}).items()
        if isinstance(_info, dict) and not _info.get("include", True)
    ]
    if _excluded_files:
        _shown = ", ".join(f"`{n}`" for n in _excluded_files[:3])
        if len(_excluded_files) > 3:
            _shown += f", … (+{len(_excluded_files) - 3} more)"
        st.info(
            f"ℹ️ **{len(_excluded_files)} file(s) marked excluded in Step 1.5**: {_shown}  \n"
            "First version is **advisory only** — the pipeline will still process "
            "all files in `rfq/`. (Phase 7 will enforce skipping.)"
        )

    # ── Pipeline lock state (computed BEFORE buttons so we can gate them) ──
    _lock_info   = read_lock_info(selected_case)
    _lock_stale  = is_lock_stale(_lock_info) if _lock_info else False
    _lock_active = bool(_lock_info) and not _lock_stale

    if _lock_active:
        _li = _lock_info or {}
        _pid_val = _li.get("pid")
        _pid_dead_hint = ""
        if _li.get("host") == socket.gethostname() and _pid_val and not _pid_alive(_pid_val):
            _pid_dead_hint = "  ⚠ PID no longer running on this host"
        st.warning(
            "🔒 **This case is currently being processed by another session.**  \n"
            f"started_at: `{_li.get('started_at', '?')}` · "
            f"host: `{_li.get('host', '?')}` · "
            f"pid: `{_pid_val}` · "
            f"user: `{_li.get('user', '?')}`  \n"
            f"start_step: `{_li.get('start_step', '?')}`"
            f"{_pid_dead_hint}"
        )
    elif _lock_info and _lock_stale:
        _li = _lock_info
        if _li.get("_invalid"):
            st.warning(
                "⚠️ **Lock file is invalid / corrupt** — safe to clear.  \n"
                f"reason: `{_li.get('reason', '?')}`"
            )
        else:
            _started = _li.get("started_at", "?")
            _age_str = "?"
            try:
                _age = datetime.now() - datetime.fromisoformat(_started)
                _age_str = f"{int(_age.total_seconds() / 60)} min ago"
            except Exception:
                pass
            st.warning(
                f"⚠️ **Stale lock detected** (older than {PIPELINE_LOCK_STALE_HOURS}h) — "
                "previous run likely crashed without releasing.  \n"
                f"started_at: `{_started}` ({_age_str}) · "
                f"host: `{_li.get('host', '?')}` · "
                f"pid: `{_li.get('pid', '?')}` · "
                f"user: `{_li.get('user', '?')}` · "
                f"start_step: `{_li.get('start_step', '?')}`"
            )
        if st.button("🗑️ Clear Stale Lock", key=f"clear_stale_lock_{selected_case}"):
            release_lock(selected_case)
            st.success("Stale lock cleared.")
            st.rerun()

    # ── Buttons (disabled while running OR while another session holds the lock) ──
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
    with col_btn1:
        if _lock_active:
            btn_label = "🔒  Locked by another session"
        elif st.session_state.pipeline_running:
            btn_label = "⏳  Pipeline Running…"
        else:
            btn_label = "► Run Full Pipeline"
        run_all = st.button(btn_label, type="primary",
                            disabled=st.session_state.pipeline_running or _lock_active,
                            use_container_width=True, help="Step 1 (LLM) + Step 2 + Step 3 + Step 4")
    with col_btn2:
        if _lock_active:
            btn_label2 = "🔒  Locked by another session"
        elif st.session_state.pipeline_running:
            btn_label2 = "⏳  Pipeline Running…"
        else:
            btn_label2 = "⚡ Enrich + Format + Export"
        run_partial = st.button(btn_label2,
                                disabled=st.session_state.pipeline_running or _lock_active,
                                use_container_width=True, help="Skip Step 1 (LLM) — use when only rules or .py files changed")
    with col_btn3:
        reset_partial = st.button(
            "🗑️ Reset Extract (Re-extract)",
            disabled=(not partial_exists) or st.session_state.pipeline_running or _lock_active,
            use_container_width=True,
            help="Delete partial.jsonl so the next Full Pipeline re-extracts from scratch",
        )

    if run_all and not st.session_state.pipeline_running:
        st.session_state.pipeline_running      = True
        st.session_state.pipeline_should_run   = True
        st.session_state.pipeline_start_step   = 0
        st.session_state.pipeline_step_results = []
        st.rerun()

    if run_partial and not st.session_state.pipeline_running:
        st.session_state.pipeline_running      = True
        st.session_state.pipeline_should_run   = True
        st.session_state.pipeline_start_step   = 1  # skip Step 1
        st.session_state.pipeline_step_results = []
        st.rerun()

    if reset_partial and partial_exists:
        partial_path.unlink()
        st.success("Partial cleared — next Full Pipeline will re-extract from scratch.")
        st.rerun()

    # ── Running banner (visible while executing) ──
    if st.session_state.pipeline_running:
        st.info("🔄  Pipeline is running… Do not close this tab.")

    # ── Execute pipeline — use st.status for real-time step visibility ──
    if st.session_state.pipeline_should_run:
        st.session_state.pipeline_should_run = False

        # Acquire case-level lock BEFORE running any subprocess.
        # If another session won the race between button click and execution,
        # bail out cleanly without touching their lock file.
        _acquired = acquire_lock(selected_case, st.session_state.pipeline_start_step)
        if _acquired is None:
            st.session_state.pipeline_running = False
            st.error(
                "🔒 Could not acquire pipeline lock — another session is "
                "currently running for this case. Please retry after it finishes."
            )
            st.rerun()
        else:
            all_ok = True
            start_step = st.session_state.pipeline_start_step

            try:
                with st.status("Running pipeline…", expanded=True) as status:
                    for idx, step in enumerate(PIPELINE_STEPS):
                        label = step["label"]

                        # Skip steps before start_step
                        if idx < start_step:
                            st.write(f"⏭  Skipped: {label}")
                            st.session_state.pipeline_step_results.append(
                                {"ok": True, "label": label, "ok_msg": "Skipped", "rc": 0, "output": "Skipped (Steps 2–4 mode)"}
                            )
                            continue

                        if step["requires_api_key"] and not has_api_key:
                            st.write(f"⏭  Skipped: {label} — OPENAI_API_KEY not set.")
                            st.session_state.pipeline_step_results.append(
                                {"ok": False, "label": label, "ok_msg": "", "rc": -1,
                                 "output": "OPENAI_API_KEY not set — step skipped."}
                            )
                            all_ok = False
                            break

                        st.write(f"▶  {label}…")
                        rc, output = run_step(step["cmd"])

                        if rc == 0:
                            st.write(f"✅  {label} — {step['ok_msg']}")
                        else:
                            st.write(f"❌  {label} failed (exit code {rc})")

                        st.session_state.pipeline_step_results.append(
                            {"ok": rc == 0, "label": label, "ok_msg": step["ok_msg"], "rc": rc, "output": output}
                        )

                        if rc != 0:
                            all_ok = False
                            break

                    if all_ok:
                        status.update(label="✅  Pipeline completed successfully", state="complete", expanded=False)
                    else:
                        status.update(label="❌  Pipeline failed — see log below", state="error", expanded=True)
            finally:
                # Always release the lock — success, failed step, or unhandled exception.
                release_lock(selected_case)

            st.session_state.pipeline_running = False
            if all_ok:
                st.session_state.pipeline_done = True
            st.rerun()  # re-render: show stored results + refresh outputs

    # ── Display stored step results ──
    for res in st.session_state.pipeline_step_results:
        if res["ok"]:
            st.success(f"✅  {res['label']}  |  {res['ok_msg']}")
        else:
            st.error(f"❌  {res['label']} failed (exit code {res['rc']})")
        if res["output"]:
            with st.expander(f"Log: {res['label']}", expanded=not res["ok"]):
                st.code(res["output"], language="text")

    if st.session_state.pipeline_step_results:
        last = st.session_state.pipeline_step_results[-1]
        if not last["ok"]:
            st.error("🛑  Pipeline stopped. Review the log above, fix the issue, and retry.")

    st.divider()

# ── Step 3: Download Results ──────────────────────────────────────────────────

st.subheader("Step 3: Download Results")

if st.session_state.pipeline_done:
    st.success("Pipeline completed successfully.")
    st.info("Output files are ready below.")
    st.session_state.pipeline_done = False

# Summary cards
counts = read_run_counts(selected_case)
if counts:
    with st.container(border=True):
        c1, c2, c3 = st.columns(3)
        c1.metric("主表 (Compliance Matrix)", counts.get("main",        "—"))
        c2.metric("需審核 (NEED_REVIEW)",      counts.get("need_review", "—"))
        c3.metric("詞彙表 (Glossary)",         counts.get("glossary",    "—"))
        st.markdown(
            "<p style='font-size:0.95rem;color:#546E7A;margin-top:4px;'>"
            "主表 = 交給客戶的 compliance_matrix 筆數</p>",
            unsafe_allow_html=True,
        )

run_dir = RUNS_DIR / selected_case
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

PRIMARY_FILES = [
    ("requirements_review.xlsx", "Review Excel",      "PM review sheet with must-level, owner, and redflags."),
    ("compliance_matrix.xlsx",   "Compliance Matrix", "Final deliverable for distribution to customer."),
]
ADVANCED_FILES = [
    ("requirements.json",          "Requirements (raw)"),
    ("requirements_enriched.json", "Requirements (enriched)"),
    ("requirements_clean.json",    "Requirements (clean)"),
    ("requirements.partial.jsonl", "Requirements (partial / resume)"),
    ("manifest.json",              "Manifest"),
]

if not run_dir.exists():
    st.info("No outputs yet. Complete Step 2 to generate files.")
else:
    # Primary downloads — prominent 2-column cards
    dcol1, dcol2 = st.columns(2)
    for col, (filename, label, desc) in zip([dcol1, dcol2], PRIMARY_FILES):
        fpath = run_dir / filename
        with col:
            with st.container(border=True):
                st.markdown(f"### {label}")
                st.markdown(
                    f"<p style='font-size:1.0rem;color:#546E7A;margin:2px 0 8px 0;'>{desc}</p>",
                    unsafe_allow_html=True,
                )
                if fpath.exists():
                    size_kb = fpath.stat().st_size / 1024
                    st.markdown(
                        f"<p style='font-size:0.95rem;color:#78909C;margin-bottom:8px;'>"
                        f"<code>{filename}</code> — {size_kb:.1f} KB</p>",
                        unsafe_allow_html=True,
                    )
                    st.download_button(
                        label=f"Download {label}",
                        data=fpath.read_bytes(),
                        file_name=filename,
                        mime=XLSX_MIME,
                        key=f"dl_{filename}",
                        use_container_width=True,
                        type="primary",
                    )
                else:
                    st.info("Not yet generated.")

    # Advanced outputs — collapsed by default
    adv_found = any((run_dir / fn).exists() for fn, _ in ADVANCED_FILES)
    if adv_found:
        with st.expander("Advanced Outputs", expanded=False):
            for filename, label in ADVANCED_FILES:
                fpath = run_dir / filename
                if fpath.exists():
                    size_kb = fpath.stat().st_size / 1024
                    st.success(f"**{label}** — `{filename}` ({size_kb:.1f} KB)")

st.divider()
st.subheader("Step 4: Review & Fill")

# 初始化 ResponsesManager
rm = ResponsesManager(RESPONSES_DIR, selected_case)

# 讀取 requirements_clean.json（req_id 已統一為 RFQ-XXX / AI-XXX 格式）
clean_path = RUNS_DIR / selected_case / "requirements_clean.json"
if not clean_path.exists():
    st.info("No requirements found. Run pipeline first.")
else:
    clean_data = json.loads(clean_path.read_text(encoding="utf-8"))

    # ── Pipeline summary ──
    _summary = clean_data.get("summary")
    if _summary and isinstance(_summary, dict):
        _s_req = _summary.get("total_requirements", 0)
        _s_glo = _summary.get("total_glossary", 0)
        _s_note = _summary.get("total_notes", 0)
        with st.container(border=True):
            _sc1, _sc2, _sc3 = st.columns(3)
            _sc1.metric("Requirements", _s_req)
            _sc2.metric("Glossary", _s_glo)
            _sc3.metric("Notes / Skipped", _s_note)
        _pm_note = _summary.get("pm_note", "")
        if _pm_note:
            st.warning(f"PM Note: {_pm_note}")

    all_items = clean_data.get("items", [])
    # 過濾掉 glossary / note（AUTO_SKIP）
    items = [i for i in all_items if i.get("status") != "AUTO_SKIP"]

    if not items:
        st.info(
            "This case has 0 actionable requirements. "
            "The uploaded files may be spec-reference, datasheet, or checklist documents "
            "without explicit shall/must requirements. "
            "You can still download the empty templates from Step 3 above."
        )
    responses = rm.load()

    # 進度統計
    status_counts = {"COMPLIANT": 0, "PARTIAL": 0, "NON-COMPLIANT": 0,
                     "NEW": 0, "PENDING": 0, "NEED_REVIEW": 0}
    for item in items:
        rid = item.get("req_id", "")
        status_key = responses.get(rid, {}).get("status") or item.get("status", "NEW")
        if status_key in status_counts:
            status_counts[status_key] += 1
        else:
            status_counts["NEW"] += 1

    s4c = st.columns(7)
    s4c[0].metric("COMPLIANT",     status_counts["COMPLIANT"])
    s4c[1].metric("PARTIAL",       status_counts["PARTIAL"])
    s4c[2].metric("NON-COMPLIANT", status_counts["NON-COMPLIANT"])
    s4c[3].metric("NEW",           status_counts["NEW"])
    s4c[4].metric("NEED_REVIEW",   status_counts["NEED_REVIEW"])
    s4c[5].metric("PENDING",       status_counts["PENDING"])
    s4c[6].metric("Total",         len(items))

    st.markdown("---")

    # 篩選器
    f1, f2, f3 = st.columns(3)
    all_cats   = sorted(set(str(i.get("category", "")) for i in items))
    all_owners = sorted(set(str(i.get("owner", "")) for i in items))
    status_opts = ["All", "NEED_REVIEW", "NEW", "PENDING", "COMPLIANT", "PARTIAL", "NON-COMPLIANT"]

    filter_status = f1.selectbox("Status",   status_opts,             key="rv_status")
    filter_cat    = f2.selectbox("Category", ["All"] + all_cats,      key="rv_cat")
    filter_owner  = f3.selectbox("Owner",    ["All"] + all_owners,    key="rv_owner")

    # 套用篩選
    filtered = []
    for item in items:
        rid = item.get("req_id", "")
        cur_status = responses.get(rid, {}).get("status") or item.get("status", "NEW")
        if filter_status != "All" and cur_status != filter_status:
            continue
        if filter_cat != "All" and str(item.get("category", "")) != filter_cat:
            continue
        if filter_owner != "All" and str(item.get("owner", "")) != filter_owner:
            continue
        filtered.append(item)

    st.markdown(f"**{len(filtered)} / {len(items)} requirements**")

    # 每筆顯示
    for idx, item in enumerate(filtered):
        rid = item.get("req_id", f"UNKNOWN-{idx}")  # 原始 key，用於 responses.json
        _key = f"{idx}_{rid}"
        cur = responses.get(rid, {})
        cur_status = cur.get("status") or item.get("status", "PENDING")
        cat = item.get("category", "")
        if isinstance(cat, list):
            cat = ", ".join(cat)
        _is_derived = item.get("derived", False)
        _derived_tag = " [DERIVED]" if _is_derived else ""
        label = f"{rid} | {cat} | {cur_status}{_derived_tag}"

        with st.expander(label):
            if _is_derived:
                st.caption("Derived from spec table — not an explicit customer requirement. Confirm against design.")
            st.markdown(f"**Requirement:** {item.get('requirement', '')}")

            meta_cols = st.columns(3)
            meta_cols[0].caption(f"Owner: {item.get('owner', '')}")
            sh = item.get("stakeholder") or []
            if isinstance(sh, str):
                sh = [s.strip() for s in sh.split(",") if s.strip()]
            meta_cols[1].caption(f"Also Involves: {', '.join(sh)}")
            meta_cols[2].caption(f"Category: {item.get('category', '')}")

            rf = item.get("risk_tags") or item.get("redflag_tags") or []
            if isinstance(rf, str):
                rf = [t.strip() for t in rf.split(",") if t.strip()]
            if rf:
                st.error(f"🚩 {', '.join(rf)}")
            _risk_note = item.get("risk_note", "")
            if _risk_note:
                st.caption(f"Risk: {_risk_note}")

            # Phase 4: show source so PM can trace ORPHAN_SUBITEM / NEED_REVIEW back to original
            _src = item.get("source", "")
            if _src:
                st.caption(f"📍 Source: {_src}")

            ai_draft = cur.get("ai_draft", "")
            if ai_draft:
                st.caption(f"AI Draft: {ai_draft}")

            _status_options = ["PENDING", "NEED_REVIEW", "COMPLIANT", "PARTIAL", "NON-COMPLIANT"]
            _status_idx = _status_options.index(cur_status) if cur_status in _status_options else 0
            new_status   = st.selectbox("Status", _status_options, index=_status_idx, key=f"st_{_key}")
            new_comment  = st.text_area("Our Response",   value=cur.get("vendor_comment", ""), key=f"vc_{_key}")
            new_evidence = st.text_input("Evidence Link", value=cur.get("evidence", ""),        key=f"ev_{_key}")
            new_gap      = st.text_area("Gap / Notes",    value=cur.get("gap", ""),             key=f"gp_{_key}")

            if st.button("💾 Save", key=f"sv_{_key}"):
                rm.update(rid,
                    status=new_status,
                    vendor_comment=new_comment,
                    evidence=new_evidence,
                    gap=new_gap,
                )
                st.success("Saved.")
