@echo off
rem Double-click to install the Canvas Grader environment on Windows (runs install.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
