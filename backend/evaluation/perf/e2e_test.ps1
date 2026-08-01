$ErrorActionPreference = "Stop"
$loginBody = @{username="perftest"; password="perf123456"} | ConvertTo-Json
$resp = Invoke-RestMethod -Uri "http://localhost:8000/api/auth/login" -Method POST -Body $loginBody -ContentType "application/json"
$token = $resp.access_token
$headers = @{Authorization = "Bearer $token"}
Write-Host "Token acquired"

$q1 = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Default.GetBytes("如何备份MySQL数据库？"))
$body1 = @{message=$q1} | ConvertTo-Json

Write-Host ""
Write-Host "=== Test 1: First query ==="
$t1 = Measure-Command { $r1 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body1 -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("First e2e: {0:N2}s" -f $t1.TotalSeconds)
$sid = $r1.session_id
Write-Host ("session_id: " + $sid)

Write-Host ""
Write-Host "=== Test 2: Same query again (retrieval cache hit) ==="
$body2 = @{message=$q1} | ConvertTo-Json
$t2 = Measure-Command { $r2 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body2 -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("Cached e2e: {0:N2}s" -f $t2.TotalSeconds)

Write-Host ""
Write-Host "=== Test 3: Different query (first retrieval) ==="
$q3 = [System.Text.Encoding]::UTF8.GetString([System.Text.Encoding]::Default.GetBytes("API网关的作用是什么？"))
$body3 = @{message=$q3} | ConvertTo-Json
$t3 = Measure-Command { $r3 = Invoke-RestMethod -Uri "http://localhost:8000/api/langgraph/chat" -Method POST -Body $body3 -ContentType "application/json; charset=utf-8" -Headers $headers -TimeoutSec 120 }
Write-Host ("Diff query e2e: {0:N2}s" -f $t3.TotalSeconds)

Write-Host ""
Write-Host "=== Summary ==="
Write-Host ("First: {0:N2}s | Cached: {1:N2}s | Speedup: {2:N1}x | Diff query: {3:N2}s" -f $t1.TotalSeconds, $t2.TotalSeconds, ($t1.TotalSeconds/$t2.TotalSeconds), $t3.TotalSeconds)
