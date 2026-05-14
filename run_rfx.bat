@echo off
chcp 65001 >nul
setlocal

set "PYTHON_EXE=C:\Users\Charlie.Hsieh\PycharmProjects\DashBoard\Collect-data\.venv\Scripts\python.exe"
set "APP_DIR=C:\Users\Charlie.Hsieh\PycharmProjects\DashBoard\Collect-data\scripts\ai_rfx_streamlit_dev"
set "APP_FILE=app.py"
set "PORT=8501"
set "URL=http://localhost:%PORT%"

echo ==========================================
echo AI RFX PM Tool (integrated)
echo Python : %PYTHON_EXE%
echo AppDir : %APP_DIR%
echo App    : %APP_FILE%
echo Port   : %PORT%
echo URL    : %URL%
echo ==========================================
echo.

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found:
    echo %PYTHON_EXE%
    pause
    exit /b 1
)

if not exist "%APP_DIR%\%APP_FILE%" (
    echo [ERROR] App file not found:
    echo %APP_DIR%\%APP_FILE%
    pause
    exit /b 1
)

cd /d "%APP_DIR%"

"%PYTHON_EXE%" -m streamlit run "%APP_FILE%" --server.address 0.0.0.0 --server.port %PORT%

echo.
echo Streamlit stopped.
pause
endlocal