@echo off
setlocal
cd /d "%~dp0"

REM =====================================================================
REM  Builds a fully standalone "PG Restore" folder: no Python and no
REM  PostgreSQL installation is needed on the PC that runs it.
REM
REM  Before running, this folder must contain:
REM    pg_restore_gui.py
REM    assets\            icon + logo files (already included)
REM    pgsql\bin\         psql.exe, pg_restore.exe and ALL .dll files from a
REM                       PostgreSQL "bin" folder (you provide these)
REM    python\            OPTIONAL portable Python with tkinter and pip.
REM                       If missing, the Python installed on this PC is used.
REM =====================================================================

echo.
echo [1/5] Looking for Python...
set "PY="
if exist "python\python.exe" (
    set "PY=python\python.exe"
    echo       Using the Python in the python folder.
) else (
    where python >nul 2>nul
    if not errorlevel 1 (
        set "PY=python"
        echo       Using the Python installed on this PC.
    )
)
if "%PY%"=="" (
    echo.
    echo  ERROR: No Python found.
    echo  Install Python from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
    echo  or put a portable Python in a folder named python next to this file.
    pause
    exit /b 1
)
"%PY%" --version

"%PY%" -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo.
    echo  ERROR: This Python has no tkinter.
    echo  Use the normal python.org installer. The "embeddable zip" does not include tkinter.
    pause
    exit /b 1
)

echo.
echo [2/5] Checking files...
if not exist "pg_restore_gui.py" (
    echo  ERROR: pg_restore_gui.py is missing from this folder.
    pause
    exit /b 1
)
if not exist "assets\app.ico" (
    echo  ERROR: assets\app.ico is missing. Keep the assets folder next to this file.
    pause
    exit /b 1
)
if not exist "pgsql\bin\psql.exe" (
    echo.
    echo  ERROR: pgsql\bin\psql.exe is missing.
    echo  Copy psql.exe, pg_restore.exe and all .dll files from a PostgreSQL bin folder into pgsql\bin
    pause
    exit /b 1
)
if not exist "pgsql\bin\pg_restore.exe" (
    echo.
    echo  ERROR: pgsql\bin\pg_restore.exe is missing.
    echo  Copy psql.exe, pg_restore.exe and all .dll files from a PostgreSQL bin folder into pgsql\bin
    pause
    exit /b 1
)
echo       OK

echo.
echo [3/5] Installing PyInstaller...
"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 (
    echo.
    echo  ERROR: Could not install PyInstaller. Check the internet connection and that this Python has pip.
    pause
    exit /b 1
)

echo.
echo [4/5] Building the app...
"%PY%" -m PyInstaller --noconfirm --clean --onedir --windowed --name "PG Restore" --icon "assets\app.ico" --add-data "assets;assets" pg_restore_gui.py
if not exist "dist\PG Restore\PG Restore.exe" (
    echo.
    echo  ERROR: Build failed. Scroll up to see the error and send it to Claude.
    pause
    exit /b 1
)

echo.
echo [5/5] Adding the PostgreSQL tools and making the zip...
robocopy "pgsql\bin" "dist\PG Restore\pgsql\bin" *.dll psql.exe pg_restore.exe /NFL /NDL /NJH /NJS /NP >nul
if exist "pgsql\share" robocopy "pgsql\share" "dist\PG Restore\pgsql\share" /E /NFL /NDL /NJH /NJS /NP >nul
if exist "PG Restore.zip" del "PG Restore.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\PG Restore' -DestinationPath 'PG Restore.zip' -Force"

echo.
echo =====================================================================
echo  Done!
echo    Folder: dist\PG Restore\     (run "PG Restore.exe" inside it)
echo    Zip:    PG Restore.zip       (copy and unzip anywhere)
echo  No Python or PostgreSQL install is needed on the target PC.
echo =====================================================================
pause
