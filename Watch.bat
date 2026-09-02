@echo off
REM One watch cycle, run by the DiecastWatch* scheduled tasks.
REM   scrape -> send any transitions -> answer bot commands
REM
REM   Watch.bat            fast tier: watched brands at the shops that stock
REM                        them near MRP. ~35s, so it can run every 2 minutes.
REM   Watch.bat full       every enabled shop, every query. ~2min, every 20.
REM   Watch.bat blinkit    quick-commerce, browser-driven. Hourly.
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

REM Prefer whatever python is on PATH; fall back to the per-user install,
REM because Task Scheduler does not always inherit a full PATH.
if not defined PY set "PY=python"
where /q "%PY%" || set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if /i "%~1"=="blinkit" (
  "%PY%" cli.py scrape --only blinkit
) else if /i "%~1"=="full" (
  "%PY%" cli.py scrape
) else (
  "%PY%" cli.py scrape --fast
)

"%PY%" cli.py alerts

REM Answer any commands sent to the bot since the last cycle. Polling rather
REM than a webhook: nothing is listening between runs.
"%PY%" cli.py bot poll
