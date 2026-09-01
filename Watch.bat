@echo off
REM One watch cycle, run by the DiecastWatch* scheduled tasks.
REM   scrape the given shops -> send any transitions -> answer bot commands
REM
REM   Watch.bat            marketplaces and specialty shops (every 5 min)
REM   Watch.bat blinkit    quick-commerce, browser-driven (hourly)
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"

REM Prefer whatever python is on PATH; fall back to the per-user install,
REM because Task Scheduler does not always inherit a full PATH.
if not defined PY set "PY=python"
where /q "%PY%" || set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"

if /i "%~1"=="blinkit" (
  "%PY%" cli.py scrape --only blinkit
) else (
  "%PY%" cli.py scrape --only firstcry blueballoon funcorp toymarche crossword
)

"%PY%" cli.py alerts

REM Answer any commands sent to the bot since the last cycle. Polling rather
REM than a webhook: nothing is listening between runs.
"%PY%" cli.py bot poll
