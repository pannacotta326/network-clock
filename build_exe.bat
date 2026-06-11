@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m venv .venv
call .venv\Scripts\activate.bat
pip install -r requirements.txt
pyinstaller --noconfirm --onefile --windowed --name "NetworkClock" clock_app.py
echo.
echo 完了: dist\NetworkClock.exe
pause
