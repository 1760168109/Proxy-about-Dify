#Requires -Version 5.1
<#
  Install once, then start the proxy from any folder by typing:

      lan

  Usage:
    powershell -ExecutionPolicy Bypass -File .\install.ps1
    powershell -ExecutionPolicy Bypass -File .\install.ps1 -StatusLine
      # 额外把 Claude Code statusLine 接到本目录 statusline-usage.ps1（按次账本）
#>
param(
    [switch]$StatusLine
)

$ErrorActionPreference = "Stop"

$proxyDir = $PSScriptRoot
$cmdPath = Join-Path $proxyDir "lan.cmd"
if (-not (Test-Path $cmdPath)) {
    Write-Error "lan.cmd not found: $cmdPath"
}

# Warn if "alan" on this machine is something else (common: Claude launcher)
$existingAlan = Get-Command alan -ErrorAction SilentlyContinue
if ($existingAlan -and $existingAlan.Source -notlike ($proxyDir + "*")) {
    Write-Host "[lan] Note: command 'alan' already exists on this PC:"
    Write-Host "      $($existingAlan.Source)"
    Write-Host "      That is NOT this proxy. We use 'lan' to avoid overwriting it."
    Write-Host ""
}

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($null -eq $userPath) { $userPath = "" }

$parts = @(
    $userPath -split ";" |
    Where-Object { $_ -and $_.Trim() -ne "" } |
    ForEach-Object { $_.TrimEnd("\") }
)
$target = $proxyDir.TrimEnd("\")
$already = $parts | Where-Object { $_ -ieq $target }

if (-not $already) {
    $newPath = if ($userPath.Trim() -eq "") { $proxyDir } else { "$userPath;$proxyDir" }
    [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    $env:Path = "$env:Path;$proxyDir"
    Write-Host "[lan] Added to user PATH:"
    Write-Host "      $proxyDir"
} else {
    if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $target })) {
        $env:Path = "$env:Path;$proxyDir"
    }
    Write-Host "[lan] Already on user PATH (skip)."
}

$req = Join-Path $proxyDir "requirements.txt"
if (Test-Path $req) {
    Write-Host "[lan] Installing Python packages..."
    python -m pip install -r $req -q
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "pip failed. Run: python -m pip install -r requirements.txt"
    } else {
        Write-Host "[lan] Packages OK."
    }
}

$envFile = Join-Path $proxyDir ".env"
$example = Join-Path $proxyDir ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $example)) {
    Copy-Item $example $envFile
    Write-Host "[lan] Created .env — set DIFY_USER_ID to your own name."
}

# 可选：把 CC statusLine 指到本仓库脚本（计费数据仍由代理端点提供）
if ($StatusLine) {
    $statusScript = Join-Path $proxyDir "statusline-usage.ps1"
    if (-not (Test-Path $statusScript)) {
        Write-Warning "statusline-usage.ps1 missing; skip -StatusLine"
    } else {
        $claudeDir = Join-Path $env:USERPROFILE ".claude"
        $settingsPath = Join-Path $claudeDir "settings.json"
        if (-not (Test-Path $claudeDir)) {
            New-Item -ItemType Directory -Path $claudeDir | Out-Null
        }
        $scriptPosix = ($statusScript -replace "\\", "/")
        $cmd = "powershell -NoProfile -File `"$scriptPosix`""
        if (Test-Path $settingsPath) {
            $obj = Get-Content $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } else {
            $obj = [pscustomobject]@{}
        }
        $obj | Add-Member -NotePropertyName statusLine -NotePropertyValue ([pscustomobject]@{
                type    = "command"
                command = $cmd
            }) -Force
        $json = $obj | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText(
            $settingsPath,
            $json + "`n",
            [System.Text.UTF8Encoding]::new($false)
        )
        Write-Host "[lan] Claude Code statusLine → $statusScript"
        Write-Host "      (reads meter from http://127.0.0.1:7272/v1/usage)"
    }
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Installed."
Write-Host ""
Write-Host "  1) Open a NEW PowerShell window"
Write-Host "  2) Type:  lan"
Write-Host "  3) Keep the window open while chatting"
Write-Host ""
Write-Host "  URL: http://127.0.0.1:7272"
if (-not $StatusLine) {
    Write-Host ""
    Write-Host "  Optional statusline (per-call `$ meter):"
    Write-Host "    powershell -ExecutionPolicy Bypass -File .\install.ps1 -StatusLine"
}
Write-Host "========================================"
