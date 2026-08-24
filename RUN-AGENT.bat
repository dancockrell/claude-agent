@echo off
setlocal enabledelayedexpansion
title Claude agent runner  --  create a file named STOP here to end it
cd /d "%~dp0"
if not exist jobs mkdir jobs
if not exist results mkdir results
if not exist done mkdir done
set PYTHONIOENCODING=utf-8
set LOG=boot.log

set PYEXE=
call :try "%USERPROFILE%\anaconda3\python.exe"
call :try "%USERPROFILE%\miniconda3\python.exe"
call :try "%ProgramData%\anaconda3\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
call :try "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
for /f "delims=" %%I in ('where python.exe 2^>nul') do call :try "%%I"
for /f "delims=" %%I in ('where py.exe 2^>nul')     do call :try "%%I"

if not defined PYEXE (
  echo NO WORKING PYTHON FOUND >> %LOG%
  echo   No working Python found - see boot.log
  pause
  exit /b 1
)

echo.
echo   Claude agent runner
echo   -------------------
echo   Using: !PYEXE!
echo   This window restarts itself if the runner stops.
echo   To end it for good: put a file named STOP in this folder.
echo.

:loop
echo =============================================>> %LOG%
echo runner start %DATE% %TIME%                   >> %LOG%
echo =============================================>> %LOG%
"!PYEXE!" -u agent_runner.py >> %LOG% 2>&1
if exist STOP goto done
echo. >> %LOG%
echo runner exited %ERRORLEVEL% - restarting in 5s >> %LOG%
echo   runner stopped, restarting in 5 seconds  (make a STOP file to end it)
timeout /t 5 /nobreak >nul
goto loop

:done
echo   STOP file found. Finished.
del STOP >nul 2>&1
pause >nul
exit /b

:try
if defined PYEXE exit /b
if not exist %1 exit /b
%1 -c "import sys" >nul 2>&1
if errorlevel 1 exit /b
set PYEXE=%~1
exit /b
