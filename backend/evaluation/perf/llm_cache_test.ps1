$ErrorActionPreference = "Stop"
$loginBody = @{username="qpstest"; password="qps123456"} | ConvertTo-Json
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/auth/login" -Method POST -Body $loginBody -ContentType "application/json"
$token = $resp.access_token
$headers = @{Authorization = "Bearer $token"}
Write-Host "Token acquired"

$q1 = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Default.GetBytes("如何备份MySQL数据库？"))
$body = @{message=$q1} | ConvertTo-Json

Write-Host ""
Write-Host "=== Test 1: First query (no cache) ==="
$t1 = Measure-Command { $r1 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("First (no cache): {0:N2}s" -f $t1.TotalSeconds)

Write-Host ""
Write-Host "=== Test 2: Same query (LLM cache hit) ==="
$t2 = Measure-Command { $r2 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("LLM cache hit: {0:N2}s" -f $t2.TotalSeconds)

Write-Host ""
Write-Host "=== Test 3: Same query again (3rd time) ==="
$t3 = Measure-Command { $r3 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("3rd (cache hit): {0:N2}s" -f $t3.TotalSeconds)

Write-Host ""
Write-Host "=== Test 4: Different query (no LLM cache) ==="
$q2 = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Default.GetBytes("Redis持久化有哪几种方式？"))
$body2 = @{message=$q2} | ConvertTo-Json
$t4 = Measure-Command { $r4 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body2 -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("Diff query (no cache): {0:N2}s" -f $t4.TotalSeconds)

Write-Host ""
Write-Host "=== Summary ==="
Write-Host ("First: {0:N2}s | LLM cache: {1:N2}s | Speedup: {2:N1}x | 3rd: {3:N2}s | Diff: {4:N2}s" -f $t1.TotalSeconds, $t2.TotalSeconds, ($t1.TotalSeconds/$t2.TotalSeconds), $t3.TotalSeconds, $t4.TotalSeconds)
