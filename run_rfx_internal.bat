@echo off
chcp 65001 >nul

:: ============================================
:: RFX PM Tool — Internal / self-hosted LLM mode
:: ============================================
:: Use this launcher when your LLM provider is an OpenAI-compatible
:: internal endpoint (e.g. a vLLM / Ollama / TGI server) instead of OpenAI.
:: INTERNAL_LLM_API_KEY is read from Windows env vars (set it once with
:: setx, this script reuses it).
:: ============================================

:: --- Provider ---
set "LLM_PROVIDER=internal"

:: --- Base URL — set this to your internal LLM endpoint, e.g.
::     set "INTERNAL_LLM_BASE_URL=https://your-internal-host/api/v1"
set "INTERNAL_LLM_BASE_URL="

:: --- Model — name your internal endpoint serves (e.g. qwen3-next-80b) ---
set "INTERNAL_LLM_MODEL="

:: --- Timeout (seconds) ---
set "LLM_TIMEOUT_SECONDS=120"

:: --- Show config ---
echo.
echo ==========================================
echo LLM_PROVIDER        = %LLM_PROVIDER%
echo INTERNAL_LLM_BASE_URL = %INTERNAL_LLM_BASE_URL%
echo INTERNAL_LLM_MODEL  = %INTERNAL_LLM_MODEL%
echo LLM_TIMEOUT_SECONDS = %LLM_TIMEOUT_SECONDS%
echo INTERNAL_LLM_API_KEY = (set in Windows env)
echo ==========================================
echo.

:: --- Launch ---
call "%~dp0run_rfx.bat"
