@echo off
REM ===================================================================
REM  MyNoteApp launcher  --  just double-click this file
REM
REM  Keep this file ASCII-only. Chinese text lives in
REM  scripts\launch.ps1, where the encoding is reliable.
REM ===================================================================
chcp 65001 >nul 2>&1
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1"
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo   Launcher exited with code %RC%
  pause
)
