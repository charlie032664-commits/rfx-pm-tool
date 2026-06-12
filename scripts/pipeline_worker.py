# -*- coding: utf-8 -*-
"""Detached pipeline worker (v1.2 robustness).

Runs the 4-stage pipeline (extract -> enrich -> format -> export) as a standalone
process so a Streamlit rerun / refresh cannot interrupt it. It updates the
persistent job status (runs/<case>/job_status.json + jobs.jsonl via job_status.py)
and writes runs/<case>/pipeline_worker.log.

It invokes the existing pipeline scripts as subprocesses — it does NOT import or
modify them. Returns 0 on success, non-zero on failure.

No secrets are printed: the API key lives in the environment, never in argv or
the log. Use --dry-run to exercise the job-status flow without any LLM call.

Usage:
    python pipeline_worker.py --case <inbound/case> --runs <runs> --rules <rules> \
        [--responses <responses.json>] [--start-step 0] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from env_loader import load_env, describe_llm_config  # noqa: E402
import job_status as js  # noqa: E402

STAGE_ORDER = ["extract", "enrich", "format", "export"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _resolve_model(provider: str) -> str:
    if provider == "internal":
        return os.environ.get("INTERNAL_LLM_MODEL", "") or "(unset)"
    return os.environ.get("OPENAI_MODEL", "") or "gpt-4.1-mini"


def _resolve_case_id(case_dir: Path, explicit: str) -> str:
    if explicit:
        return explicit
    cy = case_dir / "meta" / "case.yaml"
    if cy.exists():
        try:
            import yaml
            m = yaml.safe_load(cy.read_text(encoding="utf-8")) or {}
            return str(m.get("case_id") or case_dir.name)
        except Exception:
            pass
    return case_dir.name


def _build_stages(py: str, scripts: Path, case_dir: Path, runs_root: Path,
                  rules: str, responses: str, case_runs: Path,
                  max_chars: int, group_size: int):
    """Mirror app.py PIPELINE_STEPS as (stage, argv) pairs."""
    export_cmd = [py, str(scripts / "export_excel.py"),
                  "--in", str(case_runs / "requirements_clean.json"),
                  "--out", str(case_runs / "compliance_matrix.xlsx")]
    if responses:
        export_cmd += ["--responses", responses]
    return [
        ("extract", [py, str(scripts / "extract_requirements_llm.py"),
                     "--case", str(case_dir), "--runs", str(runs_root),
                     "--resume", "--max-chars", str(max_chars),
                     "--group-size", str(group_size)]),
        ("enrich", [py, str(scripts / "run_case.py"),
                    "--case", str(case_dir), "--rules", str(rules),
                    "--runs", str(runs_root)]),
        ("format", [py, str(scripts / "postprocess_requirements.py"),
                    "--in", str(case_runs / "requirements_enriched.json"),
                    "--out_dir", str(case_runs)]),
        ("export", export_cmd),
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description="Detached pipeline worker.")
    ap.add_argument("--case", required=True, help="Inbound case folder")
    ap.add_argument("--runs", required=True, help="Runs root")
    ap.add_argument("--rules", required=True, help="Rules folder")
    ap.add_argument("--responses", default="", help="responses.json (optional)")
    ap.add_argument("--case-id", default="", help="Explicit case_id (else derived)")
    ap.add_argument("--start-step", type=int, default=0, help="0=full, 1=skip extract")
    ap.add_argument("--max-chars", type=int, default=600)
    ap.add_argument("--group-size", type=int, default=2)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--dry-run", action="store_true",
                    help="Walk the job-status flow without running any subprocess/LLM.")
    args = ap.parse_args()

    load_env()
    scripts = Path(__file__).resolve().parent
    # Resolve to absolute so the sub-scripts (which resolve relative paths against
    # their own dir) receive unambiguous paths regardless of the worker's cwd.
    case_dir = Path(args.case).resolve()
    runs_root = Path(args.runs).resolve()
    case_id = _resolve_case_id(case_dir, args.case_id)
    run_dir = runs_root / case_id
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "pipeline_worker.log"
    case_runs = run_dir

    cfg = describe_llm_config()
    provider = str(cfg.get("provider", "") or "")
    model = _resolve_model(provider)

    _rules_abs = str(Path(args.rules).resolve())
    _resp_abs = ""
    if args.responses:
        _rp = Path(args.responses).resolve()
        if _rp.exists():
            _resp_abs = str(_rp)
    stages = _build_stages(args.python, scripts, case_dir, runs_root, _rules_abs,
                           _resp_abs, case_runs, args.max_chars, args.group_size)

    job = js.start_job(run_dir, case_id=case_id, provider=provider, model=model,
                       log_path=str(log_path), pid=os.getpid(),
                       stage=("enrich" if args.start_step == 1 else "extract"))
    jid = job["job_id"]

    rc_final = 0
    failed_stage = None
    try:
        with log_path.open("a", encoding="utf-8") as logf:
            def w(msg: str) -> None:
                logf.write(f"[{_now()}] {msg}\n")
                logf.flush()

            w(f"worker start job_id={jid} case={case_id} provider={provider} "
              f"model={model} start_step={args.start_step} dry_run={args.dry_run}")

            for idx, (stage, cmd) in enumerate(stages):
                if idx < args.start_step:
                    w(f"skip stage={stage}")
                    continue
                js.set_stage(run_dir, jid, stage)
                if args.dry_run:
                    w(f"stage={stage} DRY-RUN (cmd has {len(cmd)} args; not executed)")
                    continue
                w(f"stage={stage} START")
                logf.flush()
                try:
                    proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT,
                                          cwd=str(scripts.parent), env=os.environ.copy())
                    rc = proc.returncode
                except Exception as e:
                    rc = 1
                    w(f"stage={stage} EXCEPTION {type(e).__name__}")
                w(f"stage={stage} END rc={rc}")
                if rc != 0:
                    rc_final = rc
                    failed_stage = stage
                    break
    except Exception as e:
        rc_final = rc_final or 1
        failed_stage = failed_stage or "worker"
        try:
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(f"[{_now()}] WORKER_EXCEPTION {type(e).__name__}\n")
        except Exception:
            pass
    finally:
        try:
            if rc_final == 0:
                js.finish_job(run_dir, jid, "success")
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"[{_now()}] PIPELINE_COMPLETE\n")
            else:
                js.finish_job(run_dir, jid, "failed",
                              error=f"failed at: {failed_stage} (rc={rc_final})")
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"[{_now()}] PIPELINE_FAILED at {failed_stage} rc={rc_final}\n")
        except Exception:
            pass

    return 0 if rc_final == 0 else (rc_final if isinstance(rc_final, int) and rc_final != 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())
