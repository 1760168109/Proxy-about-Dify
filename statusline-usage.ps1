#Requires -Version 5.1
<#
  Claude Code 自定义 statusLine 脚本（随代理分发，可固化到本机）。

  两种用法：
  1) CC statusLine.command 调用（stdin 为 CC 状态 JSON）
  2) 手动：powershell -NoProfile -File .\statusline-usage.ps1

  展示：上下文%（来自 CC）+ 本机按次账本（来自代理 GET /v1/usage）
  代理地址可用环境变量 LAN_PROXY_URL，默认 http://127.0.0.1:7272
#>
$ErrorActionPreference = "SilentlyContinue"
$proxy = if ($env:LAN_PROXY_URL) { $env:LAN_PROXY_URL.TrimEnd("/") } else { "http://127.0.0.1:7272" }

# --- CC 侧（可选 stdin）---
$ctxPart = $null
try {
  $raw = [Console]::In.ReadToEnd()
  if ($raw -and $raw.Trim()) {
    $j = $raw | ConvertFrom-Json
    $pct = $j.context_window.used_percentage
    $sz = $j.context_window.context_window_size
    $mid = $j.model.id
    $bits = @()
    if ($mid) { $bits += "$mid" }
    if ($null -ne $pct) { $bits += ("{0:F0}%ctx" -f $pct) }
    elseif ($sz) {
      if ($sz -ge 1000000) { $bits += ("{0}Mctx" -f [math]::Floor($sz / 1000000)) }
      elseif ($sz -ge 1000) { $bits += ("{0}Kctx" -f [math]::Floor($sz / 1000)) }
    }
    if ($bits.Count -gt 0) { $ctxPart = ($bits -join " ") }
  }
} catch { }

# --- 代理按次账本 ---
$billPart = "lan:off"
try {
  $r = Invoke-RestMethod -Uri "$proxy/v1/usage" -TimeoutSec 2
  $usd = [math]::Round([double]$r.estimated_usd, 0)
  $billPart = "opus×$($r.opus_calls) `$$usd haiku×$($r.haiku_calls)"
} catch {
  try {
    $t = (Invoke-WebRequest -Uri "$proxy/v1/usage/statusline" -UseBasicParsing -TimeoutSec 2).Content
    if ($t) { $billPart = $t.Trim() }
  } catch { }
}

if ($ctxPart) {
  Write-Output ("{0} | {1}" -f $ctxPart, $billPart)
} else {
  Write-Output $billPart
}
