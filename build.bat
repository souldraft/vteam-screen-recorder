@echo off
setlocal EnableExtensions
title VTeam Screen Recorder - build and install
cd /d "%~dp0"

echo ============================================================
echo   VTeam Screen Recorder - one-click build + install
echo   (double-click this file; it takes 2-5 minutes the first time)
echo ============================================================
echo.

rem  Normal build: opens without the Windows "allow changes" (UAC) prompt.
rem  Want it to always run as Administrator?  Run:  build.bat admin
set "ADMINFLAG="
if /i "%~1"=="admin" set "ADMINFLAG=--uac-admin"

rem ---------- 1. Find Python 3.10+ (installs Python 3.12 with winget if missing) ----------
set "PY="
for %%V in (3.12 3.13 3.11 3.14 3.10) do call :trypy %%V
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [1/6] Python not found. Installing Python 3.12 with winget...
  winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
)
if not defined PY (
  echo.
  echo Could not find or install Python. Install it from https://www.python.org/downloads/
  echo ^(tick "Add python.exe to PATH"^), then double-click build.bat again.
  goto :fail
)
echo [1/6] Using Python: %PY%

rem ---------- 2. Private environment for the build ----------
if not exist ".venv\Scripts\python.exe" (
  echo [2/6] Creating a private Python environment...
  %PY% -m venv .venv || goto :fail
) else (
  echo [2/6] Python environment already exists.
)
set "VPY=.venv\Scripts\python.exe"

rem ---------- 3. Packages ----------
echo [3/6] Installing packages: customtkinter, pillow, soundcard, segno, onnxruntime, pyinstaller...
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install --upgrade "customtkinter>=5.2,<7" "pillow>=10" "soundcard>=0.4.4" "segno>=1.6" "pyinstaller>=6.10" --quiet --disable-pip-version-check || goto :fail
rem  onnxruntime runs the camera's Cutout (background removal). Optional: if it fails, the build still works.
"%VPY%" -m pip install --upgrade "onnxruntime>=1.17" --quiet --disable-pip-version-check || echo     Note: onnxruntime did not install, so the camera Cutout style is left out. Everything else works.

rem ---------- 4. Self-test ----------
echo [4/6] Running the self-test...
"%VPY%" VTeamScreenRecorder.py --selftest || goto :fail

rem ---------- 5. Build the .exe ----------
rem  Optional: put the official GitHub mark as github.png in this folder to use it on the GitHub button.
set "GITHUBICON="
if exist "github.png" set "GITHUBICON=--add-data "github.png;.""
set "CUTOUT="
"%VPY%" -c "import onnxruntime" >nul 2>&1 && if exist selfie_segmentation.onnx set "CUTOUT=--collect-binaries onnxruntime --add-data selfie_segmentation.onnx;. --add-data selfie_segmentation.LICENSE.txt;."
echo.
echo [5/6] Building VTeamScreenRecorder.exe (about 1 minute)...
"%VPY%" -m PyInstaller --noconfirm --clean --log-level WARN --onefile --windowed %ADMINFLAG% ^
  --name VTeamScreenRecorder --icon icon.ico --add-data "icon.ico;." --collect-data customtkinter ^
  --hidden-import PIL._tkinter_finder --hidden-import PIL._imagingtk --hidden-import PIL.ImageTk ^
  --hidden-import soundcard --collect-data soundcard --hidden-import segno ^
  %CUTOUT% %GITHUBICON% VTeamScreenRecorder.py || goto :fail
if not exist "dist\VTeamScreenRecorder.exe" goto :fail

rem ---------- 6. Install + shortcuts ----------
set "DEST=%LOCALAPPDATA%\Programs\VTeamScreenRecorder"
echo [6/6] Installing to %DEST% ...
if not exist "%DEST%" mkdir "%DEST%"
copy /y "dist\VTeamScreenRecorder.exe" "%DEST%\VTeamScreenRecorder.exe" >nul
if errorlevel 1 (
  echo.
  echo Could not copy the app. If VTeam Screen Recorder is open, close it and run build.bat again.
  goto :fail
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$exe = Join-Path $env:LOCALAPPDATA 'Programs\VTeamScreenRecorder\VTeamScreenRecorder.exe';" ^
  "$sh = New-Object -ComObject WScript.Shell;" ^
  "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  $lnk = $sh.CreateShortcut((Join-Path $dir 'VTeam Screen Recorder.lnk'));" ^
  "  $lnk.TargetPath = $exe; $lnk.WorkingDirectory = (Split-Path $exe);" ^
  "  $lnk.IconLocation = $exe + ',0'; $lnk.Description = 'Record your screen with sound, mic and camera'; $lnk.Save() }"

echo.
echo ============================================================
echo   DONE!  "VTeam Screen Recorder" is on your Desktop and in the Start menu.
echo   App folder: %DEST%
if defined ADMINFLAG echo   Admin build: Windows asks "allow changes?" each time it opens - click Yes.
echo ============================================================
echo.
start "" "%DEST%\VTeamScreenRecorder.exe"
pause
exit /b 0

:trypy
if defined PY goto :eof
py -%1 -c "import sys" >nul 2>&1 && set "PY=py -%1"
goto :eof

:fail
echo.
echo ************************************************************
echo   Something went wrong - read the message above.
echo   Tip: open Claude Code in this folder and paste the prompt
echo   from CLAUDE_CODE_PROMPT.md; it can fix and build it for you.
echo ************************************************************
pause
exit /b 1
