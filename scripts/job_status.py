# -*- coding: utf-8 -*-
"""Persistent pipeline job status — v1.2 robustness scaffold.

Writes a single source of truth for "what pipeline run is / was happening" for a
case, so the state survives Streamlit reruns and is visible to both the UI and
CLI. Complements (does not replace) the existing `.pipeline.lock` and
`run_history.jsonl`.

Files (under runs/<case>/):
  job_status.json   current / last job (one object — see SCHEMA below)
  jobs.jsonl        append-only history (one job snapshot per terminal write)

This module is pure helper code: it does NOT launch processes, call the LLM, or
modify the existing pipeline scripts. Wiring into app.py / the CLI is a later,
separate change (see docs/v1.2_pipeline_robustness_design.md).

Usage:
    from job_status import start_job, set_stage, finish_job, read_job_status
    job = start_job(run_dir, case_id="X", provider="internal",
                    model="qwen3-coder-next", log_path=str(log), pid=1234)
    set_stage(run_dir, job["job_id"], "enrich")
    finish_job(run_dir, job["job_id"], "success")
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

STAGES: List[str] = ["extract", "enrich", "format", "export", "done"]
STATUSES: List[str] = ["queued", "running", "success", "failed", "cancelled"]
TERMINAL_STATUSES = {"success", "failed", "cancelled"}

STATUS_FILENAME = "job_status.json"
HISTORY_FILENAME = "jobs.jsonl"

# Env var name fragments whose values must never appear in a persisted message.
_SECRET_HINT = ("KEY", "TOKEN", "SECRET", "PASSWORD")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_job_id() -> str:
    """Random 12-hex job id (stable for one pipeline run)."""
    return uuid.uuid4().hex[:12]


def mask_secrets(text: Optional[str]) -> str:
    """Redact any secret-looking env var VALUE that appears in text.

    Defensive: never let an API key leak into job_status.json via an error
    message. Looks at the current process env for values of vars whose name
    hints they are secret, and replaces them with ***.
    """
    s = "" if text is None else str(text)
    if not s:
        return s
    for name, val in os.environ.items():
        if val and len(val) >= 6 and any(h in name.upper() for h in _SECRET_HINT):
            s = s.replace(val, "***")
    return s


def detect_provider_model() -> Dict[str, str]:
    """Best-effort (provider, model) from env_loader/llm_client. Never raises.

    Returns {"provider": ..., "model": ...} with empty strings if unavailable.
    Imports are local so this module stays import-light and never fails to load.
    """
    provider, model = "", ""
    try:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from env_loader import load_env, describe_llm_config  # type: ignore
        load_env()
        cfg = describe_llm_config()
        provider = str(cfg.get("provider", "") or "")
    except Exception:
        pass
    try:
        import llm_client  # type: ignore
        model = str(llm_client.get_model() or "")
    except Exception:
        pass
    return {"provider": provider, "model": model}


def _status_path(run_dir: Path) -> Path:
    return Path(run_dir) / STATUS_FILENAME


def _history_path(run_dir: Path) -> Path:
    return Path(run_dir) / HISTORY_FILENAME


def _write(run_dir: Path, job: Dict[str, Any], *, append_history: bool = False) -> None:
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    job["updated_at"] = _now()
    _status_path(run_dir).write_text(
        json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if append_history:
        with _history_path(run_dir).open("a", encoding="utf-8") as f:
            f.write(json.dumps(job, ensure_ascii=False) + "\n")


def start_job(
    run_dir: Path,
    *,
    case_id: str,
    provider: str = "",
    model: str = "",
    log_path: str = "",
    pid: Optional[int] = None,
    stage: str = "extract",
) -> Dict[str, Any]:
    """Create a new running job, persist it, append a history line."""
    job = {
        "job_id": new_job_id(),
        "case_id": case_id,
        "provider": provider,
        "model": model,
        "stage": stage if stage in STAGES else "extract",
        "status": "running",
        "started_at": _now(),
        "ended_at": None,
        "pid": pid,
        "log_path": log_path,
        "error": None,
    }
    _write(run_dir, job, append_history=True)
    return job


def _load(run_dir: Path) -> Optional[Dict[str, Any]]:
    p = _status_path(run_dir)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def set_stage(run_dir: Path, job_id: str, stage: str) -> Optional[Dict[str, Any]]:
    """Advance the current job's stage. No-op if job_id doesn't match."""
    job = _load(run_dir)
    if not job or job.get("job_id") != job_id:
        return None
    if stage in STAGES:
        job["stage"] = stage
    _write(run_dir, job)
    return job


def finish_job(
    run_dir: Path,
    job_id: str,
    status: str,
    error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Write a terminal status (success/failed/cancelled). error is masked."""
    job = _load(run_dir)
    if not job or job.get("job_id") != job_id:
        return None
    job["status"] = status if status in STATUSES else "failed"
    if job["status"] in TERMINAL_STATUSES:
        job["ended_at"] = _now()
        if job["status"] == "success":
            job["stage"] = "done"
    job["error"] = mask_secrets(error) if error else None
    _write(run_dir, job, append_history=True)
    return job


def read_job_status(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read the current/last job for a case, or None if none recorded."""
    return _load(run_dir)


# ---------------------------------------------------------------------------
# Self-test (no LLM, no network; uses a system temp dir, never touches runs/)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import tempfile
    import shutil

    tmp = Path(tempfile.mkdtemp(prefix="jobstatus_test_"))
    try:
        assert read_job_status(tmp) is None, "empty dir -> None"

        job = start_job(tmp, case_id="X", provider="internal",
                        model="qwen3-coder-next", log_path="x/_pipeline.log", pid=4321)
        assert job["status"] == "running" and job["stage"] == "extract"
        assert _status_path(tmp).exists() and _history_path(tmp).exists()
        jid = job["job_id"]
        assert len(jid) == 12

        u = set_stage(tmp, jid, "enrich")
        assert u and u["stage"] == "enrich"
        assert set_stage(tmp, "wrong-id", "format") is None, "mismatched id -> no-op"

        # Secret masking: a fake secret in env must not survive into error.
        os.environ["FAKE_API_KEY"] = "supersecretvalue123"
        done = finish_job(tmp, jid, "failed", error="boom token=supersecretvalue123")
        assert done["status"] == "failed" and done["ended_at"]
        assert "supersecretvalue123" not in json.dumps(done), "secret leaked!"
        assert "***" in done["error"]
        del os.environ["FAKE_API_KEY"]

        # History captured start + finish (2 lines).
        hist = _history_path(tmp).read_text(encoding="utf-8").strip().splitlines()
        assert len(hist) == 2, hist

        # success path sets stage=done.
        job2 = start_job(tmp, case_id="X", provider="openai", model="gpt-4.1-mini")
        ok = finish_job(tmp, job2["job_id"], "success")
        assert ok["stage"] == "done" and ok["status"] == "success"

        print("[OK] job_status self-test passed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
