# Switches the STAGED bridge.conf between two variants for an A/B test. The Play script copies it into the game at the
# next launch, so run this only while the game is closed.
#   A = the settings in use until now.
#   B = A plus a larger command queue and the bridge's own filter for repeated state changes.
#   & "$P\bridge_variant.ps1" A|B
param([Parameter(Mandatory)][ValidateSet('A', 'B')][string]$Variant)
if (Get-Process Blacklist_game, NvRemixBridge -ErrorAction SilentlyContinue) { 'VARIANT FAILED: the game or the bridge is running'; exit 1 }
$conf = Join-Path $PSScriptRoot 'bridge.conf'
$base = Get-Content (Join-Path $PSScriptRoot 'bridge.conf.variantA-20261111') -Raw
if ($Variant -eq 'B') {
    $base = $base.TrimEnd() + "`r`n`r`n# 2026-11-11 A/B test (variant B): a frame sends more commands than the default 3,072-slot queue holds`r`n" +
        "clientCmdQueueSize = 32768`r`nclientDataQueueSize = 32768`r`neliminateRedundantSetterCalls = True`r`n"
}
Set-Content $conf $base -NoNewline
"VARIANT $Variant staged: " + ((Get-Content $conf | Where-Object { $_ -match '^(clientCmdQueueSize|clientDataQueueSize|eliminateRedundantSetterCalls)' }) -join '; ')
