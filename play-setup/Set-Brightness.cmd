@echo off
rem Brightness switch: Set-Brightness New or Old (double-click to be asked). Switch only while the game is closed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-Brightness.ps1" %1
if "%~1"=="" pause
