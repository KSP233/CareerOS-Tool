@echo off
setlocal
cd /d "%~dp0"
title CareerOS Debug
"%~dp0runtime\python.exe" "%~dp0main.py"
pause
