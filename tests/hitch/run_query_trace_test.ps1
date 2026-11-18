# Query trace check (worker B, X3: [Proxy] QueryTraceEvery and PresentGrabFrames). Needs a GPU for the grab part, no game.
#   normal build of query_trace_test.cpp must PASS, and the 1/8 grab it writes on a real D3D9 device must read back with
#   numpy (an independent PFM reader) as the box averages of the test pattern: checkerboard cell 0.5, red cell (1,0,0),
#   quarter-filled cell 0.25, the rest 0, top row first
#   each logic mutant (every frame traced, last draw kept instead of the first, answers of any query taken, drops after the
#   first query counted, grabs never stop) must FAIL; the point-sampling grab mutant and the reversed half-float channel
#   mutant must fail the numpy check (the grab is checked on an X8R8G8B8 and an A16B16G16R16F target)
#   source checks on d3d9_proxy.cpp and draw_filter.cpp, each with a fake source that must fail it:
#     both keys default to 0; the trace is active only with QueryStatsFrames on
#     all four draw hooks report the dropped path and the sent path; the trace entry points return at once while off
#     the composite grab follows PresentModernSceneToComposite; the present grab and the grab counter end
#     FilterEndFrameBeforePresent; a new GateDump tag starts the grabs; GrabFrame returns at once while off
# Exit code 0 = all as required, 1 = something is not, 2 = no D3D9 device.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixRoot = Split-Path -Parent (Split-Path -Parent $here)
$unified = Join-Path $remixRoot 'play-setup\unified-build'
$build = Join-Path $here '_build'
$python = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
if (-not (Test-Path -LiteralPath $vcvars)) { throw "MSVC not found: $vcvars" }
if (-not (Test-Path -LiteralPath $python)) { throw "Python not found: $python" }

function Build-Test([string]$name, [string]$define) {
    $exe = Join-Path $build "$name.exe"
    $obj = Join-Path $build $name
    New-Item -ItemType Directory -Force -Path $obj | Out-Null
    $cmd = "call `"$vcvars`" x86 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc $define /I`"$unified`" " +
           "`"$(Join-Path $here 'query_trace_test.cpp')`" /Fe:`"$exe`" /Fo:`"$obj\\`" /link /NOLOGO d3d9.lib user32.lib"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile of $name failed (exit $LASTEXITCODE)" }
    $exe
}

# Reads the grab with numpy and compares it with the box averages of the pattern. Prints PASS or FAIL lines.
$check = @'
import sys
import numpy as np
path = sys.argv[1]
with open(path, "rb") as f:
    kind = f.readline().strip(); w, h = map(int, f.readline().split()); scale = float(f.readline())
    a = np.fromfile(f, "<f4" if scale < 0 else ">f4").reshape(h, w, 3)[::-1]
want = np.zeros((6, 8, 3), np.float32)
want[0, 0] = 0.5
want[2, 1] = (1.0, 0.0, 0.0)
want[0, 3] = 0.25
bad = []
if kind != b"PF" or (w, h) != (8, 6):
    bad.append(f"header {kind!r} {w}x{h}, want PF 8x6")
else:
    err = np.abs(a - want).max(axis=2)
    for y, x in zip(*np.nonzero(err > 0.01)):
        bad.append(f"cell ({x},{y}) {a[y, x].round(3).tolist()} want {want[y, x].tolist()}")
print("FAIL grab: " + "; ".join(bad) if bad else "PASS grab: 8x6, box averages as drawn, top row first")
sys.exit(1 if bad else 0)
'@
$checkPy = Join-Path $build 'query_trace_grab_check.py'
Set-Content -LiteralPath $checkPy -Value $check -Encoding utf8

$ok = $true
$grab = Join-Path $build 'query_trace_grab.pfm'
$hdrGrab = Join-Path $build 'query_trace_grab_hdr.pfm'
Remove-Item -LiteralPath $grab, $hdrGrab -ErrorAction SilentlyContinue
$normal = Build-Test 'query_trace_test' ''
& $normal $grab $hdrGrab | Out-Host
$code = $LASTEXITCODE
if ($code -eq 2) { Write-Host 'query trace test: no D3D9 device' -ForegroundColor Yellow; exit 2 }
if ($code -ne 0) { Write-Host 'FAIL: the normal build did not pass' -ForegroundColor Red; $ok = $false }
else { Write-Host 'ok: the normal build passes' }
foreach ($g in $grab, $hdrGrab) {
    & $python -I $checkPy $g | Out-Host
    if ($LASTEXITCODE -ne 0) { Write-Host "FAIL: the grab $g does not read back as drawn" -ForegroundColor Red; $ok = $false }
}

$mutants = @('QUERY_TRACE_MUTANT_EVERY_FRAME', 'QUERY_TRACE_MUTANT_LAST_DRAW', 'QUERY_TRACE_MUTANT_ANY_QUERY',
             'QUERY_TRACE_MUTANT_DROPS_AFTER_QUERY', 'QUERY_TRACE_MUTANT_GRAB_NO_STOP')
foreach ($m in $mutants) {
    $exe = Build-Test ("query_trace_" + $m.ToLowerInvariant()) "/D$m"
    $out = @(& $exe)
    $code = $LASTEXITCODE
    $out | Where-Object { $_ -like 'FAIL*' } | Out-Host
    if ($code -ne 1) { Write-Host "FAIL: mutant $m was not caught (exit $code)" -ForegroundColor Red; $ok = $false }
    else { Write-Host "ok: mutant $m fails" }
}
# Grab mutants: each must fail the numpy check on the grab it spoils (point sampling: both; half channels reversed:
# the A16B16G16R16F grab).
foreach ($gm in @(@('QUERY_TRACE_MUTANT_GRAB_POINT', 0), @('QUERY_TRACE_MUTANT_HALF_BGR', 1))) {
    $pointGrab = Join-Path $build 'query_trace_grab_mutant.pfm'
    $pointHdr = Join-Path $build 'query_trace_grab_mutant_hdr.pfm'
    Remove-Item -LiteralPath $pointGrab, $pointHdr -ErrorAction SilentlyContinue
    $exe = Build-Test ('query_trace_' + $gm[0].ToLowerInvariant()) "/D$($gm[0])"
    & $exe $pointGrab $pointHdr | Out-Null
    & $python -I $checkPy (@($pointGrab, $pointHdr)[$gm[1]]) | Out-Host
    if ($LASTEXITCODE -ne 1) { Write-Host "FAIL: mutant $($gm[0]) passed the numpy check" -ForegroundColor Red; $ok = $false }
    else { Write-Host "ok: mutant $($gm[0]) fails the numpy check" }
}

# Source checks. Each returns a list of problems; empty means the source is as required.
function Test-ProxySource([string]$text) {
    $problems = @()
    foreach ($key in 'QueryTraceEvery', 'PresentGrabFrames') {
        $reads = @([regex]::Matches($text, "GetPrivateProfileIntW\(L`"Proxy`", L`"$key`", (\w+), iniPath\)") |
                   ForEach-Object { $_.Groups[1].Value })
        if ($reads.Count -eq 0) { $problems += "$key is never read" }
        foreach ($d in $reads) { if ($d -ne '0') { $problems += "$key defaults to $d, not 0" } }
    }
    if ($text -notmatch 'g_queryTrace\.SetEvery\(proxy::g_queryProbe\.Enabled\(\) \? queryTraceEvery : 0\)') {
        $problems += 'the trace is not tied to QueryStatsFrames (the Issue hook and gameRead marks)'
    }
    foreach ($kind in 'draw', 'indexed', 'up', 'indexed-up') {
        $dropped = [regex]::Matches($text, "QueryTraceDraw\(self, false, `"$kind`",").Count
        $sent = [regex]::Matches($text, "QueryTraceDraw\(self, (true|!fadeGuard\.dropDraw), `"$kind`",").Count
        if ($dropped -ne 1 -or $sent -ne 1) { $problems += "the $kind hook reports dropped $dropped, sent $sent times (want 1 and 1)" }
    }
    if ($text -notmatch '(?s)void QueryTraceDraw\([^)]*\) \{\s*if \(!proxy::g_queryTrace\.TracingFrame\(\)\) return;') {
        $problems += 'QueryTraceDraw does not return at once on untraced frames'
    }
    foreach ($fn in 'QueryTraceIssue', 'QueryTracePixels') {
        if ($text -notmatch "(?s)void $fn\([^)]*\) \{\s*if \(!proxy::g_queryTrace\.Enabled\(\)\) return;") {
            $problems += "$fn does not return at once while the trace is off"
        }
    }
    $problems
}
function Test-FilterSource([string]$text) {
    $problems = @()
    if ($text -notmatch 'PresentModernSceneToComposite\(device, hook, g_rt0\);\s*GrabFrame\(device, g_rt0, "composite"\);') {
        $problems += 'the composite grab does not follow PresentModernSceneToComposite'
    }
    if ($text -notmatch '(?s)g_gateDepthDumpedThisFrame = false;\s*GrabFrame\(device, CurrentBackbuffer\(device\), "present"\);\s*g_presentGrab\.EndFrame\(\);\s*\}') {
        $problems += 'the present grab and the grab counter do not end FilterEndFrameBeforePresent'
    }
    if ($text -notmatch 'g_pendingGateDumpTag = next;\s*g_presentGrab\.Start\(next\);') {
        $problems += 'a new GateDump tag does not start the grabs'
    }
    if ($text -notmatch '(?s)void GrabFrame\([^)]*\) \{\s*if \(!g_presentGrab\.Active\(\)\) return;') {
        $problems += 'GrabFrame does not return at once while no grab is running'
    }
    $problems
}

$proxyText = Get-Content -Raw (Join-Path $unified 'd3d9_proxy.cpp')
$filterText = Get-Content -Raw (Join-Path $unified 'draw_filter.cpp')
$p = @(Test-ProxySource $proxyText) + @(Test-FilterSource $filterText)
if ($p.Count) { $p | ForEach-Object { Write-Host "FAIL: $_" -ForegroundColor Red }; $ok = $false }
else { Write-Host 'ok: keys default to 0, trace tied to X1, all draw paths reported, entry points return while off, grab sites in place' }

$fakes = [ordered]@{
    'QueryTraceEvery defaults to 15'        = { Test-ProxySource ($proxyText -replace 'L"QueryTraceEvery", 0,', 'L"QueryTraceEvery", 15,') }
    'PresentGrabFrames defaults to 8'       = { Test-ProxySource ($proxyText -replace 'L"PresentGrabFrames", 0,', 'L"PresentGrabFrames", 8,') }
    'trace on without QueryStatsFrames'     = { Test-ProxySource ($proxyText -replace 'proxy::g_queryProbe\.Enabled\(\) \? queryTraceEvery : 0', 'queryTraceEvery') }
    'up hook forgets the dropped path'      = { Test-ProxySource ($proxyText -replace 'QueryTraceDraw\(self, false, "up",[^\n]*\n', "`n") }
    'indexed hook ignores the fade guard'   = { Test-ProxySource ($proxyText -replace 'QueryTraceDraw\(self, !fadeGuard\.dropDraw, "indexed",', 'QueryTraceDraw(self, true, "indexed2",') }
    'QueryTraceDraw works on every frame'   = { Test-ProxySource ($proxyText -replace 'if \(!proxy::g_queryTrace\.TracingFrame\(\)\) return;', '') }
    'QueryTracePixels works while off'      = { Test-ProxySource ($proxyText -replace '(void QueryTracePixels\([^)]*\) \{\s*)if \(!proxy::g_queryTrace\.Enabled\(\)\) return;', '$1') }
    'composite grab removed'                = { Test-FilterSource ($filterText -replace 'GrabFrame\(device, g_rt0, "composite"\);', '') }
    'grab counter never advances'           = { Test-FilterSource ($filterText -replace 'g_presentGrab\.EndFrame\(\);', '') }
    'GateDump tag does not start grabs'     = { Test-FilterSource ($filterText -replace 'g_presentGrab\.Start\(next\);', '') }
    'GrabFrame grabs while off'             = { Test-FilterSource ($filterText -replace 'if \(!g_presentGrab\.Active\(\)\) return;', '') }
}
foreach ($name in $fakes.Keys) {
    $found = @(& $fakes[$name])
    if ($found.Count -eq 0) { Write-Host "FAIL: the source check does not catch: $name" -ForegroundColor Red; $ok = $false }
    else { Write-Host "ok: the source check catches: $name" }
}

if ($ok) { Write-Host 'query trace test: PASS' -ForegroundColor Green; exit 0 }
Write-Host 'query trace test: FAIL' -ForegroundColor Red
exit 1
