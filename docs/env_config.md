# Local LLM Configuration (.env)

The app reads LLM settings from environment variables. To avoid having to export
them in every shell, you can put them in a local **`.env`** file at the repo
root. It is loaded automatically at startup by `scripts/env_loader.py`.

> `.env` is **gitignored** — never commit real secrets or internal URLs.
> Only `.env.example` (placeholders) is tracked.

## Setup

1. Copy the template:
   ```
   copy .env.example .env      # Windows
   cp   .env.example .env      # macOS/Linux
   ```
2. Fill in the values for your provider (see below).
3. **Restart Streamlit** so the new values are picked up. The pipeline runs as
   subprocesses that inherit the Streamlit process environment, so the parent
   must be (re)started after editing `.env`.

## Required variables

### Internal / self-hosted (OpenAI-compatible endpoint)
```
LLM_PROVIDER=internal
INTERNAL_LLM_BASE_URL=https://<your-internal-host>/v1
INTERNAL_LLM_API_KEY=<key>
INTERNAL_LLM_MODEL=<model-name>
```

### OpenAI
```
LLM_PROVIDER=openai
OPENAI_API_KEY=<key>
OPENAI_MODEL=gpt-4.1-mini      # optional; this is the default
```

Optional for either provider: `LLM_TIMEOUT_SECONDS` (default 60; the internal
launcher uses 120).

## Precedence & compatibility

- **OS environment always wins.** A variable already exported (e.g. via `setx`
  or a launcher `.bat`) is never overwritten by `.env`. Existing setups keep
  working unchanged.
- A missing `.env` is a **no-op** — the app behaves exactly as before.
- The launchers (`run_rfx.bat`, `run_rfx_internal.bat`) are unchanged and still
  work; `.env` is simply an additional, optional source.

## Verify config (no secrets printed)

```
python scripts/env_loader.py
```
Prints the provider, whether each required variable is **present** (never its
value), and a final `ready` flag. Use this to confirm the config is complete
before launching the Full Pipeline.
