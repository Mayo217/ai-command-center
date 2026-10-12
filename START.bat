@echo off
REM Start the Command Center server and open it in the browser.
cd /d "%~dp0"
python tracker.py open
if errorlevel 1 py tracker.py open
