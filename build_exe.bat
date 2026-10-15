@echo off
REM ============================================================
REM  t3r build script - hardened PyInstaller edition
REM
REM  Why this is much less likely to be flagged by antivirus than a
REM  plain "pyinstaller --onefile" build:
REM   1. --onedir (NOT --onefile): a onefile exe is a self-extracting
REM      archive, the single biggest heuristic trigger for Defender.
REM   2. --noupx: UPX packing is heavily associated with malware.
REM   3. --version-file: unsigned exes without company/product metadata
REM      score higher on machine-learning detections (the ones whose
REM      names end in "!ml").
REM   4. --icon: a proper icon resource makes the exe look legitimate.
REM   5. Latest PyInstaller: old bootloaders are on many AV blocklists.
REM
REM  Output: dist\t3r\t3r.exe   (ship the WHOLE dist\t3r folder, zipped)
REM ============================================================
setlocal
cd /d "%~dp0"

REM Use "python" if present, otherwise fall back to the "py" launcher.
where python >nul 2>nul
if %errorlevel%==0 (set "PY=python") else (set "PY=py")

echo [1/4] Installing/upgrading build requirements...
%PY% -m pip install --upgrade pyinstaller pillow pystray
if errorlevel 1 goto :fail

echo [2/4] Generating icons...
%PY% make_icon.py
if errorlevel 1 goto :fail

echo [3/4] Building (onedir, no UPX, with version info)...
%PY% -m PyInstaller --noconfirm --clean ^
  --onedir ^
  --windowed ^
  --noupx ^
  --name t3r ^
  --icon icon.ico ^
  --version-file version.txt ^
  --exclude-module plyer ^
  t3r.py
if errorlevel 1 goto :fail

echo [4/4] Done.
echo.
echo   Output : %cd%\dist\t3r\t3r.exe
echo   Ship the ENTIRE dist\t3r folder (zip it first), not just the exe.
echo.
echo If Defender still flags it: see BUILDING.md, section
REM "Reporting the false positive to Microsoft" - one submission usually
REM clears it within a few days.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - read the messages above.
pause
exit /b 1
