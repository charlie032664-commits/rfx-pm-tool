import getpass
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
import pandas as pd
import streamlit as st
import yaml
from datetime import datetime, timedelta
from pathlib import Path
from scripts.responses_manager import ResponsesManager
from scripts.env_loader import load_env, describe_llm_config
from scripts.job_status import start_job, set_stage, finish_job, read_job_status

# Load a repo-root .env (if present) into os.environ BEFORE any LLM-config
# checks or subprocess launches. OS env always wins, so launchers / setx keep
# precedence; a missing .env is a no-op. This is what lets the pipeline
# subprocesses (which inherit os.environ) see local LLM settings.
load_env()

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
# Phase 4.6C — Normalize Requirements UI state
if "normalize_running"      not in st.session_state: st.session_state.normalize_running      = False
if "normalize_should_run"   not in st.session_state: st.session_state.normalize_should_run   = False
if "normalize_step_results" not in st.session_state: st.session_state.normalize_step_results = []
if "normalize_sample_mode"  not in st.session_state: st.session_state.normalize_sample_mode  = "sample10"
if "normalize_force"        not in st.session_state: st.session_state.normalize_force        = False


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


def load_cache_summary(case_id: str) -> dict | None:
    """Load the incremental-cache observability summary for a case, if present.

    Produced by scripts/cache_metadata.py as runs/<case>/extraction_cache.json,
    where the metrics live under the top-level "summary" key. Returns a flat
    dict of the five summary metrics plus generated_at / model_name, or None
    when the cache step has not been run for this case yet.

    Backward-compatible by design: a missing or unreadable file simply returns
    None, so the page renders exactly as it did before this feature existed.
    A cache_summary.json fallback is accepted in case a later step writes the
    summary object on its own.
    """
    run_dir = RUNS_DIR / case_id
    for fname in ("extraction_cache.json", "cache_summary.json"):
        p = run_dir / fname
        if not p.exists():
            continue
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        summary = doc.get("summary")
        if not isinstance(summary, dict):
            # cache_summary.json may itself be the bare summary object.
            summary = doc if "total_requirements" in doc else None
        if not isinstance(summary, dict):
            continue
        return {
            "total_requirements":          summary.get("total_requirements", 0),
            "reused_from_cache":           summary.get("reused_from_cache", 0),
            "reprocessed":                 summary.get("reprocessed", 0),
            "skipped_unchanged":           summary.get("skipped_unchanged", 0),
            "estimated_runtime_saved_sec": summary.get("estimated_runtime_saved_sec", 0),
            "generated_at":                doc.get("generated_at", ""),
            "model_name":                  doc.get("model_name", ""),
        }
    return None


def _fmt_runtime_saved(sec) -> str:
    """Human-readable duration for the runtime-saved card.

    Mirrors extraction_cache._fmt_seconds; kept local so app.py needs no new
    import. Non-numeric input renders as an em dash rather than raising.
    """
    try:
        s = int(round(float(sec)))
    except (TypeError, ValueError):
        return "—"
    if s < 60:
        return f"{s}s"
    m, r = divmod(s, 60)
    return f"{m}m {r}s"


def _launch_pipeline_worker(case_id: str, start_step: int = 0):
    """v1.2: start scripts/pipeline_worker.py DETACHED so a Streamlit rerun can't
    interrupt it. The worker writes runs/<case>/job_status.json + pipeline_worker.log
    itself; we only need its pid. Returns the pid, or None on failure."""
    case_inbound = INBOUND_DIR / case_id
    responses    = RESPONSES_DIR / case_id / "responses.json"
    cmd = [PYTHON, str(AI_RFX_DIR / "pipeline_worker.py"),
           "--case", str(case_inbound), "--runs", str(RUNS_DIR),
           "--rules", str(BASE_DIR / "rules"), "--start-step", str(start_step)]
    if responses.exists():
        cmd += ["--responses", str(responses)]
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            cmd, creationflags=flags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, cwd=str(BASE_DIR),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        return proc.pid
    except Exception:
        return None


def _job_active(case_id: str) -> bool:
    """True only if job_status says running AND that pid is still alive."""
    j = read_job_status(RUNS_DIR / case_id)
    if not j or j.get("status") != "running":
        return False
    pid = j.get("pid")
    try:
        return _pid_alive(int(pid)) if pid is not None else False
    except Exception:
        return False


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


def run_step_streaming(cmd: list, on_line,
                       case_id: str | None = None) -> tuple[int, str]:
    """Run a subprocess and stream stdout to on_line() line by line.

    Returns (returncode, combined_output_stripped). stderr is merged into
    stdout so [WARN] retry lines arrive in the same event stream as
    [PROGRESS] lines. PYTHONUNBUFFERED=1 is injected into the child env so
    Python flushes prints immediately; without it, prints buffer until the
    child exits and live progress is impossible.

    on_line is called with each non-empty stripped line. Exceptions raised
    by on_line are swallowed so a UI handler bug cannot crash the loop.

    Phase 4.6I: when case_id is provided, the child's PID is published to
    runs/<case_id>/.pipeline.subproc_pid for the lifetime of the child so a
    Cancel handler in another session can find and kill it.
    """
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if case_id:
        _write_subproc_pid(case_id, proc.pid)
    buf: list[str] = []
    try:
        for line in iter(proc.stdout.readline, ""):
            buf.append(line)
            s = line.rstrip("\r\n")
            if not s:
                continue
            try:
                on_line(s)
            except Exception:
                pass
    finally:
        proc.stdout.close()
        proc.wait()
        if case_id:
            _clear_subproc_pid(case_id)
    return proc.returncode, "".join(buf).strip()


# Phase 4.6F.1 — Progress event parser & widget state for streaming UI
# See docs/schema.md "Phase 4.6F — Progress log contract" for the line
# formats this parser recognizes.

_RE_CHUNKS_TOTAL = re.compile(r'^\[INFO\]\s+(\S+):\s+(\d+)\s+chunks\s*$')
_RE_CHUNK        = re.compile(r'^\[(?:PROGRESS|SKIP)\]\s+(\S+)\s+chunk\s+(\d+)/(\d+)\b')
_RE_ENRICH_ITEM  = re.compile(
    r'^\[PROGRESS\]\s+enrich\s+item\s+(\d+)/(\d+)(?:\s+req_id=(\S+))?\s*$'
)
_RE_ITEM         = re.compile(r'^\s*\[(\d+)/(\d+)\]\s+(\S+)')
_RE_RETRY        = re.compile(
    r'^\[WARN\]\s+LLM\s+(?:call failed\s+\(attempt\s+|enrich\s+attempt\s+)'
    r'(\d+)/(\d+).*?->\s*sleep\s+(\d+(?:\.\d+)?)\s*s'
)


def _parse_progress_line(line: str) -> dict | None:
    """Parse one stdout line into a progress event dict, or None if it carries
    no progress info. Lines that don't match any pattern are still appended
    to the per-step log buffer by run_step_streaming()."""
    m = _RE_CHUNK.match(line)
    if m:
        return {"kind": "chunk", "file": m.group(1),
                "done": int(m.group(2)), "total": int(m.group(3))}
    m = _RE_ENRICH_ITEM.match(line)
    if m:
        return {"kind": "item", "done": int(m.group(1)),
                "total": int(m.group(2)),
                "req_id": m.group(3) or "?"}
    m = _RE_CHUNKS_TOTAL.match(line)
    if m:
        return {"kind": "chunks_total", "file": m.group(1),
                "total": int(m.group(2))}
    m = _RE_RETRY.match(line)
    if m:
        return {"kind": "retry", "attempt": int(m.group(1)),
                "max": int(m.group(2)), "sleep_s": float(m.group(3))}
    m = _RE_ITEM.match(line)
    if m:
        return {"kind": "item", "done": int(m.group(1)),
                "total": int(m.group(2)), "req_id": m.group(3)}
    return None


def _make_progress_slots() -> dict:
    """Build the per-step widget tree inside an st.status() block. Each slot
    is an st.empty() placeholder; slots that never receive an event stay
    invisible so e.g. Format/Export steps render only an elapsed line."""
    return {
        "file":    st.empty(),
        "chunk":   st.empty(),
        "item":    st.empty(),
        "elapsed": st.empty(),
        "retry":   st.empty(),
        "_state": {
            "file_current": "",
            "chunk_done":   0,
            "chunk_total":  0,
            "item_done":    0,
            "item_total":   0,
            "started_at":   0.0,
        },
    }


def _make_on_line(slots: dict, started_at: float):
    """Build an on_line callback bound to a step's slot tree + start time."""
    state = slots["_state"]
    state["started_at"] = started_at

    def _fmt_eta(done: int, total: int, elapsed: float) -> str:
        # Require done >= 2 to dampen the wildly misleading first-sample ETA.
        if done < 2 or total <= 0 or done >= total:
            return ""
        remaining = elapsed * (total - done) / done
        return f"  ·  ETA: ~{int(remaining)}s (rough)"

    def _render_elapsed():
        elapsed = time.monotonic() - state["started_at"]
        if state["chunk_total"] > 0:
            eta = _fmt_eta(state["chunk_done"], state["chunk_total"], elapsed)
        elif state["item_total"] > 0:
            eta = _fmt_eta(state["item_done"], state["item_total"], elapsed)
        else:
            eta = ""
        slots["elapsed"].markdown(f"⏱  elapsed: {int(elapsed)}s{eta}")

    def on_line(line: str):
        ev = _parse_progress_line(line)
        if ev is None:
            # Refresh elapsed even for non-progress lines so the timer keeps
            # ticking during pre-loop work (schema analyze, file parse, etc.)
            # — otherwise the UI looks frozen while stdout is actually flowing.
            _render_elapsed()
            return
        k = ev["kind"]
        if k == "chunks_total":
            state["chunk_total"] = ev["total"]
            state["file_current"] = ev["file"]
            slots["file"].markdown(f"📄 file: `{ev['file']}`")
            slots["chunk"].markdown(f"📦 chunk: 0 / {ev['total']}")
        elif k == "chunk":
            state["chunk_done"]  = ev["done"]
            state["chunk_total"] = ev["total"]
            if ev["file"] != state["file_current"]:
                state["file_current"] = ev["file"]
                slots["file"].markdown(f"📄 file: `{ev['file']}`")
            slots["chunk"].markdown(f"📦 chunk: {ev['done']} / {ev['total']}")
        elif k == "item":
            state["item_done"]  = ev["done"]
            state["item_total"] = ev["total"]
            slots["item"].markdown(
                f"▫ item: {ev['done']} / {ev['total']}  ·  `{ev['req_id']}`"
            )
        elif k == "retry":
            slots["retry"].warning(
                f"⚠ Last retry: attempt {ev['attempt']}/{ev['max']}"
                f", sleep {ev['sleep_s']:g}s"
            )
        _render_elapsed()

    return on_line


def _parse_normalize_summary(output: str) -> dict:
    """Parse the '=== Summary ===' block emitted by normalize_requirements_llm.py.

    Returns a dict of integer counters keyed by the script's stat names
    (e.g. processed, skipped_idempotent, already_complete, ...).
    Returns {} when parsing fails — caller should fall back to clean.json stats.
    """
    if not output:
        return {}
    try:
        m = re.search(r"===\s*Summary\s*===(.*?)(?:\n\n===|\Z)", output, re.DOTALL)
        if not m:
            return {}
        out: dict = {}
        for line in m.group(1).splitlines():
            mm = re.match(r"\s+(\w[\w_]*)\s*:\s*(\d+)\s*$", line)
            if mm:
                out[mm.group(1)] = int(mm.group(2))
        return out
    except Exception:
        return {}


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
#
# Phase 4.6D — lock files now carry an `operation` field so the UI can
# tell PM *what kind* of run is holding the lock (pipeline vs normalize).
# Old lock files without this field still parse fine; they render as
# "another session" in the UI.

PIPELINE_LOCK_STALE_HOURS = 2

# Phase 4.6D — allowed values for the lock file's `operation` field.
# Any other value gets normalized to "unknown" by acquire_lock().
ALLOWED_LOCK_OPERATIONS = {"pipeline", "normalize", "unknown"}


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


def acquire_lock(case_id: str, start_step: int,
                 operation: str = "unknown") -> dict | None:
    """Atomically create the .pipeline.lock for case_id.

    If an existing lock is stale/invalid, it is replaced. If an existing
    lock is active, returns None (caller must abort). On success returns
    the freshly-written lock dict.

    Phase 4.6D — `operation` records what kind of run is holding the lock:
      - "pipeline"  → Full Pipeline / Enrich+Format+Export
      - "normalize" → Step 3.5 Normalize
      - "unknown"   → anything else / unset / unrecognized value
    Unrecognized values are normalized to "unknown" so a typo in a future
    caller never breaks the schema.
    """
    # Phase 4.6D — defensively normalize operation
    op = (operation or "").strip().lower()
    if op not in ALLOWED_LOCK_OPERATIONS:
        op = "unknown"

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
        "operation":  op,                       # ← Phase 4.6D
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


# Phase 4.6I — Subprocess PID sidecar for cancel-from-another-session.
#
# acquire_lock records the Streamlit master's PID, which is the right answer
# for "who acquired the lock" but the wrong answer for "what to kill on cancel"
# — the actual extract/enrich/normalize work happens in a child Popen.
# We track the active child's PID in a sidecar file so a Cancel handler in
# any session can find and kill the right process. The sidecar is short-lived:
# created on Popen, removed in run_step_streaming's finally.

def _subproc_pid_path(case_id: str) -> Path:
    return RUNS_DIR / case_id / ".pipeline.subproc_pid"


def _write_subproc_pid(case_id: str, pid: int) -> None:
    p = _subproc_pid_path(case_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass  # cancel degrades to a no-op if we can't publish the pid


def _read_subproc_pid(case_id: str) -> int | None:
    p = _subproc_pid_path(case_id)
    if not p.exists():
        return None
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _clear_subproc_pid(case_id: str) -> None:
    p = _subproc_pid_path(case_id)
    try:
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _kill_process_tree(pid: int | None) -> bool:
    """Kill pid + descendants. Returns True if the process was alive when
    we tried to kill it; False if it was already gone, invalid, or our own
    pid (we refuse to suicide)."""
    if pid is None:
        return False
    try:
        pid_int = int(pid)
    except (TypeError, ValueError):
        return False
    if pid_int <= 0 or pid_int == os.getpid():
        return False
    if not _pid_alive(pid_int):
        return False
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid_int)],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
    else:
        try:
            import signal as _sig
            os.killpg(os.getpgid(pid_int), _sig.SIGTERM)
        except Exception:
            try:
                os.kill(pid_int, 9)
            except Exception:
                pass
    return True


def cancel_pipeline(case_id: str) -> dict:
    """Cancel the case's active pipeline child and clean up lock + sidecar.

    Safety: if the lock was acquired by a different host or user, refuses to
    kill anything (the lock belongs to someone else). The lock itself is
    left intact in that case so the rightful owner can still see / clear it.

    Phase 4.6J: writes a "cancelled" record to run_history.jsonl when the
    cleanup actually runs (i.e. not when we refuse on host/user mismatch).
    The record reuses the lock's started_at when available so duration is
    meaningful.

    Returns {"killed": bool, "reason": str}.
    """
    info = read_lock_info(case_id) or {}
    if info.get("host") and info.get("host") != socket.gethostname():
        return {"killed": False,
                "reason": f"refuse: lock owned by host {info.get('host')!r}"}
    if info.get("user") and info.get("user") != getpass.getuser():
        return {"killed": False,
                "reason": f"refuse: lock owned by user {info.get('user')!r}"}

    pid = _read_subproc_pid(case_id)
    killed = _kill_process_tree(pid) if pid else False
    _clear_subproc_pid(case_id)
    release_lock(case_id)

    if killed:
        reason = f"killed PID {pid} and descendants"
    elif pid is None:
        reason = "no active subprocess sidecar; lock cleared"
    else:
        reason = f"PID {pid} already gone; lock cleared"

    # Phase 4.6J — record the cancel event.
    _record_cancel_history(case_id, info, pid, killed, reason)

    return {"killed": killed, "reason": reason}


# Phase 4.6J — Persisted run history (append-only JSONL per case).
#
# Lifecycle: every Step 2 pipeline run and Step 3.5 normalize run writes a
# "started" record at launch and a "success" / "failed" record at finish.
# cancel_pipeline writes a "cancelled" record. Writes are best-effort; a
# failure to append must NEVER break the pipeline. UTF-8 without BOM is
# enforced because PowerShell-written BOM files have bitten us before
# (the json.loads call in read_lock_info rejects the U+FEFF prefix).

def _run_history_path(case_id: str) -> Path:
    return RUNS_DIR / case_id / "run_history.jsonl"


def _new_run_id() -> str:
    """Short stable id for grouping started/terminal records of one run."""
    return uuid.uuid4().hex[:12]


def append_run_history(case_id: str, record: dict) -> None:
    """Best-effort append one record to runs/<case_id>/run_history.jsonl.
    Never raises — a history write failure must not break the pipeline."""
    try:
        p = _run_history_path(case_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Explicit UTF-8 no BOM. open(..., "a", encoding="utf-8") on CPython
        # does NOT prepend a BOM; we rely on that contract.
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_run_history(case_id: str, limit: int = 5) -> list[dict]:
    """Return up to `limit` most recent records, newest first. Returns []
    on missing file, decode errors, or any other failure — bad lines are
    silently skipped so a partially-corrupt file still yields good records."""
    p = _run_history_path(case_id)
    if not p.exists():
        return []
    out: list[dict] = []
    try:
        lines = [
            l for l in p.read_text(encoding="utf-8", errors="ignore").splitlines()
            if l.strip()
        ]
        for line in reversed(lines):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                out.append(rec)
                if len(out) >= limit:
                    break
    except Exception:
        return out
    return out


def _record_cancel_history(case_id: str, lock_info: dict, killed_pid: int | None,
                           killed: bool, reason: str) -> None:
    """Helper used by cancel_pipeline. Best-effort — must not raise."""
    try:
        started_at = (lock_info or {}).get("started_at") or ""
        ended_at = datetime.now().isoformat(timespec="seconds")
        duration_sec = None
        try:
            duration_sec = int(
                (datetime.now() - datetime.fromisoformat(started_at)).total_seconds()
            )
        except Exception:
            pass
        append_run_history(case_id, {
            "run_id":       _new_run_id(),
            "case_id":      case_id,
            "operation":    (lock_info or {}).get("operation") or "unknown",
            "status":       "cancelled",
            "started_at":   started_at or ended_at,
            "ended_at":     ended_at,
            "duration_sec": duration_sec,
            "user":         getpass.getuser(),
            "host":         socket.gethostname(),
            "pid":          os.getpid(),
            "subproc_pid":  killed_pid,
            "start_step":   (lock_info or {}).get("start_step"),
            "steps":        None,
            "return_code":  None,
            "message":      reason,
        })
    except Exception:
        pass


# Phase 4.6H — Runtime guard helper: estimate the doc's chunk count from
# the partial.jsonl we wrote on the previous extract. Uses max(chunk) per
# file (not line count) because resumed/repeated runs may emit duplicate
# records and the script's chunk numbering is 1-indexed and monotonic.

def _estimate_chunks_from_partial(case_id: str) -> int:
    """Sum of max chunk index per file in requirements.partial.jsonl.
    Returns 0 on missing file, parse error, or any other failure — the
    UI just doesn't show the size warning in that case."""
    p = RUNS_DIR / case_id / "requirements.partial.jsonl"
    if not p.exists():
        return 0
    max_per_file: dict[str, int] = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            f = rec.get("file")
            c = rec.get("chunk")
            if isinstance(f, str) and isinstance(c, int) and c > 0:
                if c > max_per_file.get(f, 0):
                    max_per_file[f] = c
    except Exception:
        return 0
    return sum(max_per_file.values())


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


# ── LLM provider status (v1.2): presence-only, never prints secret values ─────
with st.sidebar.expander("LLM provider", expanded=False):
    try:
        _llm_cfg  = describe_llm_config()
        _prov     = str(_llm_cfg.get("provider", "?"))
        _ready    = bool(_llm_cfg.get("ready"))
        _present  = _llm_cfg.get("present", {}) or {}
        if _prov == "internal":
            _model = os.environ.get("INTERNAL_LLM_MODEL", "") or "(unset)"
        else:
            _model = os.environ.get("OPENAI_MODEL", "") or "gpt-4.1-mini"
        st.markdown(f"**Provider:** `{_prov}`")
        st.markdown(f"**Model:** `{_model}`")
        for _k, _ok in _present.items():
            st.markdown(f"- `{_k}`: {'✅ present' if _ok else '❌ missing'}")
        if _ready:
            st.success("ready")
        else:
            st.warning("not ready — see docs/env_config.md")
        st.caption("Presence only — no secret values are shown.")
    except Exception:
        st.caption("LLM status unavailable.")


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

st.subheader("Step 1: Case & Source Files")
st.caption("Select or create a case, upload RFQ files, and confirm which files should be "
           "treated as RFQ / source inputs for analysis.")
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
        st.markdown("**File selection**")

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
                    help="Mark whether this file should feed the pipeline. "
                         "Files with Include=False are skipped by extraction "
                         "and excluded from manifest.json (Phase 7).",
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
    st.subheader("Step 2: Pre-check")
    st.caption("Before analyzing: confirm the case is ready (LLM provider available), review the "
               "estimated runtime, and check for existing results or a running / locked job.")

    # Phase 4.6H — Runtime guard: prominent case display. The sidebar
    # selectbox is easy to overlook after a long browser session; this
    # makes the active case unambiguous next to the Pipeline buttons.
    st.markdown(
        f"<p style='font-size:1.05rem;color:#0D47A1;margin:0 0 8px 0;'>"
        f"<b>Current case:</b> <code>{selected_case}</code></p>",
        unsafe_allow_html=True,
    )

    case_inbound = INBOUND_DIR / selected_case
    case_runs    = RUNS_DIR / selected_case

    _llm_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
    has_api_key = bool(os.environ.get("OPENAI_API_KEY")) or _llm_provider == "internal"

    use_llm_enrich = st.checkbox(
        "Use LLM for enrichment (category / owner / stakeholder / redflag)",
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
            f"use <b>Continue Previous Run</b> to continue, or Analyze RFQ to re-extract</p>",
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

    # ── Step 1.5 enforcement notice: list files the extractor will skip ──
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
            f"ℹ️ **{len(_excluded_files)} file(s) will be skipped by extraction**: {_shown}  \n"
            "To process them, re-enable in **Step 1: Case & Files** above and click **Save Selection**."
        )

    # ── Pipeline lock state (computed BEFORE buttons so we can gate them) ──
    _lock_info   = read_lock_info(selected_case)
    _lock_stale  = is_lock_stale(_lock_info) if _lock_info else False
    _lock_active = bool(_lock_info) and not _lock_stale

    # Phase 4.6I — surface the result of a recent Cancel across the rerun
    _cancel_msg = st.session_state.pop("_cancel_msg", None)
    if _cancel_msg:
        st.warning(
            f"⏹ Pipeline cancelled by user. {_cancel_msg}. "
            "Partial progress is preserved."
        )

    if _lock_active:
        _li = _lock_info or {}
        _pid_val = _li.get("pid")
        _pid_dead_hint = ""
        if _li.get("host") == socket.gethostname() and _pid_val and not _pid_alive(_pid_val):
            _pid_dead_hint = "  ⚠ PID no longer running on this host"
        # Phase 4.6D — operation-aware headline so PM sees WHY the case is locked
        _op = (_li.get("operation") or "").strip().lower()
        if _op == "pipeline":
            _hdr = "🔒 **This case is currently locked by a pipeline run.**"
        elif _op == "normalize":
            _hdr = "🔒 **This case is currently locked by a normalize run.**"
        else:
            _hdr = "🔒 **This case is currently locked by another session.**"
        st.warning(
            f"{_hdr}  \n"
            f"started_at: `{_li.get('started_at', '?')}` · "
            f"host: `{_li.get('host', '?')}` · "
            f"pid: `{_pid_val}` · "
            f"user: `{_li.get('user', '?')}`  \n"
            f"operation: `{_li.get('operation', 'unknown')}` · "
            f"start_step: `{_li.get('start_step', '?')}`"
            f"{_pid_dead_hint}"
        )

        # Phase 4.6I — Cancel button (same-host/same-user only)
        # Refresh this tab (or open another) while a long run is in progress
        # to see this button; clicking it kills the running subprocess tree
        # and clears the lock. partial.jsonl and prior outputs are preserved.
        _can_cancel = (_li.get("host") == socket.gethostname()
                       and _li.get("user") == getpass.getuser())
        if _can_cancel and st.button(
            "🛑 Cancel pipeline",
            key=f"cancel_pipeline_{selected_case}",
            help="Kill the running subprocess and clear the lock. "
                 "Partial progress (requirements.partial.jsonl, "
                 "extract_errors.jsonl, etc.) is preserved.",
        ):
            _result = cancel_pipeline(selected_case)
            st.session_state["_cancel_msg"] = _result["reason"]
            st.rerun()
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
                f"operation: `{_li.get('operation', 'unknown')}` · "
                f"start_step: `{_li.get('start_step', '?')}`"
            )
        if st.button("🗑️ Clear Stale Lock", key=f"clear_stale_lock_{selected_case}"):
            release_lock(selected_case)
            st.success("Stale lock cleared.")
            st.rerun()

    # ── Phase 4.6H — Runtime guard: pre-button context + size warnings ──
    _est_chunks = _estimate_chunks_from_partial(selected_case)

    if req_json_exists:
        st.info(
            "ℹ️ `requirements.json` already exists. **Recommended:** use "
            "**Continue Previous Run** unless you intentionally want "
            "to re-run extraction."
        )

    if _est_chunks > 1000:
        st.warning(
            f"⚠ **Very large document** — previous extract recorded "
            f"~{_est_chunks} chunks. A full re-extract may take a long time "
            f"and consume significant LLM budget."
        )
    elif _est_chunks > 300:
        st.warning(
            f"⚠ **Large extraction history** — previous extract recorded "
            f"~{_est_chunks} chunks. Re-extraction will take a while."
        )

    st.markdown(
        "<p style='font-size:0.95rem;color:#BF360C;margin:8px 0 4px 0;'>"
        "ℹ️ Full Pipeline will re-run Extract and may take a long time.</p>",
        unsafe_allow_html=True,
    )

    # ── v1.3: runtime estimate / large-run guard (on-demand; reads files, no LLM) ──
    with st.expander("Runtime estimate / large-run guard", expanded=True):
        st.caption("Estimates extract runtime from file size + chunking for the "
                   "current provider. No LLM call. Enrich adds ~1 call per requirement.")
        if st.button("Estimate runtime", key=f"estimate_rt_{selected_case}"):
            try:
                _eprov = str(describe_llm_config().get("provider", "") or "")
                _emodel = (os.environ.get("INTERNAL_LLM_MODEL", "") if _eprov == "internal"
                           else (os.environ.get("OPENAI_MODEL", "") or "gpt-4.1-mini"))
                import scripts.runtime_estimator as _rte
                st.session_state[f"_rt_est_{selected_case}"] = _rte.estimate_case(
                    INBOUND_DIR / selected_case, _eprov, _emodel)
            except Exception as _e:
                st.session_state[f"_rt_est_{selected_case}"] = {"error": str(_e)}
        _est = st.session_state.get(f"_rt_est_{selected_case}")
        if _est:
            if _est.get("error"):
                st.warning(f"Estimate unavailable: {_est['error']}")
            else:
                _c = st.columns(4)
                _c[0].metric("Files",        _est.get("file_count", 0))
                _c[1].metric("Est. chunks",  _est.get("chunks", 0))
                _c[2].metric("Est. runtime", _est.get("est_runtime_human", "?"))
                _c[3].metric("Risk",         str(_est.get("risk", "?")).upper())
                st.caption(
                    f"provider `{_est.get('provider','?')}` · model "
                    f"`{_est.get('model') or '(default)'}` · "
                    f"{_est.get('seconds_per_call','?')}s/call · {_est.get('note','')}"
                )
                if _est.get("risk") == "high":
                    st.error("⛔ STRONG WARNING: estimated > 4 hours. For large internal "
                             "runs, prefer OpenAI or run overnight with explicit intent.")
                elif _est.get("risk") == "warning":
                    st.warning("⚠ Estimated > 1 hour — this will take a while.")

    st.subheader("Step 3: Analyze RFQ")
    st.caption(
        "Run AI extraction/enrichment and monitor progress. The Fast UI Test / "
        "No-AI option below runs a heuristic mock for workflow testing only."
    )

    # ── v1.4 Phase B: Fast UI Test / No-AI — workflow testing only. Runs the
    # existing heuristic mock CLI (scripts/mock_extract_requirements.py). It does
    # NOT call the LLM, NOT run the formal pipeline, and writes only a separate
    # requirements_mock.json (never requirements.json / _clean.json / the matrix).
    with st.expander("Fast UI Test / No-AI", expanded=False):
        st.caption(
            "For UI flow testing only. This does NOT call the LLM and does NOT run "
            "the formal pipeline. Output is a heuristic mock — NOT final RFQ "
            "extraction quality."
        )
        if st.button("Run No-AI Flow Test", key=f"noai_flow_{selected_case}",
                     help="Runs scripts/mock_extract_requirements.py — heuristic, no LLM, no pipeline."):
            _mock_script = AI_RFX_DIR / "mock_extract_requirements.py"
            _mock_case = INBOUND_DIR / selected_case
            _mock_out = RUNS_DIR / selected_case / "requirements_mock.json"
            if not _mock_script.exists():
                st.error(f"Not found: {_mock_script}")
            elif not _mock_case.exists():
                st.warning("No inbound folder for this case.")
            else:
                try:
                    _mp = subprocess.run(
                        [PYTHON, str(_mock_script),
                         "--case-dir", str(_mock_case),
                         "--out", str(_mock_out),
                         "--limit", "50"],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=120,
                    )
                    if _mp.returncode == 0:
                        _cnt, _mode = "?", "?"
                        try:
                            _md = json.loads(_mock_out.read_text(encoding="utf-8"))
                            _cnt = len(_md.get("requirements", []) or [])
                            _mode = (_md.get("meta", {}) or {}).get("analysis_mode", "?")
                        except Exception:
                            pass
                        st.success(f"No-AI Flow Test OK — {_cnt} mock requirement(s) "
                                   f"(analysis_mode={_mode}).")
                        st.markdown(f"Output: `{_mock_out}`")
                        st.warning(
                            "⚠ Workflow-test mock only — NOT final RFQ extraction "
                            "quality. No LLM was called, the formal pipeline did not "
                            "run, and requirements.json / requirements_clean.json / "
                            "the compliance matrix were not touched."
                        )
                    else:
                        st.error(f"No-AI Flow Test failed (exit {_mp.returncode}).")
                    if _mp.stdout:
                        st.code(_mp.stdout, language="text")
                except Exception as _e:
                    st.error(f"Failed to run mock script: {_e}")

    # Confirmation checkbox gates Run Full Pipeline whenever prior state
    # suggests re-extraction is expensive or unnecessary.
    _need_confirm = req_json_exists or _est_chunks > 300
    if _need_confirm:
        _confirm_full = st.checkbox(
            "I understand Full Pipeline will re-run extraction and may "
            "take a long time.",
            key=f"confirm_full_pipeline_{selected_case}",
            value=False,
        )
    else:
        _confirm_full = True

    # ── Buttons (disabled while running OR while another session holds the lock) ──
    col_btn1, col_btn2, col_btn3 = st.columns([1, 1, 1])
    with col_btn1:
        if _lock_active:
            btn_label = "🔒  Locked by another session"
        elif st.session_state.pipeline_running:
            btn_label = "⏳  Pipeline Running…"
        elif not _confirm_full:
            btn_label = "Analyze RFQ (confirm first)"
        else:
            btn_label = "Analyze RFQ"
        run_all = st.button(btn_label, type="primary",
                            disabled=(st.session_state.pipeline_running
                                      or _lock_active
                                      or not _confirm_full),
                            use_container_width=True, help="Analyze the RFQ end-to-end: extract, enrich, format, export")
    with col_btn2:
        if _lock_active:
            btn_label2 = "🔒  Locked by another session"
        elif st.session_state.pipeline_running:
            btn_label2 = "⏳  Pipeline Running…"
        elif req_json_exists:
            btn_label2 = "Continue from existing extraction (Advanced)"
        else:
            btn_label2 = "Continue from existing extraction (Advanced)"
        run_partial = st.button(btn_label2,
                                disabled=st.session_state.pipeline_running or _lock_active,
                                use_container_width=True, help="Skip Step 1 (LLM) — use when only rules or .py files changed")
    with col_btn3:
        reset_partial = st.button(
            "Force re-extract requirements (Advanced)",
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

    # ── v1.2: detached background launch (beta) — additive. The synchronous
    #    "Run Full Pipeline" above is unchanged and remains the default; this
    #    starts a detached worker that survives page refresh / rerun. ──
    st.markdown(
        "<p style='font-size:0.9rem;color:#546E7A;margin:6px 0 2px 0;'>"
        "Beta: run as a detached background job that survives page refresh.</p>",
        unsafe_allow_html=True,
    )
    if st.session_state.get("_bg_started_msg"):
        st.success(st.session_state.pop("_bg_started_msg"))
    _bg_active = _job_active(selected_case)
    if st.button(
        "Run as background job (Advanced)",
        key=f"run_bg_{selected_case}",
        disabled=(_lock_active or _bg_active),
        help="Starts scripts/pipeline_worker.py detached. Writes "
             "runs/<case>/job_status.json + pipeline_worker.log; survives reruns.",
    ):
        _pid = _launch_pipeline_worker(selected_case, 0)
        if _pid:
            st.session_state["_bg_started_msg"] = (
                f"Pipeline started in background (pid {_pid}). "
                "You may refresh this page; job status updates below."
            )
        else:
            st.session_state["_bg_started_msg"] = "⚠ Failed to start background worker."
        st.rerun()
    if _bg_active:
        st.info("A background pipeline job appears to be running for this case "
                "(see job status below).")
    else:
        # v1.2 Phase 4: stale job detection — status=running but the pid is dead.
        # Read-only guidance; never auto-deletes locks or outputs.
        _js = read_job_status(RUNS_DIR / selected_case)
        if _js and _js.get("status") == "running":
            _lock_note = (" A `.pipeline.lock` is also present — use the lock "
                          "controls above to clear it if needed."
                          if (RUNS_DIR / selected_case / ".pipeline.lock").exists() else "")
            st.warning(
                f"⚠ A job is marked **running** (stage `{_js.get('stage','?')}`, "
                f"job_id `{_js.get('job_id','?')}`, pid `{_js.get('pid')}`) but that "
                "process is no longer alive — the previous background run likely "
                "stopped without a terminal status. Outputs (if any) are preserved; "
                "you can safely start a new run (the status will be overwritten). "
                "No files were deleted." + _lock_note
            )

    # ── Running banner (visible while executing) ──
    if st.session_state.pipeline_running:
        st.info("🔄  Pipeline is running… Do not close this tab.")

    # ── Execute pipeline — use st.status for real-time step visibility ──
    if st.session_state.pipeline_should_run:
        st.session_state.pipeline_should_run = False

        # Acquire case-level lock BEFORE running any subprocess.
        # If another session won the race between button click and execution,
        # bail out cleanly without touching their lock file.
        # Phase 4.6D — both Full Pipeline and Enrich+Format+Export are tagged "pipeline".
        _acquired = acquire_lock(
            selected_case,
            st.session_state.pipeline_start_step,
            operation="pipeline",
        )
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

            # Phase 4.6J — record run start
            _run_id = _new_run_id()
            _run_started_at = datetime.now().isoformat(timespec="seconds")
            _run_t0 = time.monotonic()
            # Differentiate "pipeline" (full) vs "enrich_format_export" (start_step=1)
            _run_op = "enrich_format_export" if start_step == 1 else "pipeline"
            append_run_history(selected_case, {
                "run_id":      _run_id,
                "case_id":     selected_case,
                "operation":   _run_op,
                "status":      "started",
                "started_at":  _run_started_at,
                "user":        getpass.getuser(),
                "host":        socket.gethostname(),
                "pid":         os.getpid(),
                "start_step":  start_step,
            })

            # v1.2: persistent job status (best-effort; never breaks the run)
            _job_id = None
            try:
                _jcfg   = describe_llm_config()
                _jprov  = str(_jcfg.get("provider", "") or "")
                _jmodel = (os.environ.get("INTERNAL_LLM_MODEL", "") if _jprov == "internal"
                           else (os.environ.get("OPENAI_MODEL", "") or "gpt-4.1-mini"))
                _job = start_job(
                    RUNS_DIR / selected_case,
                    case_id=selected_case, provider=_jprov, model=_jmodel,
                    log_path=str(RUNS_DIR / selected_case / "run_history.jsonl"),
                    pid=os.getpid(),
                    stage=("enrich" if start_step == 1 else "extract"),
                )
                _job_id = _job["job_id"]
            except Exception:
                _job_id = None

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

                        # v1.2: advance persistent job stage (best-effort)
                        if _job_id:
                            try:
                                _STAGES = ["extract", "enrich", "format", "export"]
                                set_stage(RUNS_DIR / selected_case, _job_id,
                                          _STAGES[idx] if idx < len(_STAGES) else "export")
                            except Exception:
                                pass

                        st.write(f"▶  {label}…")
                        _slots = _make_progress_slots()
                        _t0 = time.monotonic()
                        _slots["elapsed"].markdown("⏱  elapsed: 0s  (running…)")
                        rc, output = run_step_streaming(
                            step["cmd"], _make_on_line(_slots, _t0),
                            case_id=selected_case,
                        )
                        _elapsed_final = int(time.monotonic() - _t0)
                        _slots["elapsed"].markdown(
                            f"⏱  elapsed: {_elapsed_final}s"
                        )

                        if rc == 0:
                            st.write(
                                f"✅  {label} — {step['ok_msg']}  "
                                f"({_elapsed_final}s)"
                            )
                        else:
                            st.write(
                                f"❌  {label} failed (exit code {rc})  "
                                f"({_elapsed_final}s)"
                            )

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

            # Phase 4.6J — record terminal status (best-effort, after lock release)
            _last = (st.session_state.pipeline_step_results[-1]
                     if st.session_state.pipeline_step_results else {})
            append_run_history(selected_case, {
                "run_id":       _run_id,
                "case_id":      selected_case,
                "operation":    _run_op,
                "status":       "success" if all_ok else "failed",
                "started_at":   _run_started_at,
                "ended_at":     datetime.now().isoformat(timespec="seconds"),
                "duration_sec": int(time.monotonic() - _run_t0),
                "user":         getpass.getuser(),
                "host":         socket.gethostname(),
                "pid":          os.getpid(),
                "start_step":   start_step,
                "steps":        [{"label": r["label"], "ok": r["ok"], "rc": r["rc"]}
                                 for r in st.session_state.pipeline_step_results],
                "return_code":  _last.get("rc"),
                "message":      (_last.get("ok_msg") if all_ok
                                 else f"failed at: {_last.get('label', '?')}"),
            })

            # v1.2: persistent job terminal status (best-effort; helper masks secrets)
            if _job_id:
                try:
                    if all_ok:
                        finish_job(RUNS_DIR / selected_case, _job_id, "success")
                    else:
                        finish_job(RUNS_DIR / selected_case, _job_id, "failed",
                                   error=f"failed at: {_last.get('label', '?')}")
                except Exception:
                    pass

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

    # ── v1.2: persistent job status readback (state only, no secrets) ──
    _jobst = read_job_status(RUNS_DIR / selected_case)
    if _jobst:
        with st.expander("Job Status", expanded=False):
            st.markdown(
                f"- **status:** `{_jobst.get('status','?')}` · "
                f"**stage:** `{_jobst.get('stage','?')}`\n"
                f"- **provider / model:** `{_jobst.get('provider','?')}` / "
                f"`{_jobst.get('model','?')}`\n"
                f"- **started:** `{_jobst.get('started_at','?')}` · "
                f"**ended:** `{_jobst.get('ended_at') or '—'}`\n"
                f"- **job_id:** `{_jobst.get('job_id','?')}` · "
                f"**pid:** `{_jobst.get('pid','—')}`\n"
                f"- **log_path:** `{_jobst.get('log_path','—')}`"
            )
            if _jobst.get("error"):
                st.warning(f"error: {_jobst['error']}")
            st.caption("From runs/<case>/job_status.json — survives reruns; no secrets shown.")

    st.divider()

# ── Step 4 / 5 / 6: PM review, generate, and download results ─────────────────

def _load_review_source(case_id: str):
    """Load requirements for PM review from the highest-priority existing file.
    Priority: reviewed -> clean -> mock -> raw. Returns (source_file, items)."""
    rd = RUNS_DIR / case_id
    for fname in ("requirements_reviewed.json", "requirements_clean.json",
                  "requirements_mock.json", "requirements.json"):
        p = rd / fname
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        items = data.get("items") or data.get("requirements") or []
        if isinstance(items, list):
            return fname, items
    return None, []


def _to_review_rows(items: list) -> list:
    """Map heterogeneous requirement items to the review table's columns."""
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        cat = it.get("category")
        if isinstance(cat, list):
            cat = cat[0] if cat else ""
        rows.append({
            "id":          str(it.get("id") or it.get("req_id") or ""),
            "category":    str(cat or ""),
            "requirement": str(it.get("requirement") or it.get("text") or ""),
            "must_level":  str(it.get("must_level") or ""),
            "owner":       str(it.get("owner") or ""),
            "pm_comment":  str(it.get("pm_comment") or ""),
            "deleted":     bool(it.get("deleted", False)),
        })
    return rows


st.subheader("Step 4: Review Requirements")
st.caption(
    "Optional: review, edit, add, or mark deleted requirements before filling compliance "
    "responses. Click **Save Reviewed Requirements** to apply changes to the next step. "
    "This edits requirement source only — it does not change `compliance_matrix.xlsx`."
)

# ── v1.4 Phase C-1: Review/Edit table MVP. app.py only; writes a SEPARATE
# requirements_reviewed.json and never overwrites requirements.json / _clean.json. ──
if mode == "Select Existing Case" and selected_case:
    _rev_source, _rev_items = _load_review_source(selected_case)
    if not _rev_source:
        st.info("No requirements found yet. Run **Analyze RFQ** (or the No-AI Flow "
                "Test) to produce requirements to review.")
    else:
        st.caption(
            f"Loaded from: `{_rev_source}`"
            + (" — your prior review" if _rev_source == "requirements_reviewed.json" else "")
        )
        _rev_df = pd.DataFrame(
            _to_review_rows(_rev_items),
            columns=["id", "category", "requirement", "must_level", "owner", "pm_comment", "deleted"],
        )
        _edited_rev = st.data_editor(
            _rev_df,
            key=f"review_editor_{selected_case}",
            num_rows="dynamic",
            use_container_width=True,
        )
        st.caption("Edit category / requirement / must_level / owner / pm_comment · "
                   "mark **deleted** to exclude (row is kept, flagged) · add rows at the bottom.")
        if st.button("Save Reviewed Requirements", type="primary",
                     key=f"save_reviewed_{selected_case}"):
            try:
                _recs = _edited_rev.fillna("").to_dict(orient="records")
                for _r in _recs:
                    _r["deleted"] = bool(_r.get("deleted"))
                _payload = {
                    "meta": {
                        "review_status": "pm_reviewed",
                        "source_file": _rev_source,
                        "saved_by": "streamlit_ui",
                        "schema_version": "v1.4_review_mvp",
                    },
                    "requirements": _recs,
                }
                _rev_out = RUNS_DIR / selected_case / "requirements_reviewed.json"
                _rev_out.write_text(
                    json.dumps(_payload, ensure_ascii=False, indent=2), encoding="utf-8")
                _kept = sum(1 for _r in _recs if not _r["deleted"])
                st.success(f"Saved `requirements_reviewed.json` — {len(_recs)} row(s), "
                           f"{_kept} active (not deleted).")
                st.warning("This is PM review output. It does NOT update "
                           "`compliance_matrix.xlsx`. Generating the matrix from "
                           "reviewed output is planned for the next phase.")
            except Exception as _e:
                st.error(f"Failed to save reviewed requirements: {_e}")

st.divider()
st.subheader("Step 5: Fill Compliance Responses")
st.caption(
    "Optional: fill compliance status (complied / partial / not complied), response, and "
    "remarks before generating the Excel matrix. If left blank, the matrix keeps these fields "
    "empty. This is **compliance response filling — not requirement source editing** "
    "(use Step 4: Review Requirements for that)."
)

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
            "You can still download the empty templates from Step 6 above."
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
    # Phase 4.6E.1: "Excluded" filter shows rows PM marked exclude_from_matrix=True
    status_opts = ["All", "NEED_REVIEW", "NEW", "PENDING", "COMPLIANT", "PARTIAL", "NON-COMPLIANT", "Excluded"]

    filter_status = f1.selectbox("Status",   status_opts,             key="rv_status")
    filter_cat    = f2.selectbox("Category", ["All"] + all_cats,      key="rv_cat")
    filter_owner  = f3.selectbox("Owner",    ["All"] + all_owners,    key="rv_owner")

    # 套用篩選
    filtered = []
    for item in items:
        rid = item.get("req_id", "")
        _cur_resp = responses.get(rid, {})
        cur_status = _cur_resp.get("status") or item.get("status", "NEW")
        _is_pm_excluded = _cur_resp.get("exclude_from_matrix") is True
        # Phase 4.6E.1: "Excluded" filter — only show PM-excluded rows
        if filter_status == "Excluded":
            if not _is_pm_excluded:
                continue
        elif filter_status != "All":
            if cur_status != filter_status:
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
        # Phase 4.6E.1: tag rows PM marked exclude_from_matrix=True
        _is_pm_excluded = cur.get("exclude_from_matrix") is True
        _excluded_tag = " [EXCLUDED]" if _is_pm_excluded else ""
        label = f"{rid} | {cat} | {cur_status}{_derived_tag}{_excluded_tag}"

        with st.expander(label):
            if _is_derived:
                st.caption("Derived from spec table — not an explicit customer requirement. Confirm against design.")

            # ── Requirement (Original) — never mutated ──
            st.markdown(f"**Requirement (Original):** {item.get('requirement', '')}")

            # ── Phase 4.6E.1: read-only Normalized display (when present) ──
            _norm_text = (item.get("normalized_requirement") or "").strip()
            _rewrite_reason = (item.get("rewrite_reason") or "").strip()
            if _norm_text:
                st.markdown(f"**Requirement (Normalized):** {_norm_text}")
                try:
                    _conf_str = f"{float(item.get('rewrite_confidence') or 0):.2f}"
                except (TypeError, ValueError):
                    _conf_str = "—"
                _info_bits = []
                if _rewrite_reason:
                    _info_bits.append(f"Rewrite Reason: `{_rewrite_reason}`")
                _info_bits.append(f"Confidence: `{_conf_str}`")
                st.caption(" · ".join(_info_bits))
                if bool(item.get("needs_rewrite_review", False)):
                    st.warning(
                        "⚠️ **REVIEW** — Normalized text may contain hallucinated "
                        "tokens not present in the Original. Compare carefully "
                        "before using."
                    )
            elif _rewrite_reason == "already_complete":
                st.caption("✓ Already complete — Original is a standalone requirement.")

            # ── Context: meta / risk / source / ai_draft (unchanged) ──
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

            # ── Phase 4.6E.1: PM Final Requirement (editable, fallback chain) ──
            # Default value:  PM edit (responses.final_requirement) →
            #                 LLM normalized (item.normalized_requirement) →
            #                 Original (item.requirement)
            _pm_final_saved = (cur.get("final_requirement") or "").strip()
            _orig_text = item.get("requirement", "") or ""
            if _pm_final_saved:
                _default_final  = _pm_final_saved
                _default_source = "PM edit"
            elif _norm_text:
                _default_final  = _norm_text
                _default_source = "normalized"
            else:
                _default_final  = _orig_text
                _default_source = "original"
            new_final = st.text_area(
                "PM Final Requirement",
                value=_default_final,
                key=f"fr_{_key}",
                help="Text that will appear in the final compliance_matrix.xlsx "
                     "(Phase 4.6E.2 will wire this into the Excel export). "
                     "Defaults to Normalized if available, else Original. Edit freely.",
            )
            st.caption(f"Default source: **{_default_source}**")

            # ── Phase 4.6E.1: Exclude from final matrix ──
            _excol1, _excol2 = st.columns([1, 3])
            new_exclude = _excol1.checkbox(
                "Exclude from final matrix",
                value=bool(cur.get("exclude_from_matrix", False)),
                key=f"ex_{_key}",
                help="Mark to move this row to the 'Excluded' sheet of "
                     "compliance_matrix.xlsx (Phase 4.6E.2 — Excel wiring "
                     "is not yet active in this build).",
            )
            new_exclude_reason = _excol2.text_input(
                "Exclude reason",
                value=cur.get("exclude_reason", "") or "",
                disabled=not new_exclude,
                key=f"er_{_key}",
                help="Why this row is excluded (free text). Saved to "
                     "responses.json regardless of toggle state, so re-enabling "
                     "Exclude restores the previous reason.",
            )

            # ── Existing edit fields ──
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
                    # Phase 4.6E.1 — three new fields:
                    final_requirement=new_final,
                    exclude_from_matrix=new_exclude,
                    exclude_reason=new_exclude_reason,
                )
                st.success("Saved.")

st.subheader("Step 6: Generate Compliance Matrix")
st.caption("Generate or refresh compliance_matrix.xlsx using the latest cleaned requirements "
           "and saved compliance responses. Reviewed requirement source support is planned "
           "for the next milestone.")
st.info("Current version uses requirements_clean.json plus saved compliance responses. "
        "PM-reviewed requirement edits from Step 4 will be connected in the next milestone.")

# ── v1.4 M5: explicit Generate Compliance Matrix action. Re-runs the existing
# export_excel.py (cleaned requirements + saved responses) — no LLM, no
# re-extraction, no full pipeline. Does NOT use requirements_reviewed.json yet. ──
_gm_clean = RUNS_DIR / selected_case / "requirements_clean.json"
_gm_li = read_lock_info(selected_case)
_gm_lock_active = bool(_gm_li) and not is_lock_stale(_gm_li)
_gm_running = st.session_state.get("pipeline_running", False)
if not _gm_clean.exists():
    st.caption("Please analyze the RFQ first before generating the matrix.")
if st.button("Generate Compliance Matrix", type="primary",
             key=f"gen_matrix_{selected_case}",
             disabled=(_gm_running or _gm_lock_active or not _gm_clean.exists()),
             help="Run export_excel.py to (re)build compliance_matrix.xlsx from "
                  "requirements_clean.json + saved responses. No LLM, no re-extraction."):
    if not _gm_clean.exists():
        st.warning("Please analyze the RFQ first before generating the matrix.")
    else:
        try:
            _gm_proc = subprocess.run(
                [PYTHON, str(AI_RFX_DIR / "export_excel.py"),
                 "--in", str(_gm_clean),
                 "--out", str(RUNS_DIR / selected_case / "compliance_matrix.xlsx"),
                 "--responses", str(RESPONSES_DIR / selected_case / "responses.json")],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
            if _gm_proc.returncode == 0:
                st.success("Compliance Matrix generated successfully.")
                if _gm_proc.stdout:
                    st.code(_gm_proc.stdout, language="text")
            else:
                st.error(f"Matrix generation failed (exit {_gm_proc.returncode}).")
                if _gm_proc.stderr:
                    st.code(_gm_proc.stderr, language="text")
        except Exception as _e:
            st.error(f"Failed to generate matrix: {_e}")

st.subheader("Step 7: Download Results")
st.caption("Download the latest compliance_matrix.xlsx (the primary deliverable). Review "
           "sheets, raw JSON, logs, and cache details are under Advanced / Diagnostics.")

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

# Phase 8C — Incremental cache summary (observability ONLY). This never skips
# LLM work, never reuses old extraction output, and never changes extraction
# behavior. It just displays runs/<case>/extraction_cache.json when present and
# stays silent/empty otherwise (backward-compatible).
with st.expander("Incremental Cache Summary", expanded=False):
    # Optional, low-risk action first so freshly generated metrics render below
    # in the same run. cache_metadata.py is report-only — it writes the
    # gitignored runs/<case>/extraction_cache.json and touches no pipeline output.
    if st.button("Generate cache summary", key=f"gen_cache_{selected_case}",
                 help="Runs scripts/cache_metadata.py for this case (report-only — no LLM work)."):
        _cache_script = AI_RFX_DIR / "cache_metadata.py"
        if not _cache_script.exists():
            st.error(f"Not found: {_cache_script}")
        elif not (RUNS_DIR / selected_case / "requirements_clean.json").exists():
            st.warning("requirements_clean.json missing — run the pipeline (Steps 1–3) first.")
        else:
            try:
                _proc = subprocess.run(
                    [PYTHON, str(_cache_script),
                     "--case", selected_case, "--runs", str(RUNS_DIR)],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=120,
                )
                if _proc.returncode == 0:
                    st.success("Cache summary generated.")
                else:
                    st.error(f"cache_metadata.py exited {_proc.returncode}")
                if _proc.stdout:
                    st.code(_proc.stdout, language="text")
            except Exception as _e:
                st.error(f"Failed to run cache_metadata.py: {_e}")

    _cache = load_cache_summary(selected_case)
    if _cache:
        _cc = st.columns(5)
        _cc[0].metric("Total requirements", _cache["total_requirements"])
        _cc[1].metric("Reused from cache",  _cache["reused_from_cache"])
        _cc[2].metric("Reprocessed",        _cache["reprocessed"])
        _cc[3].metric("Skipped unchanged",  _cache["skipped_unchanged"])
        _cc[4].metric("Est. runtime saved", _fmt_runtime_saved(_cache["estimated_runtime_saved_sec"]))
        st.caption("Report-only. No LLM work is skipped yet.")
        if _cache.get("generated_at"):
            _meta_line = f"Generated: {_cache['generated_at']}"
            if _cache.get("model_name"):
                _meta_line += f" · model: {_cache['model_name']}"
            st.caption(_meta_line)
    else:
        st.info(
            "Cache summary not generated yet. "
            "Run cache metadata step to view cache reuse estimate."
        )

# Phase 4.6G — surface chunks that were soft-failed during extraction so PM
# notices they didn't make it into the compliance matrix.
_errors_path = RUNS_DIR / selected_case / "extract_errors.jsonl"
if _errors_path.exists():
    try:
        _err_count = sum(
            1 for ln in _errors_path.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip()
        )
    except Exception:
        _err_count = 0
    if _err_count > 0:
        st.warning(
            f"⚠ **{_err_count} chunk(s) were skipped during extraction** "
            f"(LLM retry exhausted). Those chunks are not represented in the "
            f"compliance matrix. See `extract_errors.jsonl` under **Advanced "
            f"Outputs** below for details, or rerun the pipeline from the "
            f"command line with `--retry-failed-chunks` to re-attempt them."
        )

# Phase 4.6J — Recent runs preview (latest 5, collapsed by default)
_recent_runs = read_run_history(selected_case, limit=5)
if _recent_runs:
    _STATUS_ICON = {
        "success":   "✅",
        "failed":    "❌",
        "cancelled": "⏹",
        "started":   "▶",
    }
    with st.expander(f"Recent runs ({len(_recent_runs)})", expanded=False):
        for _r in _recent_runs:
            _icon  = _STATUS_ICON.get(_r.get("status", ""), "·")
            _t     = _r.get("ended_at") or _r.get("started_at") or "?"
            _op    = _r.get("operation", "?")
            _st_   = _r.get("status", "?")
            _dur   = _r.get("duration_sec")
            _dur_s = f" · {_dur}s" if isinstance(_dur, int) else ""
            _msg   = _r.get("message") or ""
            _msg_s = f" — {_msg}" if _msg else ""
            st.markdown(
                f"- {_icon} `{_t}` · **{_op}** · {_st_}{_dur_s}{_msg_s}"
            )

run_dir = RUNS_DIR / selected_case
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

PRIMARY_FILES = [
    ("compliance_matrix.xlsx",   "Compliance Matrix", "Final deliverable for distribution to customer."),
]
ADVANCED_FILES = [
    ("requirements_review.xlsx", "Review Excel"),
    ("requirements.json",          "Requirements (raw)"),
    ("requirements_enriched.json", "Requirements (enriched)"),
    ("requirements_clean.json",    "Requirements (clean)"),
    ("requirements.partial.jsonl", "Requirements (partial / resume)"),
    ("extract_errors.jsonl",       "Extraction errors (soft-fail log)"),
    ("run_history.jsonl",          "Run history (Phase 4.6J)"),
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
        with st.expander("Advanced / Diagnostics", expanded=False):
            for filename, label in ADVANCED_FILES:
                fpath = run_dir / filename
                if fpath.exists():
                    size_kb = fpath.stat().st_size / 1024
                    st.success(f"**{label}** — `{filename}` ({size_kb:.1f} KB)")

st.divider()

# ── Step 3.5: Normalize Requirements (Phase 4.6C, optional, opt-in) ──────────

if mode == "Select Existing Case":
    st.subheader("Advanced: Improve requirement wording with AI (optional)")
    st.markdown(
        "<p style='font-size:1.0rem;color:#455A64;margin-bottom:6px;'>"
        "Use the LLM to rewrite fragment-style requirements into complete, "
        "verifiable, standalone form. Adds 4 columns to "
        "<code>compliance_matrix.xlsx</code>. <b>Does NOT change</b> "
        "<code>req_id</code> or the original requirement text.</p>",
        unsafe_allow_html=True,
    )

    _norm_clean_p = RUNS_DIR / selected_case / "requirements_clean.json"
    if not _norm_clean_p.exists():
        st.info(
            "ℹ️ Run Analyze RFQ first to produce "
            "`requirements_clean.json` before normalization."
        )
    else:
        # ── Read clean.json for eligibility + cumulative stats ──
        try:
            _norm_clean = json.loads(_norm_clean_p.read_text(encoding="utf-8"))
            _norm_items = _norm_clean.get("items", [])
        except Exception:
            _norm_items = []
        _norm_eligible = [
            i for i in _norm_items if (i.get("type") or "").lower() == "requirement"
        ]
        _norm_already = sum(
            1 for i in _norm_eligible
            if (i.get("rewrite_reason") or "").strip()
            and (i.get("rewrite_reason") or "").strip() != "not_attempted"
        )
        _norm_remaining = len(_norm_eligible) - _norm_already

        # ── LLM availability ──
        _norm_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()
        _norm_has_api = bool(os.environ.get("OPENAI_API_KEY")) or _norm_provider == "internal"

        # ── Lock state (shared with Phase 2 pipeline lock) ──
        _norm_lock_info  = read_lock_info(selected_case)
        _norm_lock_stale = is_lock_stale(_norm_lock_info) if _norm_lock_info else False
        _norm_lock_active = bool(_norm_lock_info) and not _norm_lock_stale

        # ── Mode selectbox + force checkbox ──
        _norm_col_m, _norm_col_f = st.columns([3, 1])
        _mode_options = ["Sample 10 (default)", "Sample 30", "All eligible items"]
        _mode_to_internal = {
            "Sample 10 (default)": "sample10",
            "Sample 30":            "sample30",
            "All eligible items":   "all",
        }
        _internal_to_mode = {v: k for k, v in _mode_to_internal.items()}
        _default_label = _internal_to_mode.get(
            st.session_state.normalize_sample_mode, "Sample 10 (default)"
        )
        _picked_label = _norm_col_m.selectbox(
            "Mode",
            options=_mode_options,
            index=_mode_options.index(_default_label),
            disabled=st.session_state.normalize_running or _norm_lock_active,
            key=f"normalize_mode_{selected_case}",
        )
        st.session_state.normalize_sample_mode = _mode_to_internal[_picked_label]
        _internal_mode = st.session_state.normalize_sample_mode

        _force_val = _norm_col_f.checkbox(
            "--force",
            value=st.session_state.normalize_force,
            disabled=st.session_state.normalize_running or _norm_lock_active,
            help="Re-normalize rows that already have rewrite_reason set",
            key=f"normalize_force_{selected_case}",
        )
        st.session_state.normalize_force = _force_val

        # ── "All" confirm checkbox + warnings ──
        _all_confirmed = True
        if _internal_mode == "all":
            st.warning(
                f"⚠️ **All mode** will process **{_norm_remaining}** remaining items "
                f"({len(_norm_eligible)} eligible total). "
                "Estimated 4-7 min, ~$0.50-0.80."
            )
            _all_confirmed = st.checkbox(
                "I understand --all will process all eligible items",
                value=False,
                disabled=st.session_state.normalize_running or _norm_lock_active,
                key=f"normalize_confirm_all_{selected_case}",
            )

        if not _norm_has_api:
            st.warning(
                "⚠️ LLM not configured — set `OPENAI_API_KEY` or "
                "`LLM_PROVIDER=internal` and restart Streamlit."
            )

        if _norm_lock_active:
            # Phase 4.6D — surface what kind of run holds the lock
            _norm_op = (_norm_lock_info or {}).get("operation", "")
            _norm_op = (_norm_op or "").strip().lower()
            if _norm_op == "pipeline":
                _norm_what = "a pipeline run"
            elif _norm_op == "normalize":
                _norm_what = "another normalize run"
            else:
                _norm_what = "another session"
            st.info(
                f"🔒 Cannot normalize — case is locked by {_norm_what}. "
                "See the lock banner under Step 3 for details."
            )

        # ── Run button ──
        _norm_disabled = (
            st.session_state.normalize_running
            or _norm_lock_active
            or not _norm_has_api
            or (_internal_mode == "all" and not _all_confirmed)
        )
        if _norm_lock_active:
            _norm_btn_label = "🔒  Locked by another session"
        elif st.session_state.normalize_running:
            _norm_btn_label = "⏳  Normalize Running…"
        else:
            _norm_btn_label = "🪄 Run Normalize"
        run_normalize = st.button(
            _norm_btn_label,
            type="primary",
            disabled=_norm_disabled,
            use_container_width=False,
            key=f"normalize_run_{selected_case}",
        )

        if run_normalize and not st.session_state.normalize_running:
            st.session_state.normalize_running      = True
            st.session_state.normalize_should_run   = True
            st.session_state.normalize_step_results = []
            st.rerun()

        if st.session_state.normalize_running:
            st.info("🔄 Normalize is running… Do not close this tab.")

        # ── Execute normalize when triggered ──
        if st.session_state.normalize_should_run:
            st.session_state.normalize_should_run = False
            # Acquire same case-level lock as pipeline (Phase 2).
            # Phase 4.6D — tag this acquisition as "normalize" so the UI can
            # distinguish it from a pipeline run.
            _norm_acq = acquire_lock(selected_case, 0, operation="normalize")
            if _norm_acq is None:
                st.session_state.normalize_running = False
                st.error(
                    "🔒 Could not acquire pipeline lock — another session "
                    "started running for this case. Retry after it finishes."
                )
                st.rerun()
            else:
                _norm_cmd = [
                    PYTHON,
                    str(AI_RFX_DIR / "normalize_requirements_llm.py"),
                    "--case", selected_case,
                ]
                if _internal_mode == "sample10":
                    _norm_cmd += ["--sample", "10"]
                elif _internal_mode == "sample30":
                    _norm_cmd += ["--sample", "30"]
                elif _internal_mode == "all":
                    _norm_cmd += ["--all"]
                if st.session_state.normalize_force:
                    _norm_cmd += ["--force"]

                _norm_case_runs = RUNS_DIR / selected_case
                _norm_export_cmd = [
                    PYTHON,
                    str(AI_RFX_DIR / "export_excel.py"),
                    "--in",  str(_norm_case_runs / "requirements_clean.json"),
                    "--out", str(_norm_case_runs / "compliance_matrix.xlsx"),
                    "--responses", str(RESPONSES_DIR / selected_case / "responses.json"),
                ]

                _NORMALIZE_STEPS = [
                    {"label": "Normalize — LLM rewrite",                "cmd": _norm_cmd},
                    {"label": "Export — refresh compliance_matrix.xlsx", "cmd": _norm_export_cmd},
                ]

                _all_ok = True

                # Phase 4.6J — record normalize run start
                _norm_run_id = _new_run_id()
                _norm_started_at = datetime.now().isoformat(timespec="seconds")
                _norm_t0 = time.monotonic()
                append_run_history(selected_case, {
                    "run_id":     _norm_run_id,
                    "case_id":    selected_case,
                    "operation":  "normalize",
                    "status":     "started",
                    "started_at": _norm_started_at,
                    "user":       getpass.getuser(),
                    "host":       socket.gethostname(),
                    "pid":        os.getpid(),
                    "start_step": 0,
                })

                try:
                    with st.status("Running normalize…", expanded=True) as _norm_status:
                        for _step in _NORMALIZE_STEPS:
                            _lbl = _step["label"]
                            st.write(f"▶  {_lbl}…")
                            _slots = _make_progress_slots()
                            _t0 = time.monotonic()
                            _slots["elapsed"].markdown(
                                "⏱  elapsed: 0s  (running…)"
                            )
                            _rc, _out = run_step_streaming(
                                _step["cmd"], _make_on_line(_slots, _t0),
                                case_id=selected_case,
                            )
                            _elapsed_final = int(time.monotonic() - _t0)
                            _slots["elapsed"].markdown(
                                f"⏱  elapsed: {_elapsed_final}s"
                            )
                            if _rc == 0:
                                st.write(f"✅  {_lbl}  ({_elapsed_final}s)")
                            else:
                                st.write(
                                    f"❌  {_lbl} failed (exit code {_rc})  "
                                    f"({_elapsed_final}s)"
                                )
                            st.session_state.normalize_step_results.append(
                                {"ok": _rc == 0, "label": _lbl, "rc": _rc, "output": _out}
                            )
                            if _rc != 0:
                                _all_ok = False
                                break
                        if _all_ok:
                            _norm_status.update(
                                label="✅ Normalize complete",
                                state="complete", expanded=False,
                            )
                        else:
                            _norm_status.update(
                                label="❌ Normalize failed — see log below",
                                state="error", expanded=True,
                            )
                finally:
                    release_lock(selected_case)

                # Phase 4.6J — record normalize terminal status
                _norm_last = (st.session_state.normalize_step_results[-1]
                              if st.session_state.normalize_step_results else {})
                append_run_history(selected_case, {
                    "run_id":       _norm_run_id,
                    "case_id":      selected_case,
                    "operation":    "normalize",
                    "status":       "success" if _all_ok else "failed",
                    "started_at":   _norm_started_at,
                    "ended_at":     datetime.now().isoformat(timespec="seconds"),
                    "duration_sec": int(time.monotonic() - _norm_t0),
                    "user":         getpass.getuser(),
                    "host":         socket.gethostname(),
                    "pid":          os.getpid(),
                    "start_step":   0,
                    "steps":        [{"label": r["label"], "ok": r["ok"], "rc": r["rc"]}
                                     for r in st.session_state.normalize_step_results],
                    "return_code":  _norm_last.get("rc"),
                    "message":      ("normalize complete" if _all_ok
                                     else f"failed at: {_norm_last.get('label', '?')}"),
                })

                st.session_state.normalize_running = False
                st.rerun()

        # ── Display stored step results ──
        for _res in st.session_state.normalize_step_results:
            if _res["ok"]:
                st.success(f"✅  {_res['label']}")
            else:
                st.error(f"❌  {_res['label']} failed (exit code {_res['rc']})")
            if _res["output"]:
                with st.expander(f"Log: {_res['label']}", expanded=not _res["ok"]):
                    st.code(_res["output"], language="text")

        # ── This run summary (parsed from stdout; silent fallback if parse fails) ──
        if st.session_state.normalize_step_results:
            _norm_res = next(
                (r for r in st.session_state.normalize_step_results
                 if "Normalize" in r["label"]),
                None,
            )
            if _norm_res and _norm_res.get("output"):
                _summary = _parse_normalize_summary(_norm_res["output"])
                if _summary:
                    st.markdown("**This run:**")
                    _c1, _c2, _c3, _c4, _c5 = st.columns(5)
                    _c1.metric("Processed",         _summary.get("processed", 0))
                    _c2.metric("Skipped",           _summary.get("skipped_idempotent", 0))
                    _c3.metric("Already Complete",  _summary.get("already_complete", 0))
                    _c4.metric("Fragment→Std",      _summary.get("fragment_to_standalone", 0))
                    _c5.metric("Needs Review",      _summary.get("needs_review_set", 0))

        # ── Case cumulative state (always shown, even before any normalize run) ──
        if _norm_items:
            _cum_norm = sum(
                1 for i in _norm_items if (i.get("normalized_requirement") or "").strip()
            )
            _cum_review = sum(1 for i in _norm_items if i.get("needs_rewrite_review"))
            _cum_reasons: dict = {}
            for i in _norm_items:
                _rsn = (i.get("rewrite_reason") or "").strip()
                if _rsn:
                    _cum_reasons[_rsn] = _cum_reasons.get(_rsn, 0) + 1
            with st.expander("📊 Case cumulative state (from clean.json)", expanded=False):
                st.markdown(
                    f"- normalized_requirement non-empty: **{_cum_norm}** / "
                    f"{len(_norm_items)} items\n"
                    f"- needs_rewrite_review flagged: **{_cum_review}**\n"
                    f"- eligible (type=requirement): **{len(_norm_eligible)}**\n"
                    f"- already normalized: **{_norm_already}**, remaining: **{_norm_remaining}**"
                )
                if _cum_reasons:
                    st.markdown("rewrite_reason distribution:")
                    for _r, _n in sorted(_cum_reasons.items()):
                        st.markdown(f"  - `{_r}`: {_n}")

        # ── Download button for refreshed Excel ──
        _norm_xlsx = RUNS_DIR / selected_case / "compliance_matrix.xlsx"
        if _norm_xlsx.exists():
            _xlsx_size_kb = _norm_xlsx.stat().st_size / 1024
            st.download_button(
                label=f"📥 Download Compliance Matrix ({_xlsx_size_kb:.1f} KB)",
                data=_norm_xlsx.read_bytes(),
                file_name="compliance_matrix.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_normalize_xlsx_{selected_case}",
            )

