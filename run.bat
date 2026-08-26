@echo off
cd /d "%~dp0"
python -m pip install -r requirements.txt --quiet
python -m pip install --quiet --upgrade yt-dlp
python player_app.py
pause
