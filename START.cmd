@echo off
setlocal
cd /d "%~dp0"
title CareerOS
if not exist "%~dp0runtime\python.exe" (
  echo CareerOS Python runtime is missing.
  echo Please restore the runtime folder or reinstall CareerOS.
  pause
  exit /b 1
)
start "CareerOS" "%~dp0runtime\pythonw.exe" "%~dp0main.py"
