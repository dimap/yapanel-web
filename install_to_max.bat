@echo off
setlocal
chcp 65001 >nul
title Установка YapAnel для Станции Макс

if not exist "%~dp0install_to_max.ps1" (
    echo [ИНФО] Загрузка компонентов установщика YapAnel с GitHub...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12; (New-Object Net.WebClient).DownloadFile('https://raw.githubusercontent.com/Timobzdecc/yapanel-web_max/main/install_to_max.ps1', '%~dp0install_to_max.ps1')"
)

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
