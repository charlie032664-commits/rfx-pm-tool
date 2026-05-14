@echo off
chcp 65001 >nul

:: ============================================
:: RFX PM Tool — Internal LLM mode
:: ============================================
:: INTERNAL_LLM_API_KEY is read from Windows env vars (already set).
:: Change the MODEL line below to switch models.
:: ============================================

:: --- Provider ---
set "LLM_PROVIDER=internal"

:: --- Base URL (change to your internal endpoint) ---
set "INTERNAL_LLM_BASE_URL=https://172.17.20.220/api/v1"

:: --- Model (uncomment ONE line) ---
:: General review / RFQ extraction (default after Qwen3-32B retirement)
set "INTERNAL_LLM_MODEL=qwen3-next-80b"
:: Code generation / code fix (still in service):
:: set "INTERNAL_LLM_MODEL=qwen3-coder-next"
:: --- Retired ---
:: set "INTERNAL_LLM_MODEL=nvidia/Qwen3-32B-NVFP4"
:: set "INTERNAL_LLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8"
:: set "INTERNAL_LLM_MODEL=nvidia/Gemma-4-31B-IT-NVFP4"

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
