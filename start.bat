@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Shorts Maker
echo.
echo  ==========================================
echo    Shorts Maker - private, free, on this PC
echo  ==========================================
echo.

rem ---- 1. find a good Python (3.10 - 3.13 works best with the AI packages) ----
set "PY="
for %%V in (3.12 3.11 3.13 3.10) do (
  if not defined PY (
    py -%%V --version >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo  Python was not found on this PC. Trying to get Python 3.12 automatically...
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  py -3.12 --version >nul 2>&1 && set "PY=py -3.12"
)
if not defined PY (
  echo  Could not find or install Python automatically.
  echo  Open https://www.python.org/downloads/ , install Python 3.12, tick "Add to PATH", then run this again.
  pause
  exit /b 1
)
echo  Using: %PY%

rem ---- 2. private virtual environment ----
if not exist ".venv\Scripts\python.exe" (
  echo  Creating private environment...
  %PY% -m venv .venv
  if errorlevel 1 ( echo  Could not create the environment. & pause & exit /b 1 )
)

rem ---- 3. packages (first run only) ----
if not exist ".venv\.installed" (
  echo  Installing packages, first time only, a few minutes...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo.
    echo  Package install failed. Trying again with Python 3.12 from winget...
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    rmdir /s /q .venv
    py -3.12 -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 ( echo  Still failing. Take a photo of this window and send it to Claude. & pause & exit /b 1 )
  )
  echo ok> ".venv\.installed"
)

rem ---- 4. NVIDIA GPU speed-up libraries (only if you have an NVIDIA card) ----
if not exist ".venv\.gpu" (
  where nvidia-smi >nul 2>&1
  if not errorlevel 1 (
    echo  NVIDIA graphics card found, adding GPU speed-up for speech recognition...
    ".venv\Scripts\python.exe" -m pip install nvidia-cublas-cu12 "nvidia-cudnn-cu12==9.*"
  )
  echo ok> ".venv\.gpu"
)

rem ---- 5. FFmpeg (first run only) ----
if not exist "bin\ffmpeg.exe" (
  echo  Downloading FFmpeg, first time only...
  powershell -NoProfile -ExecutionPolicy Bypass -File "tools\get_ffmpeg.ps1"
  if not exist "bin\ffmpeg.exe" (
    echo  FFmpeg download failed. Check your internet and run this again.
    pause
    exit /b 1
  )
)

rem ---- 6. go ----
echo.
echo  Starting... your browser will open. Keep this window open while you use the site.
echo  (Close this window to stop it.)
echo.
".venv\Scripts\python.exe" app.py
echo.
echo  Shorts Maker stopped.
pause
