@echo off
setlocal
chcp 65001 >nul
title Установка YapAnel для Станции Макс
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_to_max.ps1" %*
set ERR=%ERRORLEVEL%
if %ERR% neq 0 (
    echo.
    echo ----------------------------------------------------------------------
    echo  [ОШИБКА] Установка завершилась с ошибкой. См. сообщение выше.
    echo ----------------------------------------------------------------------
    pause
)
exit /b %ERR%
