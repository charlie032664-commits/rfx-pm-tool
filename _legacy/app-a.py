import json
import os
import subprocess
import sys
import streamlit as st
import yaml
from pathlib import Path

BASE_DIR    = Path(__file__).parent
INBOUND_DIR = BASE_DIR / "inbound"
RUNS_DIR    = BASE_DIR / "runs"
AI_RFX_DIR  = BASE_DIR.parent / "ai_rfx"   # original scripts, never modified
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

    raw = _load("requirements.json")
    if raw:
        counts["raw"] = len(raw.get("requirements", []))

    enriched = _load("requirements_enriched.json")
    if enriched:
        counts["enriched"] = len(enriched.get("requirements", []))

    clean = _load("requirements_clean.json")
    if clean:
        counts["clean"] = len([
            i for i in clean.get("items", []) if i.get("type") == "requirement"
        ])

    counts["files"] = sum(1 for f in run_dir.iterdir() if f.is_file())
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

st.divider()

# ── Run Pipeline ──────────────────────────────────────────────────────────────

if mode == "Select Existing Case":
    st.subheader("Step 2: Run Pipeline")

    case_inbound = INBOUND_DIR / selected_case
    case_runs    = RUNS_DIR / selected_case

    PIPELINE_STEPS = [
        {
            "label": "Step 1 — Extract Requirements (LLM)",
            "ok_msg": "Requirements extracted from RFQ documents and saved.",
            "cmd": [
                PYTHON,
                str(AI_RFX_DIR / "extract_requirements_llm.py"),
                "--case",  str(case_inbound),
                "--runs",  str(RUNS_DIR),
                "--resume",
            ],
            "requires_api_key": True,
        },
        {
            "label": "Step 2 — Enrich with Rules",
            "ok_msg": "Category, owner, and must-level assigned to all requirements.",
            "cmd": [
                PYTHON,
                str(AI_RFX_DIR / "run_case.py"),
                "--case",  str(case_inbound),
                "--rules", str(BASE_DIR / "rules"),
                "--runs",  str(RUNS_DIR),
            ],
            "requires_api_key": False,
        },
        {
            "label": "Step 3 — Post-process",
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
            "label": "Step 4 — Export Excel",
            "ok_msg": "Compliance matrix exported and ready for distribution.",
            "cmd": [
                PYTHON,
                str(AI_RFX_DIR / "export_excel.py"),
                "--in",  str(case_runs / "requirements_enriched.json"),
                "--out", str(case_runs / "compliance_matrix.xlsx"),
            ],
            "requires_api_key": False,
        },
    ]

    has_api_key = bool(os.environ.get("OPENAI_API_KEY"))
    if not has_api_key:
        st.warning("OPENAI_API_KEY is not set — Step 1 (LLM extraction) will fail. Steps 2–4 can still run if requirements.json already exists.")

    st.markdown(
        "<p style='font-size:1.0rem;color:#455A64;margin-bottom:8px;'>"
        "ℹ️ Step 1 may take several minutes for multiple files. "
        "Partial results are written during processing and the run can be resumed if interrupted.</p>",
        unsafe_allow_html=True,
    )

    # ── Button (disabled while running) ──
    btn_label = "⏳  Pipeline Running…" if st.session_state.pipeline_running else "▶  Run Full Pipeline"
    run_all = st.button(btn_label, type="primary", disabled=st.session_state.pipeline_running)

    if run_all and not st.session_state.pipeline_running:
        st.session_state.pipeline_running      = True
        st.session_state.pipeline_should_run   = True
        st.session_state.pipeline_step_results = []
        st.rerun()  # re-render with disabled button before executing

    # ── Running banner (visible while executing) ──
    if st.session_state.pipeline_running:
        st.info("🔄  Pipeline is running… Do not close this tab.")

    # ── Execute pipeline — use st.status for real-time step visibility ──
    if st.session_state.pipeline_should_run:
        st.session_state.pipeline_should_run = False
        all_ok = True

        with st.status("Running pipeline…", expanded=True) as status:
            for step in PIPELINE_STEPS:
                label = step["label"]

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
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Raw Requirements",   counts.get("raw",      "—"))
        c2.metric("Enriched",           counts.get("enriched", "—"))
        c3.metric("Clean Requirements", counts.get("clean",    "—"))
        c4.metric("Output Files",       counts.get("files",    "—"))
        st.markdown(
            "<p style='font-size:0.95rem;color:#546E7A;margin-top:4px;'>"
            "Clean = classified as 'requirement' type (excludes glossary, notes, junk)</p>",
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
