# Current Task

How to use this file:
- Charlie pastes a long task/prompt into the **TASK** section below, saves it,
  then tells Claude Code: **"Read docs/tasks/current_task.md and execute it."**
- This avoids pasting long text directly into the terminal.
- When a task is finished, move a copy of it into `docs/tasks/archive/`
  (e.g. `docs/tasks/archive/2026-06-17_<short-name>.md`) and clear the TASK
  section below for the next one.

## Standing rules (always apply unless the TASK explicitly overrides)
1. Follow the task instructions **exactly**.
2. Do **not** modify code unless the task explicitly says to.
3. Do **not** push, merge, tag, or create a release unless explicitly approved.
4. Do **not** run the LLM or the pipeline unless explicitly approved.
5. Do **not** run SilverPeak or any long full pipeline unless explicitly approved.
6. Do **not** modify `.env`; never print secrets.
7. Do **not** commit `runs/`, `inbound/`, or `.claude/`.
8. Prefer small, additive changes; keep commits small and logical.
9. If a step becomes risky or unclear, stop, report findings, and ask before
   proceeding.

## TASK
<!-- Charlie: paste the task below this line. Replace everything in this section. -->

(no task pasted yet)

## Notes / scratch
<!-- Optional: branch name, constraints, expected output format, etc. -->
