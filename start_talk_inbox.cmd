@echo off
setlocal
set "PROJECT_DIR=%~dp0"
set "PYTHONW=%PROJECT_DIR%.venv\Scripts\pythonw.exe"
set "APP=%PROJECT_DIR%talk_private_chat_window.py"

if not exist "%PYTHONW%" (
  echo Python environment not found: %PYTHONW%
  echo Run: uv sync
  pause
  exit /b 1
)

start "Kontur Talk private inbox" "%PYTHONW%" "%APP%" %*
endlocal
