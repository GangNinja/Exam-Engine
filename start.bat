@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Exam Engine Dashboard

rem Allow a caller to choose the interpreter, e.g.
rem   set EXAM_ENGINE_PYTHON=C:\Python313\python.exe
set "PYTHON=%EXAM_ENGINE_PYTHON%"
if not defined PYTHON (
    where py >nul 2>nul
    if errorlevel 1 (
        set "PYTHON=python"
    ) else (
        set "PYTHON=py -3"
    )
)

rem Work from a source checkout even when the package has not been installed.
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"

%PYTHON% -c "import exam_engine" >nul 2>nul
if errorlevel 1 (
    echo.
    echo ERROR: Python could not import the exam_engine package.
    echo Install the project with:
    echo     py -3 -m pip install -e .
    echo Or set EXAM_ENGINE_PYTHON to a Python executable that can import it.
    echo.
    pause
    exit /b 1
)

rem Prefer the documented port, but move to the next free port when it is busy.
rem If every preferred port is occupied, --port 0 lets the OS assign one.
set "PORT="
for %%P in (8765 8766 8767 8768 8769 8770 8771 8772 8773 8774 8775) do (
    if not defined PORT (
        %PYTHON% -c "import socket; s=socket.socket(); s.bind(('127.0.0.1',%%P)); s.close()" >nul 2>nul
        if not errorlevel 1 set "PORT=%%P"
    )
)

if not defined PORT set "PORT=0"

set "URL="
if not "%PORT%"=="0" set "URL=http://127.0.0.1:%PORT%/"

if defined URL (
    echo Dashboard: %URL%
) else (
    echo All preferred ports are busy; the operating system will choose a free port.
    echo The actual URL will be printed below once the dashboard starts.
)

echo Database:  %CD%\data\exam_engine.db
echo Press Ctrl+C to stop.
echo.

rem Poll the URL before opening a browser, so a slow database start does not
rem produce a connection-refused tab. Set EXAM_ENGINE_NO_BROWSER=1 to disable.
if defined URL if /I not "%EXAM_ENGINE_NO_BROWSER%"=="1" (
    start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$u='%URL%'; for($i=0; $i -lt 40; $i++){ try { Invoke-WebRequest -UseBasicParsing -Uri $u -TimeoutSec 1 | Out-Null; Start-Process $u; break } catch { Start-Sleep -Milliseconds 500 } }"
)

%PYTHON% -m exam_engine dashboard --host 127.0.0.1 --port %PORT%
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
    echo.
    echo Dashboard exited with code %RC%.
    echo If another program took the selected port, run start.bat again.
    echo.
    pause
)

endlocal & exit /b %RC%
