@echo off
rem Starts the whole AI receptionist on this computer: double-click this file.
rem Three windows open (rag-core 8000, voice-memory 8001, kiosk-ui 8002) and then the kiosk in your browser.
rem To stop everything: close those three windows (or press Ctrl+C in each).

cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment .venv is missing. Do the one-time setup in RUN.md first.
  pause
  exit /b 1
)

start "rag-core 8000" /D "%~dp0rag-core" cmd /k "set HF_HUB_OFFLINE=1&& ..\.venv\Scripts\python.exe -m rag_core.api"
start "voice-memory 8001" /D "%~dp0voice-memory" cmd /k "..\.venv\Scripts\python.exe -m voice_memory.api"
start "kiosk-ui 8002" /D "%~dp0kiosk-ui" cmd /k "..\.venv\Scripts\python.exe -m kiosk_ui"

echo.
echo Starting... rag-core takes about a minute to load its models.
echo The kiosk opens in your browser in 60 seconds. If it says Offline, wait a little and refresh.
echo.
echo   Kiosk        http://127.0.0.1:8002/
echo   Analytics    http://127.0.0.1:8002/dashboard
echo   Admin        http://127.0.0.1:8002/admin
echo   Phone page   http://127.0.0.1:8002/go
echo   QR stickers  http://127.0.0.1:8002/qr
echo.
timeout /t 60
start "" "http://127.0.0.1:8002/"
