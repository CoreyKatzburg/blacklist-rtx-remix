# Diagnostic: dumps a burst of frames (every 6th frame, -Frames of them) while the camera turns, to see what the game
# does to Sam's draws as the camera swings past him. Needs the proxy build with [Proxy] CaptureBurst.
#   & "$P\fade_burst.ps1" -Out <folder> [-Frames 40] [-Turn 3750]
param([Parameter(Mandatory)][string]$Out, [int]$Frames = 40, [int]$Turn = 3750)
$S = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM"
$ini = "$S\d3d9_proxy.ini"; $dumpDir = "$S\framedump"
if (-not (Get-Process Blacklist_game -ErrorAction SilentlyContinue)) { 'BURST FAILED: Blacklist_game is not running'; exit 1 }
New-Item -ItemType Directory -Force $Out | Out-Null
function SetKey([string]$text, [string]$key, [string]$value) {
    $pattern = '(?m)^' + [regex]::Escape($key) + '\s*=.*$'
    if ($text -match $pattern) { return [regex]::Replace($text, $pattern, "$key=$value") }
    return $text.Replace('[Proxy]', "[Proxy]`r`n$key=$value")
}
$iniQuoted = $ini.Replace("'", "''")   # the path has an apostrophe; it goes inside a quoted command below
$original = Get-Content $ini -Raw
$before = @(Get-ChildItem $dumpDir -Filter 'f*_draws.tsv' -ErrorAction SilentlyContinue | ForEach-Object Name)
try {
    Set-Content $ini (SetKey (SetKey $original 'CaptureBurst' "$Frames") 'CaptureDraws' '0') -NoNewline
    Start-Sleep -Milliseconds 500
    # The turn first (its start-up takes about a second), the burst trigger from a second process half a second in.
    $trigger = Start-Process powershell -WindowStyle Hidden -PassThru -ArgumentList '-NoProfile', '-Command',
        "Start-Sleep -Milliseconds 700; `$t = Get-Content -LiteralPath '$iniQuoted' -Raw; Set-Content -LiteralPath '$iniQuoted' ([regex]::Replace(`$t, '(?m)^CaptureDraws=\d', 'CaptureDraws=1')) -NoNewline"
    "BURST start $(Get-Date -Format HH:mm:ss.fff)"
    & (Join-Path $PSScriptRoot 'rca_keys.ps1') -Do "turn:$Turn", 'wait:2500'
    $trigger.WaitForExit(5000) | Out-Null
    Start-Sleep -Seconds 3
}
finally { Set-Content $ini $original -NoNewline }
Start-Sleep -Seconds 1
$new = @(Get-ChildItem $dumpDir -Filter 'f*_draws.tsv' | Where-Object { $_.Name -notin $before } | Sort-Object Name)
foreach ($f in $new) { Copy-Item $f.FullName $Out -Force }
"BURST DONE: $($new.Count) frame dumps copied to $Out ($(($new | Select-Object -First 1).Name) .. $(($new | Select-Object -Last 1).Name))"
