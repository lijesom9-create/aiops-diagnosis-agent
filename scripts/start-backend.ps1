# 启动 education-agent 后端服务
# 使用说明: 在 PowerShell 中运行 .\start-backend.ps1

$ErrorActionPreference = "Stop"

# 切换到脚本所在目录（backend 目录）
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

# 设置 NO_PROXY，避免 Windows 系统代理影响 DeepSeek API 连接
$env:NO_PROXY = "api.deepseek.com"

# 启动服务
Write-Host "正在启动 education-agent 后端服务..." -ForegroundColor Green
Write-Host "访问地址: http://localhost:8000" -ForegroundColor Cyan
Write-Host "API 文档: http://localhost:8000/docs" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Yellow

.\venv\Scripts\python.exe main.py
