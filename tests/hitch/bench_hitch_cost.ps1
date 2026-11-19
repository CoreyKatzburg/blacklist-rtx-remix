# HitchSampler cost on a game-like render thread (no game, no GPU). Builds tests\hitch\hitch_cost_bench.cpp x86
# against the real play-setup\unified-build\proxy_diagnostics.cpp, then runs it off/on alternately -Pairs times
# and prints each run plus the median of each side. A measurement, not a test: named bench_* so run_all_offline
# does not pick it up. It loads 4 cores for about a minute, so do not run it while a game launch is measuring.
[CmdletBinding()]
param([int]$Pairs = 4, [int]$Frames = 600)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$unified = Join-Path $remixRoot 'play-setup\unified-build'
$lock = 'D:\BlacklistOverhaul\tools\remix\play-setup\AGENT_GAME_LOCK.txt'
if (Test-Path -LiteralPath $lock) { throw "a game launch holds $lock; run this when it is gone" }
$build = Join-Path $here '_build'
New-Item -ItemType Directory -Force -Path (Join-Path $build 'bench') | Out-Null
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$sources = @('proxy_diagnostics.cpp', 'proxy_log.cpp', 'lightpub.cpp', 'shader_traits.cpp', 'camera_solver.cpp',
             'pass_graph.cpp', 'draw_filter.cpp', 'decal_tag.cpp', 'handoff_renderer.cpp') |
    ForEach-Object { "`"$(Join-Path $unified $_)`"" }
$exe = Join-Path $build 'hitch_cost_bench.exe'
$cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc /I`"$unified`" " +
       "`"$(Join-Path $here 'hitch_cost_bench.cpp')`" $($sources -join ' ') /Fe:`"$exe`" /Fo:`"$build\bench\\`" " +
       "/link /NOLOGO d3d9.lib user32.lib psapi.lib"
& $env:ComSpec /d /s /c $cmd | Out-Null
if ($LASTEXITCODE -ne 0) { throw "compile failed (exit $LASTEXITCODE)" }

$results = @{ off = @(); on = @() }
for ($i = 1; $i -le $Pairs; $i++) {
    foreach ($mode in 'off', 'on') {
        $line = & $exe $mode $Frames
        Write-Host "pair $i $line"
        if ($line -match 'median ([\d.]+) ms') { $results[$mode] += [double]$Matches[1] }
    }
}
$med = { param($v) $s = $v | Sort-Object; $s[[int][Math]::Floor($s.Count / 2)] }
$off = & $med $results.off
$on = & $med $results.on
Write-Host ("median of run medians: off {0:N3} ms, on {1:N3} ms, cost {2:N3} ms per frame" -f $off, $on, ($on - $off))
