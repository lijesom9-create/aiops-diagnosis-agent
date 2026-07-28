# 启动 education-agent 前端服务
# 使用说明: 在 PowerShell 中运行 .\start-frontend.ps1

$ErrorActionPreference = "Stop"

# 切换到 frontend 目录
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$frontendDir = Join-Path $scriptDir "frontend"
Set-Location $frontendDir

Write-Host "正在启动 education-agent 前端服务..." -ForegroundColor Green
Write-Host "访问地址: http://localhost:3000" -ForegroundColor Cyan
Write-Host "按 Ctrl+C 停止服务" -ForegroundColor Yellow

npm run dev
