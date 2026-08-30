#Requires -Version 7.0
<#
  Install once, then start the proxy from any folder by typing:

      lan

  Usage:
    pwsh -ExecutionPolicy Bypass -File .\install.ps1
    pwsh -ExecutionPolicy Bypass -File .\install.ps1 -StatusLine
      # Also connect Claude Code statusLine to this folder's per-call ledger.
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

# Put the command on PATH through an ASCII-only junction. This avoids command
# discovery failures when a terminal host corrupts non-ASCII PATH entries.
$launcherDir = Join-Path $env:LOCALAPPDATA "lan-proxy"
$launcherItem = Get-Item -LiteralPath $launcherDir -Force -ErrorAction SilentlyContinue
if ($launcherItem) {
    $isReparsePoint = [bool]($launcherItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
    if (-not $launcherItem.PSIsContainer -or -not $isReparsePoint) {
        Write-Error "Cannot install launcher: '$launcherDir' exists and is not a directory junction."
    }

    # Delete only the junction itself, never its target, then recreate it in
    # case the project folder was renamed or moved.
    [System.IO.Directory]::Delete($launcherDir)
}
New-Item -ItemType Junction -Path $launcherDir -Target $proxyDir | Out-Null
Write-Host "[lan] Launcher -> $proxyDir"

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

$proxyTarget = $proxyDir.TrimEnd("\")
$launcherTarget = $launcherDir.TrimEnd("\")
$parts = @(
    $userPath -split ";" |
    Where-Object { $_ -and $_.Trim() -ne "" } |
    ForEach-Object { $_.Trim().TrimEnd("\") } |
    Where-Object { $_ -ine $proxyTarget -and $_ -ine $launcherTarget }
)
$newUserPath = (@($parts) + $launcherDir) -join ";"
[Environment]::SetEnvironmentVariable("Path", $newUserPath, "User")

if (-not (($env:Path -split ";") | Where-Object { $_.TrimEnd("\") -ieq $launcherTarget })) {
    $env:Path = "$env:Path;$launcherDir"
}
Write-Host "[lan] User PATH -> $launcherDir"

$req = Join-Path $proxyDir "requirements.txt"
if (Test-Path $req) {
    Write-Host "[lan] Installing Python packages..."
    python -m pip install -r $req -q
    if ($LASTEXITCODE -ne 0) {
        Write-Error "pip failed. Run: python -m pip install -r requirements.txt"
    } else {
        Write-Host "[lan] Packages OK."
    }
}

$envFile = Join-Path $proxyDir ".env"
$example = Join-Path $proxyDir ".env.example"
if (-not (Test-Path $envFile) -and (Test-Path $example)) {
    Copy-Item $example $envFile
    Write-Host "[lan] Created .env - set DIFY_USER_ID to your own name."
}

# 子代理身份与完成报告依赖两个 command hook。结构化合并只替换本项目自己的
# claude_hook.py 项，保留用户已有的其它 hooks / permissions / env。
$hookScript = Join-Path $proxyDir "claude_hook.py"
if (Test-Path $hookScript) {
    $claudeDir = Join-Path $env:USERPROFILE ".claude"
    $settingsPath = Join-Path $claudeDir "settings.json"
    if (-not (Test-Path $claudeDir)) {
        New-Item -ItemType Directory -Path $claudeDir | Out-Null
    }
    python $hookScript --install $settingsPath
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to install Claude Code subagent hooks."
    }
    Write-Host "[lan] Claude hooks -> SubagentStart / SubagentStop"
}

# Optional: point Claude Code statusLine at the local per-call usage script.
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
        $cmd = "pwsh -NoProfile -File `"$scriptPosix`""
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
        Write-Host "[lan] Claude Code statusLine -> $statusScript"
        Write-Host "      (reads meter from http://127.0.0.1:7272/v1/usage)"
    }
}

Write-Host ""
Write-Host "========================================"
Write-Host "  Installed."
Write-Host ""
Write-Host "  1) Open a NEW PowerShell 7 window"
Write-Host "  2) Type:  lan"
Write-Host "  3) Keep the window open while chatting"
Write-Host ""
Write-Host "  URL: http://127.0.0.1:7272"
if (-not $StatusLine) {
    Write-Host ""
    Write-Host "  Optional statusline (per-call `$ meter):"
    Write-Host "    pwsh -ExecutionPolicy Bypass -File .\install.ps1 -StatusLine"
}
Write-Host "========================================"
