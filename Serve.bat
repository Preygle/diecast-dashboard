@echo off
REM Runs the dashboard with no browser popup - meant for autostart.
REM Port defaults to 8005; override with:  Serve.bat 9000
cd /d "%~dp0"
set "PORT=8005"
if not "%~1"=="" set "PORT=%~1"
set "PYTHONIOENCODING=utf-8"
"C:\Users\moham\AppData\Local\Programs\Python\Python313\python.exe" -m uvicorn hotwheels.server:app --host 127.0.0.1 --port %PORT% --log-level warning
