@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
set "P1=%~1"
set "P2=%~2"
if "%P1%"=="" set "P1=502"
if "%P2%"=="" set "P2=8080"

net session >nul 2>&1
if errorlevel 1 (
    echo 需要管理员权限，正在申请...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%P1%','%P2%' -Verb RunAs"
    exit /b 0
)

for %%P in (%P1% %P2%) do (
    netsh advfirewall firewall delete rule name="ModbusMonitor TCP %%P" >nul 2>&1
    netsh advfirewall firewall add rule name="ModbusMonitor TCP %%P" dir=in action=allow protocol=TCP localport=%%P >nul
    if errorlevel 1 (
        echo 放行 TCP %%P 失败
    ) else (
        echo 已放行入站 TCP %%P
    )
)
echo.
echo 完成。如果修改过端口，请把端口号作为参数传入：
echo   开放防火墙端口.bat 502 8080
pause
