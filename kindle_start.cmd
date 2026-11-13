@echo off
rem Start the XP-Pen key map on the Kindle in the background (no window).
rem A second copy is harmless: it notices the first one and exits.
rem Runs from %TEMP% so the program doesn't keep this folder busy (it can be renamed/moved).
start "" /D "%TEMP%" pythonw "%~dp0xppen_kindle.py"
