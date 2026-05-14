@echo off
chcp 65001 >nul
setlocal

for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8501 ^| findstr LISTENING') do (
    echo Stopping PID %%a ...
    taskkill /PID %%a /F
)

echo.
echo Done.
pause
endlocal