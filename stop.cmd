@echo off
taskkill /F /IM HpSebEscape.exe 2>nul
taskkill /F /IM AltA2dpConfig.exe 2>nul
taskkill /F /IM edge_feedback.exe 2>nul
taskkill /F /IM pythonw.exe 2>nul
echo All stopped.
