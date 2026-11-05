# 打包单文件 exe：powershell -ExecutionPolicy Bypass -File tools\build.ps1
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$venv = Join-Path $root ".venv"
if (-not (Test-Path $venv)) {
    Write-Host "创建虚拟环境 .venv ..."
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3.13 -m venv $venv
    } else {
        python -m venv $venv
    }
}
$py = Join-Path $venv "Scripts\python.exe"

Write-Host "安装打包依赖 ..."
& $py -m pip install --upgrade pip pyinstaller pillow

if (-not $SkipTests) {
    Write-Host "运行测试 ..."
    & $py -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "测试未通过，已停止打包"
    }
}

Write-Host "打包 ..."
& $py -m PyInstaller --noconfirm --clean --onefile --console `
    --icon "assets\app.ico" `
    --version-file "tools\version_info.txt" `
    --name "ModbusTcpMonitor" `
    --add-data "modbus_proxy\static;modbus_proxy\static" `
    app.py

Write-Host ""
Write-Host "构建完成：dist\ModbusTcpMonitor.exe"
