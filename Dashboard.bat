@echo off
setlocal EnableDelayedExpansion
title Hot Wheels Price Dashboard

REM Always run from the folder this file lives in, so double-clicking works
REM regardless of where Explorer thinks the working directory is.
cd /d "%~dp0"

set "PORT=8005"
if not "%~1"=="" set "PORT=%~1"

REM ---- locate Python -------------------------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo   Python was not found on your PATH.
  echo   Install it from https://python.org and tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

REM ---- warn if nothing has been scraped yet --------------------------------
if not exist "hotwheels.db" (
  echo.
  echo   No hotwheels.db yet - the dashboard will open empty.
  echo   Close this window and run:  %PY% cli.py scrape
  echo.
)

REM ---- open the browser once the port is actually accepting connections ----
REM Launching the browser immediately would race the server and show an error
REM page, so poll the socket first. Timeout after ~30s so this never hangs.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command ^
  "$p=%PORT%; for($i=0;$i -lt 60;$i++){ try{ $c=New-Object Net.Sockets.TcpClient; $c.Connect('127.0.0.1',$p); $c.Close(); Start-Process ('http://127.0.0.1:'+$p+'/'); break } catch { Start-Sleep -Milliseconds 500 } }"

echo.
echo   Hot Wheels dashboard  ^|  http://127.0.0.1:%PORT%/
echo   Close this window or press Ctrl+C to stop the server.
echo.

%PY% cli.py serve --port %PORT%

REM Only reached if the server exits on its own (e.g. port already in use).
if errorlevel 1 (
  echo.
  echo   The server stopped unexpectedly. If port %PORT% is already in use,
  echo   start it on another one:   Dashboard.bat 8001
  echo.
  pause
)
endlocal
