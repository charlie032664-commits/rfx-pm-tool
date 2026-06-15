# PM Guide — Running the Pipeline (Background & Status)

A practical guide for PMs using the RFX PM Tool after v1.2/v1.3.

## Normal run vs background (beta) run

- **Run Full Pipeline** (normal/synchronous): runs Extract → Enrich → Format →
  Export in the page. **Don't refresh or navigate away** while it runs — a page
  reload can abandon it.
- **Run Full Pipeline in background (beta)**: starts a **detached worker** that
  keeps running even if you refresh or close the tab. Recommended for long runs.
  After clicking, you'll see "Pipeline started in background. You may refresh
  this page." Progress is shown in the **Pipeline job status (persistent)** panel.

## When to use "Enrich + Format + Export"

Use this (instead of Full Pipeline) when **`requirements.json` already exists**
and you only changed rules, owners, or post-processing — it **skips Extract**
(the slow, expensive LLM step). Re-running Extract unnecessarily wastes a lot of
time and LLM budget, especially on the internal model.

## What `job_status` means

`runs/<case>/job_status.json` is the source of truth for the latest run:

- `status`: `running`, `success`, `failed`, or `cancelled`.
- `stage`: `extract` → `enrich` → `format` → `export` → `done`.
- `provider` / `model`: which LLM produced this run.
- `started_at` / `ended_at`, `pid`, `log_path`.

The status panel reads this file, so it survives page refreshes.

## What to do if a "stale job" warning appears

A stale job means `status=running` but the process is no longer alive (e.g. a
synchronous run was interrupted by a refresh). It is safe:

- Your existing outputs are preserved (nothing is deleted).
- Simply start a new run — the status will be overwritten.
- Prefer the **background (beta)** button so it can't happen again.
- If a `.pipeline.lock` is also shown as stale, use the lock controls in the UI
  to clear it (it only clears locks older than 2 hours / from dead runs).

## Why large internal runs take a long time

The internal model (`qwen3-coder-next`) "reasons" before answering, so each LLM
call takes ~18–22 seconds. A big document can be thousands of chunks:

- Small case (e.g. AtlasRFQ): ~20 min on internal.
- Large case (e.g. SilverPeak ~3000+ chunks): **~17 hours** on internal vs ~30
  min on OpenAI.

Click **"Estimate runtime"** before launching. If it shows a **>4h strong
warning**, prefer the OpenAI provider, or run on the internal model overnight
with the background worker.

## Quick checklist

1. Confirm provider/model in the sidebar **LLM provider** panel (ready = true).
2. Click **Estimate runtime** for big cases; heed warnings.
3. For long runs, use **Run in background (beta)**.
4. Don't re-run Extract if `requirements.json` already exists — use
   **Enrich + Format + Export**.
5. Watch **Pipeline job status (persistent)** for stage/status.
