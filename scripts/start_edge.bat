@echo off
REM Start Edge (or Chrome if Edge is missing) with a separate profile and a CDP port.
REM Then log in to zhihuishu manually in the opened window.
REM Optional: set ZHS_CDP_PORT to use a port other than 9222 (also change cdp_url in config.yaml).
setlocal
if "%ZHS_CDP_PORT%"=="" (set "PORT=9222") else (set "PORT=%ZHS_CDP_PORT%")
set "BROWSER=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"
if not exist "%BROWSER%" set "BROWSER=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"
if not exist "%BROWSER%" set "BROWSER=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not exist "%BROWSER%" set "BROWSER=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not exist "%BROWSER%" (
  echo Neither Edge nor Chrome was found. Edit BROWSER path in this file.
  pause
  exit /b 1
)
start "" "%BROWSER%" --remote-debugging-port=%PORT% --user-data-dir="%USERPROFILE%\zhs_edge_profile" https://onlineweb.zhihuishu.com/
endlocal
