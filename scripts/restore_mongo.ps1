# MongoDB 恢复脚本（mongorestore，先 drop 目标库再写入）
# 用法: .\scripts\restore_mongo.ps1 -Zip .\backups\mongo_20260906_120000.zip [-Database education_agent]
# 警告: 恢复会覆盖现有数据。执行前：
#   1. 停写（docker compose stop backend celery-worker）
#   2. 确认 -Database 目标库名（默认 education_agent，测试误恢复到 education_agent_test 无害）
param(
    [Parameter(Mandatory=$true)][string]$Zip,
    [string]$Database = "education_agent",
    [string]$Container = "education-agent-mongodb-1"
)
$ErrorActionPreference = "Stop"

if (-not (Test-Path $Zip)) { throw "备份文件不存在: $Zip" }

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$tmp = Join-Path $env:TEMP "mongo_restore_$stamp"
Write-Host "[1/3] 解包 $Zip"
Expand-Archive -Path $Zip -DestinationPath $tmp -Force

# 定位 dump 根目录（包含目标库子目录的一层）
$dumpRoot = (Get-ChildItem $tmp -Recurse -Directory -Filter $Database |
    Select-Object -First 1).Parent.FullName
if (-not $dumpRoot) { throw "备份中未找到库目录 $Database" }

Write-Host "[2/3] mongorestore --drop（$Database）"
docker cp $dumpRoot "${Container}:/tmp/restore_$stamp"
docker exec $Container mongorestore --drop --db $Database "/tmp/restore_$stamp/$Database"
if ($LASTEXITCODE -ne 0) { throw "mongorestore 失败" }

Write-Host "[3/3] 清理临时文件"
docker exec $Container rm -rf "/tmp/restore_$stamp"
Remove-Item -Recurse -Force $tmp
Write-Host "恢复完成: $Zip -> $Database（已先 drop 原库）"
Write-Host "提醒: 重新启动 backend / celery-worker（docker compose start backend celery-worker）"
