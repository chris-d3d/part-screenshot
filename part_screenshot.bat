@echo off
rem Save an isometric image of a Teamcenter part revision, fully loaded in NX.
rem
rem   part-screenshot.bat --part 123456/A --folder C:\screenshots
rem
rem All options are passed to part_screenshot.py (run with --help to list them).
rem UGII_BASE_DIR and DAPS_PYTHON may be set beforehand to use another NX install
rem or Python; part_screenshot.py sets the Teamcenter and NX journal variables.

setlocal

if not defined UGII_BASE_DIR set "UGII_BASE_DIR=C:\CAD\NX2406"
if not defined DAPS_PYTHON set "DAPS_PYTHON=C:\CAD\DAPS_Plugin\python.exe"

if not exist "%UGII_BASE_DIR%\NXBIN\run_journal.exe" (
    echo error: NX not found at %UGII_BASE_DIR%. Set UGII_BASE_DIR to the NX install folder. 1>&2
    exit /b 2
)
if not exist "%DAPS_PYTHON%" (
    echo error: Python not found at %DAPS_PYTHON%. Set DAPS_PYTHON to the DAPS embedded python.exe. 1>&2
    exit /b 2
)

"%DAPS_PYTHON%" "%~dp0part_screenshot.py" %*
exit /b %ERRORLEVEL%
