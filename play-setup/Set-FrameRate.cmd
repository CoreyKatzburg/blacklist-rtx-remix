@echo off
rem Frame-rate switch: Set-FrameRate Today, A, B or D (double-click to be asked). Switch only while the game is closed.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Set-FrameRate.ps1" %1
if "%~1"=="" pause
