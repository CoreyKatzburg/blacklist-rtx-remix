# Model check of the Remix bridge data queue (TLA+ / TLC). No game, no GPU, about 15 s. Exit 0 = every run gave
# its expected result and the covered bridge code is unchanged since the model was last confirmed against it.
# tests\run_all_offline.ps1 finds this script by its name (run_*.ps1) and runs it with the other offline checks.
# How to read a failure: D:\BlacklistOverhaul\.agents\skills\blacklist-graphics-verification\features\bridge-data-queue.md
$ErrorActionPreference = 'Stop'
$py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $py)) { $py = 'python' }
if (-not (Get-Command java -ErrorAction SilentlyContinue)) { Write-Host 'FAIL: java is not on PATH (TLC needs Java 11 or newer).'; exit 1 }
& $py -I (Join-Path $PSScriptRoot 'check_bridge_queue.py') @args
exit $LASTEXITCODE
