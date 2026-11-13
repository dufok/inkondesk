@echo off
rem Stop the XP-Pen key map and give the Kindle its normal UI back.
python "%~dp0xppen_kindle.py" --stop
timeout /t 3 >nul
