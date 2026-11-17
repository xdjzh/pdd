@echo off
setlocal EnableDelayedExpansion
title PDD Review Helper - Build EXE
rem This file MUST be saved as GBK/ANSI (cp936), CRLF, NO BOM, NO chcp.
rem Never add a BOM or a UTF-8 code page switch: cmd.exe would split Chinese lines.

echo ============================================================
echo   PDD Review Helper - Windows EXE Builder
echo   Source folder: %~dp0
echo   This ZIP has no EXE. The EXE is built here, on this PC.
echo   Build once only - later changes go through hot update.
echo ============================================================
echo.

set "SRC=%~dp0"
set "ROOT=%LOCALAPPDATA%\PDD_Build"
if not defined LOCALAPPDATA set "ROOT=%TEMP%\PDD_Build"

echo [PREP] Copy sources to an ASCII-only folder to avoid path issues ...
echo         Build folder: %ROOT%
if not exist "%ROOT%" mkdir "%ROOT%" 2>nul
robocopy "%SRC%." "%ROOT%" /E /NFL /NDL /NJH /NJS /NP /XD dist build runtime pdd_data __pycache__ >nul
if errorlevel 8 goto ERR_COPY
cd /d "%ROOT%"
if errorlevel 1 goto ERR_COPY

set "MISSING="
for %%F in (requirements_gui.txt run_gui.py app.ico version_info.txt gui\__init__.py gui\state.py gui\adapter.py gui\engine.py gui\app.py gui\theme.py gui\license.py gui\login_window.py updater.py scripts\report_reviews.py) do (
  if not exist "%ROOT%\%%F" set "MISSING=!MISSING! %%F"
)
if defined MISSING goto ERR_MISSING
echo [0/4] File check passed (14 items)
echo.

where python >nul 2>&1
if not errorlevel 1 goto HAVE_PY
where py >nul 2>&1
if errorlevel 1 goto NO_PYTHON
set "PY=py -3"
goto CHECK_VER

:HAVE_PY
set "PY=python"

:CHECK_VER
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)" >nul 2>&1
if errorlevel 1 goto OLD_PYTHON
echo       Python version:
%PY% -V
echo.

echo [1/4] Install/check dependencies (playwright + pyinstaller), 1-3 min ...
%PY% -m pip install --disable-pip-version-check --no-warn-script-location "playwright>=1.40" "pyinstaller>=6.0"
if not errorlevel 1 goto PIP_OK
echo        Default index failed, retrying with Tsinghua mirror ...
%PY% -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple --disable-pip-version-check --no-warn-script-location "playwright>=1.40" "pyinstaller>=6.0"
if not errorlevel 1 goto PIP_OK
echo        Retrying with --user ...
%PY% -m pip install --user --disable-pip-version-check --no-warn-script-location "playwright>=1.40" "pyinstaller>=6.0"
if errorlevel 1 goto PIP_FAIL

:PIP_OK
%PY% -c "import playwright, PyInstaller" >nul 2>&1
if errorlevel 1 goto PIP_FAIL
echo        Dependencies OK
echo.

set "CFG_ARG="
if exist "%ROOT%\cloud_config.json" set CFG_ARG=--add-data "%ROOT%\cloud_config.json;."
if defined CFG_ARG (
echo        Auth server address is embedded, end users need no setup.
) else (
echo       [NOTE] cloud_config.json missing, users must set server URL manually.
)
echo.

echo [2/4] Browser engine
echo        Just press Enter to use system Edge / Chrome (smaller EXE).
echo        Choose Y only for PCs without Edge / Chrome.
set "EMBED=N"
set /p "EMBED=Embed Chromium engine? [y/N]: "
if /i "%EMBED%"=="Y" goto EMBED_YES
goto BUILD_LIGHT

:EMBED_YES
echo        Downloading Chromium ...
if not exist "%ROOT%\runtime" mkdir "%ROOT%\runtime" 2>nul
set "PLAYWRIGHT_BROWSERS_PATH=%ROOT%\runtime\ms-playwright"
%PY% -m playwright install chromium
if errorlevel 1 goto ERR_BROWSER
echo.
echo [3/4] Building EXE (full, with Chromium) ...
echo        This takes 3-10 minutes. Do NOT close this window ...
if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build" >nul 2>&1
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PDD_Review_Helper ^
  --icon "%ROOT%\app.ico" ^
  --version-file "%ROOT%\version_info.txt" ^
  --distpath "%ROOT%\dist" --workpath "%ROOT%\build" --specpath "%ROOT%" ^
  --collect-all playwright ^
  --collect-all pyee ^
  --collect-all greenlet ^
  --hidden-import=gui.license ^
  --hidden-import=gui.login_window ^
  --hidden-import=updater ^
  --add-data "%ROOT%\scripts;scripts" ^
  --add-data "%ROOT%\runtime\ms-playwright;ms-playwright" ^
  %CFG_ARG% ^
  "%ROOT%\run_gui.py"
if errorlevel 1 goto ERR_BUILD
goto BUILD_DONE

:BUILD_LIGHT
echo.
echo [3/4] Building EXE (light, uses system Edge / Chrome) ...
echo        This takes 3-10 minutes. Do NOT close this window ...
if exist "%ROOT%\build" rmdir /s /q "%ROOT%\build" >nul 2>&1
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name PDD_Review_Helper ^
  --icon "%ROOT%\app.ico" ^
  --version-file "%ROOT%\version_info.txt" ^
  --distpath "%ROOT%\dist" --workpath "%ROOT%\build" --specpath "%ROOT%" ^
  --collect-all playwright ^
  --collect-all pyee ^
  --collect-all greenlet ^
  --hidden-import=gui.license ^
  --hidden-import=gui.login_window ^
  --hidden-import=updater ^
  --add-data "%ROOT%\scripts;scripts" ^
  %CFG_ARG% ^
  "%ROOT%\run_gui.py"
if errorlevel 1 goto ERR_BUILD

:BUILD_DONE
if not exist "%ROOT%\dist\PDD_Review_Helper.exe" goto ERR_NO_EXE
echo.
echo [4/4] Done!
echo ============================================================
echo   EXE: %ROOT%\dist\PDD_Review_Helper.exe
echo.
echo   Copying EXE to source folder and Desktop ...
if not exist "%SRC%dist" mkdir "%SRC%dist" 2>nul
copy /y "%ROOT%\dist\PDD_Review_Helper.exe" "%SRC%dist\PDD_Review_Helper.exe" >nul
copy /y "%ROOT%\dist\PDD_Review_Helper.exe" "%USERPROFILE%\Desktop\PDD_Review_Helper.exe" >nul
if exist "%USERPROFILE%\Desktop\PDD_Review_Helper.exe" (
echo   Copied to Desktop: %USERPROFILE%\Desktop\PDD_Review_Helper.exe
) else (
echo   Desktop copy failed, please take it from dist manually.
)
echo.
echo   Send ONLY PDD_Review_Helper.exe to your users.
echo   They need no Python, no source code, no this bat.
echo.
echo   Opening dist folder ...
start "" "%SRC%dist" >nul 2>&1
goto END

:ERR_COPY
echo [ERROR E01] Failed to copy sources to: %ROOT%
goto FAIL

:ERR_MISSING
echo [ERROR E02] Required files are missing:
echo   Missing:!MISSING!
echo   Extract the WHOLE zip, not just build_exe.bat.
goto FAIL

:NO_PYTHON
echo [ERROR E03] Python not found. Install Python 3.9 or newer,
echo             and CHECK "Add python.exe to PATH", then run this bat again.
echo             Download: https://www.python.org/downloads/
goto FAIL

:OLD_PYTHON
echo [ERROR E04] Python is older than 3.9, please upgrade.
goto FAIL

:PIP_FAIL
echo [ERROR E05] Dependency install failed, most likely a network issue.
echo             Manual: python -m pip install playwright pyinstaller
echo             China mirror: -i https://pypi.tuna.tsinghua.edu.cn/simple
goto FAIL

:ERR_BROWSER
echo [ERROR E06] Chromium download failed. Re-run and press N to use system Edge.
goto FAIL

:ERR_BUILD
echo [ERROR E07] PyInstaller failed. Screenshot the Traceback / red errors above.
echo             Build folder: %ROOT%
echo             Usual causes: antivirus, low disk space, too-new Python.
goto FAIL

:ERR_NO_EXE
echo [ERROR E08] Build finished but no EXE found, check PyInstaller output above.
goto FAIL

:FAIL
echo.
echo [FAILED] Troubleshooting:
echo   1. If you see many is-not-recognized lines, you are using the OLD bat. Overwrite it.
echo   2. Extract the WHOLE zip, not just build_exe.bat.
echo   3. Need Python 3.9+ with Add python.exe to PATH checked.
echo   4. Antivirus may block PyInstaller; disable it temporarily.
echo   5. Send a full screenshot including the [ERROR E0x] code above.
goto END

:END
echo.
pause
endlocal
