# Download alertmanager image using PowerShell Invoke-WebRequest (avoid curl schannel issue)
# Use .NET HTTP client which handles TLS through proxy better
$ErrorActionPreference = "Stop"
$proxy = "http://127.0.0.1:7897"
$image = "prom/alertmanager"
$tag = "latest"
$registry = "https://registry-1.docker.io/v2"

# Force TLS 1.2 + TLS 1.3 (Docker Hub CDN requires modern TLS)
# Some CDN nodes reject older protocols with "connection closed"
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.SecurityProtocolType]::Tls12 -bor `
    [Net.SecurityProtocolType]::Tls13 -bor `
    [Net.SecurityProtocolType]::Tls11

# Guard TLS 1.3 on older PowerShell (enum value may not exist)
try {
    [Net.ServicePointManager]::SecurityProtocol = `
        [Net.SecurityProtocolType]::Tls12 -bor `
        [Net.SecurityProtocolType]::Tls13
} catch {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
}

function Invoke-Api {
    param($url, $token, $outFile)
    $headers = @{
        "Authorization" = "Bearer $token"
        "Accept" = "application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json"
    }
    # Retry loop: CDN may close connection sporadically
    $maxRetries = 4
    $attempt = 0
    while ($attempt -lt $maxRetries) {
        $attempt++
        try {
            if ($outFile) {
                Invoke-WebRequest -Uri $url -Headers $headers -Proxy $proxy -OutFile $outFile -TimeoutSec 180
            } else {
                return Invoke-RestMethod -Uri $url -Headers $headers -Proxy $proxy -TimeoutSec 30
            }
            return  # success
        } catch {
            $msg = $_.Exception.Message
            if ($attempt -lt $maxRetries) {
                Write-Host "    Retry $attempt/$maxRetries after error: $msg" -ForegroundColor Yellow
                Start-Sleep -Seconds 2
            } else {
                Write-Host "    FAILED after $maxRetries attempts: $msg" -ForegroundColor Red
                throw
            }
        }
    }
}

# Step 1: Get auth token
Write-Host "Step 1: Get Docker Hub auth token..."
$authUrl = "https://auth.docker.io/token?service=registry.docker.io&scope=repository:$image`:pull"
$tokenResp = Invoke-RestMethod -Uri $authUrl -Proxy $proxy -TimeoutSec 30
$token = $tokenResp.token
Write-Host "  Token OK: $($token.Substring(0,24))..."

# Step 2: Get manifest (handle multi-arch)
Write-Host "Step 2: Get manifest..."
$manifestUrl = "$registry/$image/manifests/$tag"
$manifestFile = Join-Path $env:TEMP "manifest-$(Get-Random).json"
Invoke-Api -url $manifestUrl -token $token -outFile $manifestFile
$manifest = Get-Content $manifestFile -Raw | ConvertFrom-Json

if ($manifest.manifests) {
    Write-Host "  Multi-arch manifest list, selecting amd64..."
    $amd64 = $manifest.manifests | Where-Object {
        $_.platform.architecture -eq "amd64" -and $_.platform.os -eq "linux"
    } | Select-Object -First 1
    if (-not $amd64) {
        Write-Host "  ERROR: linux/amd64 platform not found!"
        exit 1
    }
    $subDigest = $amd64.digest
    Write-Host "  amd64 sub-manifest digest: $($subDigest.Substring(0,24))..."
    Invoke-Api -url "$registry/$image/manifests/$subDigest" -token $token -outFile $manifestFile
    $manifest = Get-Content $manifestFile -Raw | ConvertFrom-Json
}

Write-Host "  Config: $($manifest.config.digest.Substring(0,24))..."
Write-Host "  Layers: $($manifest.layers.Count)"

# Step 3: Download config and all layers
Write-Host "Step 3: Download image layers..."
$tempDir = Join-Path $env:TEMP "docker-am-$(Get-Random)"
New-Item -ItemType Directory -Path $tempDir -Force | Out-Null

# config
$configDigest = $manifest.config.digest
$configFileName = ($configDigest -replace ":", "_") + ".json"
$configFile = Join-Path $tempDir $configFileName
$configUrl = "$registry/$image/blobs/$configDigest"
Write-Host "  Download config..."
Invoke-Api -url $configUrl -token $token -outFile $configFile

# layers
$layerFileNames = @()
$i = 0
foreach ($layer in $manifest.layers) {
    $i++
    $digest = $layer.digest
    $fileName = ($digest -replace ":", "_") + ".tar.gz"
    $filePath = Join-Path $tempDir $fileName
    $blobUrl = "$registry/$image/blobs/$digest"
    Write-Host "  Download layer $i/$($manifest.layers.Count): $($digest.Substring(7,16))..."
    Invoke-Api -url $blobUrl -token $token -outFile $filePath
    $size = (Get-Item $filePath).Length / 1MB
    Write-Host "    OK: $([math]::Round($size, 2)) MB"
    $layerFileNames += $fileName
}

# Step 4: Build manifest.json
Write-Host "Step 4: Build docker load format..."
$loadManifestObj = [PSCustomObject]@{
    Config = $configFileName
    RepoTags = @("$image`:$tag")
    Layers = $layerFileNames
}
# Serialize object, then ensure top-level is an array (docker load expects [{...}])
# -AsArray param is PowerShell 7+ only; use manual wrapping for PS 5.1 compat
$single = ConvertTo-Json -InputObject $loadManifestObj -Depth 5
if ($single -match '^\s*\[') {
    $loadManifest = $single
} else {
    $loadManifest = "[" + $single + "]"
}
$loadManifestFile = Join-Path $tempDir "manifest.json"
# Write without BOM, as UTF-8 (docker load expects ASCII-compatible JSON)
[System.IO.File]::WriteAllText($loadManifestFile, $loadManifest, [System.Text.UTF8Encoding]::new($false))
Write-Host "  manifest.json written, size: $((Get-Item $loadManifestFile).Length) bytes"

# Step 5: Pack tar and docker load
Write-Host "Step 5: Pack and docker load..."
$tarFile = Join-Path $env:TEMP "alertmanager.tar"
Push-Location $tempDir
tar -cf $tarFile manifest.json $configFileName $layerFileNames
Pop-Location
& docker load -i $tarFile

# Cleanup
Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
Remove-Item -Force $tarFile -ErrorAction SilentlyContinue
Remove-Item -Force $manifestFile -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Done!"
& docker images $image
