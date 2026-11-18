# Query probe check (worker B, X1 [Proxy] QueryStatsFrames and the rebuilt X2 [Proxy] SkipBracketDraws). No GPU, no game.
#   normal build of query_probe_test.cpp (linked with the real pass_graph.cpp) must PASS
#   each mutant build (key ignored, the first X2's before-marker span, draws reading the scene HDR skipped, every poll
#   counted as pixels>0, window one frame late) must FAIL
#   source checks on d3d9_proxy.cpp and draw_filter.cpp, each with a fake source that must fail it:
#     both keys default to 0 (off) at load and at live reload
#     Issue (slot 6) is hooked only when QueryStatsFrames > 0; GetData (slot 7) stays the only hook otherwise
#     HookQueryGetData returns the old path (QueryGetDataCached) untouched while the probe is off
#     draw_filter.cpp calls SkipAfterHandoffClass exactly once, with the live key, inside the MarkerDoneThisFrame()
#     branch after the composite and ModernFilter skips; the old SkipBracketDrawClass call is gone
# Exit code 0 = all as required, 1 = something is not.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$unified = Join-Path $remixRoot 'play-setup\unified-build'
$build = Join-Path $here '_build'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC not found: $vcvars" }

function Build-Test([string]$name, [string]$define) {
    $exe = Join-Path $build "$name.exe"
    $obj = Join-Path $build $name
    New-Item -ItemType Directory -Force -Path $obj | Out-Null
    $cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc $define /I`"$unified`" " +
           "`"$(Join-Path $here 'query_probe_test.cpp')`" `"$unified\pass_graph.cpp`" `"$unified\shader_traits.cpp`" /Fe:`"$exe`" /Fo:`"$obj\\`" /link /NOLOGO"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile of $name failed (exit $LASTEXITCODE)" }
    $exe
}

$ok = $true

$normal = Build-Test 'query_probe_test' ''
& $normal | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Host 'FAIL: the normal build did not pass' -ForegroundColor Red; $ok = $false }
else { Write-Host 'ok: the normal build passes' }

$mutants = @('QUERY_PROBE_MUTANT_IGNORE_KEY', 'QUERY_PROBE_MUTANT_BEFORE_MARKER_ONLY', 'QUERY_PROBE_MUTANT_SKIP_READERS',
             'QUERY_PROBE_MUTANT_POLL_COUNTS_ZERO', 'QUERY_PROBE_MUTANT_WINDOW_OFF_BY_ONE')
foreach ($m in $mutants) {
    $exe = Build-Test ("query_probe_" + $m.ToLowerInvariant()) "/D$m"
    $out = @(& $exe)
    $code = $LASTEXITCODE
    $out | Where-Object { $_ -like 'FAIL*' } | Out-Host
    if ($code -ne 1) { Write-Host "FAIL: mutant $m was not caught (exit $code)" -ForegroundColor Red; $ok = $false }
    else { Write-Host "ok: mutant $m fails" }
}

# Source checks. Each returns a list of problems; empty means the source is as required.
function Test-ProxySource([string]$text) {
    $problems = @()
    foreach ($key in 'QueryStatsFrames', 'SkipBracketDraws') {
        $reads = @([regex]::Matches($text, "GetPrivateProfileIntW\(L`"Proxy`", L`"$key`", (\w+), iniPath\)") |
                   ForEach-Object { $_.Groups[1].Value })
        if ($reads.Count -eq 0) { $problems += "$key is never read" }
        foreach ($d in $reads) { if ($d -ne '0') { $problems += "$key defaults to $d, not 0" } }
    }
    if ($text -notmatch 'HookObjectMethods\(\*query, slots, proxy::g_queryProbe\.Enabled\(\) \? ARRAYSIZE\(slots\) : 1\)') {
        $problems += 'HookCreateQuery does not limit the hooks to GetData while the probe is off'
    }
    if ($text -notmatch '(?s)const MethodSlot slots\[\] = \{\s*\{7, reinterpret_cast<void\*>\(&HookQueryGetData\)[^\n]*\n\s*\{6, reinterpret_cast<void\*>\(&HookQueryIssue\)') {
        $problems += 'the slot table does not list GetData (7) first and Issue (6) second'
    }
    if ($text -notmatch '(?s)HRESULT WINAPI HookQueryGetData\([^)]*\) \{\s*CallTimer timer\(proxy::kQueryGetData\);\s*if \(!proxy::g_queryProbe\.Enabled\(\)\) return QueryGetDataCached\(self, data, size, flags\);') {
        $problems += 'HookQueryGetData does not hand straight to QueryGetDataCached while the probe is off'
    }
    $problems
}
function Test-FilterSource([string]$text) {
    $problems = @()
    $call = 'SkipAfterHandoffClass\(g_skipBracketDraws, g_cfg\.passFilter, g_passGraph\.MarkerDoneThisFrame\(\),\s*' +
            'g_modernAwaitingComposite, pixel\.composite, g_rt0 == CurrentSceneHdrTarget\(\),\s*ctx\.stage0ReadsSceneHdr\)'
    $n = [regex]::Matches($text, 'SkipAfterHandoffClass\(').Count
    if ($n -ne 1) { $problems += "SkipAfterHandoffClass is called $n times, not once" }
    if ($text -notmatch $call) { $problems += 'SkipAfterHandoffClass is not given the live key and draw state' }
    # Placement: inside if (MarkerDoneThisFrame()) { if (g_modernAwaitingComposite) { composite ... ModernFilter skip ...
    # X2 ... } g_drawIsWasted = dec.skipDraw; return; }
    $placed = '(?s)if \(g_passGraph\.MarkerDoneThisFrame\(\)\) \{\s*if \(g_modernAwaitingComposite\) \{\s*' +
              'if \(pixel\.composite && g_rt0\) \{[^}]*\}\s*' +
              'if \(g_rt0 != CurrentSceneHdrTarget\(\) \|\| ctx\.stage0ReadsSceneHdr\) \{[^}]*\}\s*' +
              '(//[^\n]*\n\s*)*if \(' + $call + '\) \{[^}]*\}\s*\}\s*g_drawIsWasted = dec\.skipDraw;\s*return;'
    if ($text -notmatch $placed) { $problems += 'SkipAfterHandoffClass is not the last check inside the marker-done branch' }
    if ($text -match 'SkipBracketDrawClass') { $problems += 'the old SkipBracketDrawClass call is still there' }
    $problems
}

$proxyText = Get-Content -Raw (Join-Path $unified 'd3d9_proxy.cpp')
$filterText = Get-Content -Raw (Join-Path $unified 'draw_filter.cpp')

$p = @(Test-ProxySource $proxyText) + @(Test-FilterSource $filterText)
if ($p.Count) { $p | ForEach-Object { Write-Host "FAIL: $_" -ForegroundColor Red }; $ok = $false }
else { Write-Host 'ok: keys default to 0, Issue hooked only with the probe on, GetData passes through while off, X2 reads the live key in the marker-done branch' }

# The X2 block in draw_filter.cpp, from its comment to its closing brace; the fakes move or copy it.
$x2Block = '(?s)\r?\n[ \t]*// SkipBracketDraws \(query_probe\.h, X2\).*?return;\s*\}'
$x2Match = [regex]::Match($filterText, $x2Block)
if (-not $x2Match.Success) { Write-Host 'FAIL: the X2 block was not found in draw_filter.cpp' -ForegroundColor Red; $ok = $false }
$x2Text = $x2Match.Value.Replace('$', '$$')

# Fake sources: each must be caught.
$fakes = [ordered]@{
    'QueryStatsFrames defaults to 30'      = { Test-ProxySource ($proxyText -replace 'L"QueryStatsFrames", 0,', 'L"QueryStatsFrames", 30,') }
    'SkipBracketDraws defaults to 1'       = { Test-ProxySource ($proxyText -replace 'L"SkipBracketDraws", 0,', 'L"SkipBracketDraws", 1,') }
    'Issue always hooked'                  = { Test-ProxySource ($proxyText -replace 'proxy::g_queryProbe\.Enabled\(\) \? ARRAYSIZE\(slots\) : 1', 'ARRAYSIZE(slots)') }
    'Issue listed before GetData'          = { Test-ProxySource ($proxyText -replace '\{7, reinterpret_cast<void\*>\(&HookQueryGetData\)', '{6, reinterpret_cast<void*>(&HookQueryIssue)') }
    'GetData counts with the probe off'    = { Test-ProxySource ($proxyText -replace 'if \(!proxy::g_queryProbe\.Enabled\(\)\) return QueryGetDataCached\(self, data, size, flags\);', '') }
    'X2 given a fixed key'                 = { Test-FilterSource ($filterText -replace 'SkipAfterHandoffClass\(g_skipBracketDraws,', 'SkipAfterHandoffClass(1u,') }
    'X2 moved after the branch (old place)' = { Test-FilterSource (($filterText -replace $x2Block, '') -replace '(\r?\n    SetBracket\(device, dec\.openBracket\);)', ($x2Text + '$1')) }
    'X2 called twice'                      = { Test-FilterSource ($filterText -replace $x2Block, ($x2Text + $x2Text)) }
    'X2 before the ModernFilter skip'      = { Test-FilterSource (($filterText -replace $x2Block, '') -replace '(\r?\n\s*if \(g_rt0 != CurrentSceneHdrTarget\(\) \|\| ctx\.stage0ReadsSceneHdr\) \{)', ($x2Text + '$1')) }
    'old SkipBracketDrawClass call left'   = { Test-FilterSource ($filterText + "`n// SkipBracketDrawClass(g_skipBracketDraws, 0, 0, 0, 0, 0);`n") }
}
foreach ($name in $fakes.Keys) {
    $found = @(& $fakes[$name])
    if ($found.Count -eq 0) { Write-Host "FAIL: the source check does not catch: $name" -ForegroundColor Red; $ok = $false }
    else { Write-Host "ok: the source check catches: $name" }
}

if ($ok) { Write-Host 'query probe test: PASS' -ForegroundColor Green; exit 0 }
Write-Host 'query probe test: FAIL' -ForegroundColor Red
exit 1
