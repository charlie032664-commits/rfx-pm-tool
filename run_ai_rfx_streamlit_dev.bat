@echo off
chcp 65001 >nul
setlocal

set PYTHON_EXE=C:\Users\Charlie.Hsieh\PycharmProjects\DashBoard\Collect-data\.venv\Scripts\python.exe
set APP_DIR=C:\Users\Charlie.Hsieh\PycharmProjects\DashBoard\Collect-data\scripts\ai_rfx_streamlit_dev
set APP_FILE=app.py
set PORT=8501

echo ==========================================
echo AI RFX Streamlit Dev
echo Python : %PYTHON_EXE%
echo AppDir : %APP_DIR%
echo App    : %APP_FILE%
echo Port   : %PORT%
echo ==========================================
echo.

cd /d "%APP_DIR%"

"%PYTHON_EXE%" -m streamlit run "%APP_FILE%" --server.address 0.0.0.0 --server.port %PORT%

echo.
echo Streamlit stopped.
pause
endlocal