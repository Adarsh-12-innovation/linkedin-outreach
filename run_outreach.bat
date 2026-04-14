@echo off
:: 1. Navigate to your project directory (Essential for loading .env)
cd /d "C:\Users\adars\Documents\linkedin-saved-items"

echo [%date% %time%] --- RUNNING LINKEDIN OUTREACH AGENTS ---

:: 2. Run the Keyword Search script
echo [%date% %time%] Executing Search Outreach...
".venv\Scripts\python.exe" search_outreach.py
if %ERRORLEVEL% NEQ 0 (
    echo [%date% %time%] ERROR: Search Outreach failed with code %ERRORLEVEL%
    exit /b %ERRORLEVEL%
)

echo [%date% %time%] --- ALL TASKS COMPLETED SUCCESSFULLY ---
exit /b 0