@echo off
REM Stop the Command Center server.
cd /d "%~dp0"
python -c "import tracker; tracker._stop_server_on(8790)"
if errorlevel 1 py -c "import tracker; tracker._stop_server_on(8790)"
echo Stopped.
