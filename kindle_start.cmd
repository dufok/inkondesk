@echo off
rem Start the XP-Pen key map on the Kindle in the background (no window).
rem A second copy is harmless: it notices the first one and exits.
start "" pythonw "%~dp0xppen_kindle.py"
