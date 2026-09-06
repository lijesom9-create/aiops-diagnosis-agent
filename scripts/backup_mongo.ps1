# MongoDB 备份脚本（mongodump 经 docker exec，dump 后打包为 zip）
# 用法: .\scripts\backup_mongo.ps1 [-Keep 7] [-OutDir .\backups]
# 策略建议见 docs/DEPLOYMENT.md「数据备份与恢复」：每日定时执行，
# 本地保留最近 N 份 + 异地（对象存储/另一台机器）至少一份。
param(
    [int]$Keep = 7,
    [string]$OutDir = ".\backups",
    [string]$Container = "education-agent-mongodb-1"  # docker compose 默认容器名，docker ps 可查
)
$ErrorActionPreference = "Stop"

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "[1/3] mongodump（容器内 /tmp/dump_$stamp）"
docker exec $Container mongodump --out "/tmp/dump_$stamp"
if ($LASTEXITCODE -ne 0) { throw "mongodump 失败" }

Write-Host "[2/3] 拷出并打包"
$out = Join-Path $OutDir "mongo_$stamp"
docker cp "${Container}:/tmp/dump_$stamp" $out
docker exec $Container rm -rf "/tmp/dump_$stamp"
Compress-Archive -Path (Join-Path $out "*") -DestinationPath "$out.zip" -Force
Remove-Item -Recurse -Force $out
Write-Host "备份完成: $out.zip"

Write-Host "[3/3] 保留策略：仅保留最近 $Keep 份"
Get-ChildItem $OutDir -Filter "mongo_*.zip" | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip $Keep | ForEach-Object {
        Remove-Item $_.FullName -Force
        Write-Host "  清理过期备份: $($_.Name)"
    }
