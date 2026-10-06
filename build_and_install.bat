@echo off
setlocal EnableDelayedExpansion
title Raspberry Shake Viewer — Build & Install

echo.
echo ==========================================
echo  Raspberry Shake Viewer  ^|  Build ^& Install
echo ==========================================
echo.

REM ── 1. Locate the correct Python ─────────────────────────────────────────
REM
REM  We use the Windows Python Launcher (py.exe) to find the real Python 3
REM  installation. This avoids accidentally picking up Inkscape's bundled
REM  python.exe (or any other app that ships its own Python) which would
REM  not have our packages installed.
REM

set "PYTHON="

REM Try the py launcher first (most reliable on Windows)
py --version >nul 2>&1
if not errorlevel 1 (
    for /f "delims=" %%i in ('py -3 -c "import sys; print(sys.executable)"') do set "PYTHON=%%i"
)

REM Fallback: try python3, then python — but verify it's not Inkscape's copy
if not defined PYTHON (
    for /f "delims=" %%i in ('where python3 2^>nul') do (
        set "CANDIDATE=%%i"
        echo !CANDIDATE! | findstr /i "inkscape" >nul
        if errorlevel 1 (
            set "PYTHON=!CANDIDATE!"
            goto :found_python
        )
    )
)
if not defined PYTHON (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        set "CANDIDATE=%%i"
        echo !CANDIDATE! | findstr /i "inkscape" >nul
        if errorlevel 1 (
            set "PYTHON=!CANDIDATE!"
            goto :found_python
        )
    )
)

:found_python
if not defined PYTHON (
    echo [ERROR] Could not find a suitable Python 3 installation.
    echo         Please install Python 3.9+ from https://www.python.org
    echo         Make sure to tick "Add Python to PATH" during install.
    pause
    exit /b 1
)

echo [OK] Using Python: %PYTHON%
"%PYTHON%" --version

REM ── 2. Install / upgrade dependencies ────────────────────────────────────
echo.
echo Installing dependencies (this may take a minute)...
"%PYTHON%" -m pip install --upgrade --quiet pip
"%PYTHON%" -m pip install --quiet PyQt6 pyqtgraph numpy paramiko pyinstaller

if errorlevel 1 (
    echo [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo [OK] Dependencies installed.

REM ── 3. Build the executable ───────────────────────────────────────────────
echo.
echo Building executable with PyInstaller...

set "SCRIPT_DIR=%~dp0"
set "VIEWER=%SCRIPT_DIR%raspberryshake_viewer.py"

if not exist "%VIEWER%" (
    echo [ERROR] raspberryshake_viewer.py not found in %SCRIPT_DIR%
    echo         Make sure build_and_install.bat and raspberryshake_viewer.py are in the same folder.
    pause
    exit /b 1
)

set "BUILD_DIR=%SCRIPT_DIR%_build"
set "DIST_DIR=%SCRIPT_DIR%_dist"
set "ICON=%SCRIPT_DIR%raspberryshake_icon.ico"

REM Check for icon file (optional — build still works without it)
if not exist "%ICON%" (
    echo [WARN] raspberryshake_icon.ico not found — building without custom icon.
    set "ICON_FLAG="
) else (
    echo [OK] Icon found: %ICON%
    set "ICON_FLAG=--icon "%ICON%""
)

"%PYTHON%" -m PyInstaller ^
    --noconfirm ^
    --onefile ^
    --windowed ^
    --name "RaspberryShakeViewer" ^
    %ICON_FLAG% ^
    --distpath "%DIST_DIR%" ^
    --workpath "%BUILD_DIR%" ^
    --specpath "%BUILD_DIR%" ^
    "%VIEWER%"

if errorlevel 1 (
    echo [ERROR] PyInstaller build failed. See output above for details.
    pause
    exit /b 1
)

set "EXE=%DIST_DIR%\RaspberryShakeViewer.exe"
if not exist "%EXE%" (
    echo [ERROR] Build finished but .exe not found at %EXE%
    pause
    exit /b 1
)
echo [OK] Executable built successfully.

REM ── 4. Place shortcut on the Desktop ─────────────────────────────────────
echo.
echo Creating Desktop shortcut...

set "DESKTOP=%USERPROFILE%\Desktop"
set "SHORTCUT=%DESKTOP%\Raspberry Shake Viewer.lnk"

powershell -NoProfile -Command ^
  "$ws = New-Object -ComObject WScript.Shell; " ^
  "$s  = $ws.CreateShortcut('%SHORTCUT%'); " ^
  "$s.TargetPath   = '%EXE%'; " ^
  "$s.WorkingDirectory = '%DIST_DIR%'; " ^
  "$s.IconLocation = '%EXE%,0'; " ^
  "$s.Description  = 'Raspberry Shake Live Waveform Viewer'; " ^
  "$s.Save()"

if errorlevel 1 (
    echo [WARN] Could not create shortcut automatically.
    echo        You can find the executable here:
    echo        %EXE%
) else (
    echo [OK] Shortcut placed on Desktop.
)

REM ── 5. Cleanup ────────────────────────────────────────────────────────────
echo.
echo Cleaning up build files...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
echo [OK] Done.

REM ── 6. Done ───────────────────────────────────────────────────────────────
echo.
echo ==========================================
echo  All done!
echo  Double-click "Raspberry Shake Viewer"
echo  on your Desktop to launch the app.
echo ==========================================
echo.
pause
endlocal
