@echo off
setlocal
title VTeam Screen Recorder - uninstall
echo This removes VTeam Screen Recorder, its settings and its tools folder.
echo Your recordings are NOT touched.
echo.
choice /c YN /m "Uninstall now"
if errorlevel 2 exit /b 0
taskkill /im VTeamScreenRecorder.exe /f >nul 2>&1
rmdir /s /q "%LOCALAPPDATA%\Programs\VTeamScreenRecorder" 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "foreach ($dir in @([Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('Programs'))) {" ^
  "  Remove-Item -LiteralPath (Join-Path $dir 'VTeam Screen Recorder.lnk') -ErrorAction SilentlyContinue }"
echo Done. (FFmpeg installed with winget stays installed.)
pause
