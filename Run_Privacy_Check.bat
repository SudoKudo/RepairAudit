@echo off
setlocal
cd /d %~dp0

if exist venv\Scripts\python.exe (
  venv\Scripts\python.exe -m scripts.study_cli privacy-check
) else (
  python -m scripts.study_cli privacy-check
)

echo.
echo Privacy check complete. Press any key to close.
pause >nul
