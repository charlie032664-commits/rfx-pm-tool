@echo off
chcp 65001 >nul
setlocal

:: ============================================
:: RFX PM Tool - Internal / self-hosted LLM launcher
:: ============================================
:: Use this launcher ONLY for an OpenAI-compatible internal endpoint
:: (vLLM / Ollama / TGI / etc.). It FORCES LLM_PROVIDER=internal and then
:: fails fast if the internal config is incomplete - so Streamlit never starts
:: in a broken state that later errors with "LLM not configured".
::
:: NOTE: this launcher checks the PROCESS environment only (Windows env / setx
:: or values uncommented below). It does NOT read .env. If you configure the
:: internal endpoint via a local .env file, use run_rfx.bat instead - it does
:: not force a provider and lets .env / OS env decide. OS env takes precedence
:: over .env.
::
:: Usage:
::   run_rfx_internal.bat            launch (after the config check passes)
::   run_rfx_internal.bat --check    run the config check only; do NOT launch
:: ============================================

:: Optional preflight-only mode (validate config, then exit without launching).
set "CHECK_ONLY="
if /i "%~1"=="--check" set "CHECK_ONLY=1"

:: --- Force internal provider (this launcher's purpose) ---
set "LLM_PROVIDER=internal"

:: --- Internal endpoint + model come from your Windows env (setx) ---
::     This launcher deliberately does NOT set blank values (that previously
::     shadowed working OS env / .env config). To hardcode here instead of
::     using Windows env, uncomment and fill these two lines:
::       set "INTERNAL_LLM_BASE_URL=https://your-internal-host/api/v1"
::       set "INTERNAL_LLM_MODEL=qwen3-next-80b"
::     INTERNAL_LLM_API_KEY should be set once via: setx INTERNAL_LLM_API_KEY ...

:: --- Timeout (seconds): default only if not already provided ---
if not defined LLM_TIMEOUT_SECONDS set "LLM_TIMEOUT_SECONDS=120"

:: --- Fail fast: require a complete internal config before launching ---
set "MISSING="
if not defined INTERNAL_LLM_BASE_URL set "MISSING=%MISSING% INTERNAL_LLM_BASE_URL"
if not defined INTERNAL_LLM_API_KEY  set "MISSING=%MISSING% INTERNAL_LLM_API_KEY"
if not defined INTERNAL_LLM_MODEL    set "MISSING=%MISSING% INTERNAL_LLM_MODEL"

if defined MISSING (
    echo.
    echo [ERROR] Internal LLM config is incomplete.
    echo Missing:
    for %%V in (%MISSING%) do echo    - %%V
    echo.
    echo run_rfx_internal.bat is for internal LLM only and forces LLM_PROVIDER=internal.
    echo If you want normal .env / OpenAI mode, please run:
    echo     run_rfx.bat
    echo.
    echo To use internal mode, configure the required variables in Windows env or local .env:
    echo     INTERNAL_LLM_BASE_URL
    echo     INTERNAL_LLM_API_KEY
    echo     INTERNAL_LLM_MODEL
    echo.
    exit /b 1
)

:: --- Show config (no secrets: base URL and API key values are NOT printed) ---
echo.
echo ==========================================
echo LLM_PROVIDER          = internal
echo INTERNAL_LLM_BASE_URL = (set)
echo INTERNAL_LLM_API_KEY  = (set)
echo INTERNAL_LLM_MODEL    = %INTERNAL_LLM_MODEL%
echo LLM_TIMEOUT_SECONDS   = %LLM_TIMEOUT_SECONDS%
echo ==========================================
echo.

if defined CHECK_ONLY (
    echo [OK] Internal LLM config looks complete. ^(--check: not starting Streamlit^)
    exit /b 0
)

:: --- Launch (config is complete) ---
call "%~dp0run_rfx.bat"
