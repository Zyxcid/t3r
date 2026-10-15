@echo off
REM ============================================================
REM  t3r build script - Nuitka ONEFILE edition (single t3r.exe)
REM
REM  Produces ONE standalone exe you can upload straight to
REM  GitHub Releases: users download t3r.exe and run it - no
REM  folder, no _internal directory, no installer.
REM
REM  Why Nuitka onefile and not "pyinstaller --onefile":
REM   * Nuitka compiles the Python to real C code. There is no
REM     generic PyInstaller bootloader (the #1 signature AVs
REM     train on) and no UPX packing, so false positives are
REM     rare - not impossible though: see BUILDING.md section 5.
REM   * The exe self-extracts on launch, but into a CACHED folder
REM     ({CACHE_DIR}), so only the very first start is slow.
REM     After that it starts like a normal app.
REM
REM  Notes:
REM   * The first BUILD downloads a C compiler (MinGW) and can
REM     take 5-15 minutes. Later builds are faster.
REM   * Requires Windows 10+ / Python 3.8+.
REM
REM  Output: dist_onefile\t3r.exe  (this single file IS the release)
REM ============================================================
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (set "PY=python") else (set "PY=py")

echo [1/3] Installing/upgrading build requirements...
%PY% -m pip install --upgrade nuitka pillow pystray
if errorlevel 1 goto :fail

echo [2/3] Generating icons...
%PY% make_icon.py
if errorlevel 1 goto :fail

echo [3/3] Compiling to a single exe (first run: 5-15 minutes)...
%PY% -m nuitka ^
  --onefile ^
  --onefile-tempdir-spec="{CACHE_DIR}/{PRODUCT}{VERSION}" ^
  --enable-plugin=tk-inter ^
  --windows-console-mode=disable ^
  --windows-icon-from-ico=icon.ico ^
  --company-name="t3r project" ^
  --product-name="t3r Eye Break Timer" ^
  --file-version=1.3.0.0 ^
  --product-version=1.3.0.0 ^
  --file-description="t3r - 20-20-20 rule eye break timer" ^
  --copyright="MIT License. Copyright (c) 2026 t3r project" ^
  --output-dir=dist_onefile ^
  --output-filename=t3r.exe ^
  --remove-output ^
  --assume-yes-for-downloads ^
  t3r.py
if errorlevel 1 goto :fail

echo.
echo Done.
echo   Output : %cd%\dist_onefile\t3r.exe
echo   This ONE file is your release - upload it directly to
echo   GitHub Releases. Nothing else is needed.
echo   Note: the very first launch on a PC extracts itself once
echo   (a few extra seconds); every later start is instant.
echo.
echo If Defender ever flags it: see BUILDING.md section 5
echo ("Reporting the false positive to Microsoft").
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - read the messages above.
pause
exit /b 1
