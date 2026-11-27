@echo off
echo ============================================
echo  SEB Kit v2 - One-Click Setup
echo ============================================
echo.

:: Find Python
set PYPATH=
for %%P in ("F:\Python\python.exe" "C:\Python312\python.exe" "C:\Python311\python.exe" "C:\Python313\python.exe") do (
    if exist %%P set PYPATH=%%~P
)
if "%PYPATH%"=="" (
    where python >nul 2>&1
    if %errorlevel% equ 0 (
        set PYPATH=python
    ) else (
        echo [*] Installing Python...
        winget install Python.Python.3.12 --accept-package-agreements --accept-source-agreements -s winget 2>nul
        set PYPATH=python
    )
)
echo [+] Python: %PYPATH%

:: Install agy CLI
echo [*] Checking agy CLI...
where agy >nul 2>&1 && echo [+] agy already installed || (
    echo [*] Install agy with: powershell -Command "irm https://antigravity.google/cli/install.ps1 | iex"
    echo [*] Then run 'agy' once to login
)

:: Compile HpSebEscape
echo [*] Compiling HpSebEscape...
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /optimize /target:exe /platform:x64 /out:"%~dp0HpSebEscape.exe" "%~dp0HpSebEscape.cs" 2>nul
if exist "%~dp0HpSebEscape.exe" (echo [+] HpSebEscape compiled) else (echo [!] HpSebEscape compile failed)

:: Compile AltA2dpConfig
echo [*] Compiling AltA2dpConfig...
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /optimize /target:winexe /out:"%~dp0AltA2dpConfig.exe" "%~dp0AltA2dpConfig.cs" 2>nul
if exist "%~dp0AltA2dpConfig.exe" (echo [+] AltA2dpConfig compiled) else (echo [!] AltA2dpConfig compile failed)

:: Deploy to A2DP path (if installed + we have write access)
if exist "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe" (
    echo [*] A2DP driver found - deploying...
    if not exist "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe.real_backup" (
        copy /Y "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe" "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe.real_backup" >nul 2>&1
    )
    copy /Y "%~dp0AltA2dpConfig.exe" "C:\Program Files\Luculent Systems\AltA2DP\AltA2dpConfig.exe" >nul 2>&1
    if %errorlevel% equ 0 (
        echo [+] Deployed to A2DP path (SEB-safe)
    ) else (
        echo [!] No admin - will run from user folder (use Ctrl+Space to access)
    )
) else (
    echo [*] A2DP driver not installed - will run from user folder
    echo [*] Use Ctrl+Space to switch to Default desktop, then open AltA2dpConfig.exe
)

:: Copy agy as edge_feedback.exe
echo [*] Setting up AI backend...
set AGYPATH=
if exist "%LOCALAPPDATA%\agy\bin\agy.exe" set AGYPATH=%LOCALAPPDATA%\agy\bin\agy.exe
if "%AGYPATH%"=="" for /f "tokens=*" %%i in ('where agy.exe 2^>nul') do set AGYPATH=%%i
if not "%AGYPATH%"=="" (
    copy /Y "%AGYPATH%" "%~dp0edge_feedback.exe" >nul
    echo [+] AI backend ready
) else (
    echo [!] agy.exe not found - install agy first, then run setup again
)

echo.
echo [+] SETUP COMPLETE!
echo     Run: run.cmd
pause
