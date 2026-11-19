# Replays an apitrace recording of the game's Direct3D 9 calls with no Steam and no game process.
#   replay.ps1 -Trace <file.trace> -Mode plain   Windows' own Direct3D 9: the original game's picture
#   replay.ps1 -Trace <file.trace> -Mode proxy   our proxy in replay mode, chained to Windows' Direct3D 9
#   replay.ps1 -Trace <file.trace> -Mode stack   our proxy, the Remix bridge and Remix with ray tracing
# Writes <Out>\frames\f<frame>.png (d3dretrace's picture of every -SnapshotInterval'th frame), <Out>\retrace.log,
# <Out>\run.json and, in proxy and stack mode, the proxy's own files (d3d9_proxy.log, d3d9_proxy_frames.csv with
# one line of times per frame) in <Out>\proxy.
#   -Reference <folder with f*.png>   compares the pictures with apitrace's snapdiff.py; exit code 1 on a mismatch
#   -Side <recording folder>          proxy and stack mode: the side recording the proxy wrote in the recording run
#                                     (d3d9_proxy_replay_calls.csv: camera uploads and decal draws by frame and call
#                                     position; d3d9_proxy_replay_lights.bin: lamps and flares by frame)
#   -Lights <light dump .tsv>         instead of a lamp recording: one light dump (DumpLights) for every frame
#   -Setup <play-setup folder>        proxy and stack mode: take d3d9_proxy.ini, rtx.conf and bridge.conf from this
#                                     folder (a worktree's play-setup) instead of the installed ones in the main tree
# Windows PowerShell 5.1 compatible.
param([Parameter(Mandatory = $true)][string]$Trace, [ValidateSet('plain', 'proxy', 'stack')][string]$Mode = 'plain',
      [string]$Out = '', [string]$Proxy = 'D:\BlacklistOverhaul\experiments\replay-rig\build\d3d9.dll',
      [string]$Side = '', [string]$Lights = '', [string]$Reference = '', [int]$SnapshotInterval = 1, [double]$Fuzz = 0.05,
      [string]$Setup = '')
$ErrorActionPreference = 'Stop'
$Rig = 'D:\BlacklistOverhaul\experiments\replay-rig'
$Apitrace = Join-Path $Rig 'tools\apitrace-14.0-win32'
$Remix = 'D:\BlacklistOverhaul\tools\remix'
$Py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
$GameLock = Join-Path $Remix 'play-setup\AGENT_GAME_LOCK.txt'
if (-not $Setup) { $Setup = Join-Path $Remix 'play-setup' }
$Setup = (Resolve-Path -LiteralPath $Setup).Path
# The same C: limit as verify_runtime.ps1: the Remix bridge holds about 8 GB and the page file on C: grows with it.
$MinCFreeMB = 10500

$Trace = (Resolve-Path -LiteralPath $Trace).Path
if (-not $Out) { $Out = Join-Path $Rig ("runs\{0}-{1}-{2}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'), [IO.Path]::GetFileNameWithoutExtension($Trace), $Mode) }
$frames = Join-Path $Out 'frames'
New-Item -ItemType Directory -Force $frames | Out-Null
$stage = Join-Path $Out 'stage'
$exe = Join-Path $Apitrace 'bin\d3dretrace.exe'
$tookLock = $false
try {
    if ($Mode -ne 'plain') {
        New-Item -ItemType Directory -Force $stage | Out-Null
        Copy-Item -LiteralPath $exe, (Join-Path $Apitrace 'bin\D3Dcompiler_47.dll'), $Proxy -Destination $stage
        $exe = Join-Path $stage 'd3dretrace.exe'
        $ini = [IO.File]::ReadAllText((Join-Path $Setup 'd3d9_proxy.ini'))
        if ($Mode -eq 'proxy') {
            $ini = [Regex]::Replace($ini, '(?m)^\[Proxy\][ \t]*\r?\n', "[Proxy]`r`nChainDll=C:\Windows\SysWOW64\d3d9.dll`r`n", 1)
        }
        [IO.File]::WriteAllText((Join-Path $stage 'd3d9_proxy.ini'), $ini)
        foreach ($name in 'd3d9_proxy_replay_calls.csv', 'd3d9_proxy_replay_lights.bin') {
            if ($Side -and (Test-Path -LiteralPath (Join-Path $Side $name))) { Copy-Item -LiteralPath (Join-Path $Side $name) -Destination $stage }
        }
        if ($Lights) { Copy-Item -LiteralPath $Lights -Destination (Join-Path $stage 'd3d9_proxy_replay_lights.tsv') }
    }
    # Every mode holds the game lock for the whole replay: a replay is a graphics-card job, and a game launch or
    # another replay beside it would spoil both runs' timing.
    $busy = @(Get-Process Blacklist_game, Blacklist_DX11_game, NvRemixBridge, d3dretrace -ErrorAction SilentlyContinue)
    if ($busy.Count) { throw "$($busy[0].Name) is running: the graphics card is in use." }
    foreach ($lock in $GameLock, 'D:\BlacklistOverhaul\experiments\GAME_IN_USE.txt') {
        if (Test-Path -LiteralPath $lock) { throw "Someone else has the game: $(Get-Content -LiteralPath $lock -Raw)" }
    }
    if ($Mode -eq 'stack') {
        $cFreeMB = [int]((Get-PSDrive C).Free / 1MB)
        if ($cFreeMB -lt $MinCFreeMB) { throw "C: has $cFreeMB MB free, below $MinCFreeMB MB: Remix cannot start safely." }
    }
    New-Item -ItemType File -Path $GameLock -Value "replay.ps1 $Mode replay of $Trace, started $(Get-Date -Format 'HH:mm:ss')" | Out-Null
    $tookLock = $true
    if ($Mode -eq 'stack') {
        Copy-Item -LiteralPath (Join-Path $Remix 'pkg\d3d9.dll') -Destination (Join-Path $stage 'd3d9_remix.dll')
        Copy-Item -LiteralPath (Join-Path $Remix 'pkg\.trex') -Destination (Join-Path $stage '.trex') -Recurse
        Copy-Item -LiteralPath (Join-Path $Setup 'bridge.conf') -Destination (Join-Path $stage '.trex\bridge.conf')
        Copy-Item -LiteralPath (Join-Path $Setup 'rtx.conf') -Destination $stage
        New-Item -ItemType Directory -Force (Join-Path $stage 'rtx-remix\mods') | Out-Null
        New-Item -ItemType Junction -Path (Join-Path $stage 'rtx-remix\mods\materials') -Target (Join-Path $Remix 'materials') | Out-Null
    }
    $env:BLPROXY_REPLAY = $(if ($Mode -eq 'plain') { '' } else { '1' })
    $started = Get-Date
    Push-Location $(if ($Mode -eq 'plain') { $Out } else { $stage })
    try {
        $ErrorActionPreference = 'Continue'
        & $exe -s (Join-Path $frames 'f') --call-nos=0 "--snapshot-interval=$SnapshotInterval" $Trace 2>&1 | ForEach-Object { "$_" } | Set-Content (Join-Path $Out 'retrace.log')
        $exit = $LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } finally { Pop-Location }
    if ($Mode -ne 'plain') {
        New-Item -ItemType Directory -Force (Join-Path $Out 'proxy') | Out-Null
        Get-ChildItem -LiteralPath $stage -File | Where-Object { ($_.Name -like 'd3d9_proxy*' -or $_.Name -like 'lightpub*' -or $_.Name -like 'rtx_telemetry*') -and
                $_.Name -notin 'd3d9_proxy_replay_calls.csv', 'd3d9_proxy_replay_lights.bin', 'd3d9_proxy_replay_lights.tsv' } |
            Copy-Item -Destination (Join-Path $Out 'proxy')
    }
    $pictures = @(Get-ChildItem -LiteralPath $frames -Filter 'f*.png').Count
    $rendered = Select-String -LiteralPath (Join-Path $Out 'retrace.log') -Pattern '^Rendered (\d+) frames in ([\d.]+) secs' | Select-Object -Last 1
    $result = [ordered]@{ trace = $Trace; mode = $Mode; started = $started.ToString('s'); exitCode = $exit; pictures = $pictures
                          framesRendered = $(if ($rendered) { [int]$rendered.Matches[0].Groups[1].Value }); seconds = $(if ($rendered) { [double]$rendered.Matches[0].Groups[2].Value })
                          proxySha256 = $(if ($Mode -ne 'plain') { (Get-FileHash -LiteralPath $Proxy).Hash }); side = $Side; lights = $Lights; setup = $(if ($Mode -ne 'plain') { $Setup }); compare = $null }
    $failed = $exit -ne 0 -or -not $pictures
    if ($Reference -and $pictures) {
        $text = & $Py -I (Join-Path $Apitrace 'lib\scripts\snapdiff.py') -v -f $Fuzz -o (Join-Path $Out 'compare.html') ((Resolve-Path -LiteralPath $Reference).Path + '\f') ($frames + '\f') 2>&1 | ForEach-Object { "$_" }
        $text | Set-Content (Join-Path $Out 'compare.txt')
        $compared = @($text | Where-Object { $_ -like 'Comparing *' })
        $mismatch = @($compared | Where-Object { $_ -like '*MISMATCH' })
        $missing = @($text | Where-Object { $_ -like '*MISSING*' })
        $result.compare = [ordered]@{ reference = $Reference; fuzz = $Fuzz; compared = $compared.Count; mismatch = $mismatch.Count; missing = $missing.Count }
        if (-not $compared.Count -or $mismatch.Count -or $missing.Count) { $failed = $true }
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $Out 'run.json')
    Write-Host "$Mode replay: $($result.framesRendered) frames, $pictures pictures, exit $exit, run folder $Out"
    if ($result.compare) { Write-Host "compare: $($result.compare.compared) frames compared, $($result.compare.mismatch) differ, $($result.compare.missing) missing" }
    if ($failed) { exit 1 }
} finally {
    if ($tookLock) { Remove-Item -LiteralPath $GameLock }
}
