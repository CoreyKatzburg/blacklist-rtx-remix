# Every offline (game-free) check of the Remix overhaul, in one command. Exit 0 only if all of them pass.
# Runs everything even after a failure and prints a summary. Tests are found by path, so new ones join automatically:
#   C++ replay suite         tests\verify_proxy_tests.cpp (+ play-setup\unified-build sources), built x64 and run
#   config / lights / PBR    tests\verify_config.py, tests\verify_lights_and_materials.py (--self-test)
#   materials                tests\verify_materials_*.py
#   Python unit tests        test_*.py under tests\remix_source and tests\runtime (unittest discovery per folder)
#   PowerShell tests         tests\runtime\test_*.ps1 (run with Windows PowerShell 5.1, like the desktop shortcuts)
#   folder runners           tests\<folder>\run_*.ps1 (e.g. remix_source: Track B's C++ and telemetry checks)
#   script parse check       every .ps1 in play-setup and tests (PowerShell parser, no execution)
#   runtime gate dry run     covered by tests\runtime\test_gate.py (good fake run passes, every broken one fails)
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$remixDir = Split-Path -Parent $testsDir
$unifiedDir = Join-Path $remixDir 'play-setup\unified-build'
$py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
$results = [System.Collections.Generic.List[object]]::new()
$sw = [Diagnostics.Stopwatch]::StartNew()

function Step($name, [scriptblock]$body) {
    Write-Host "== $name" -ForegroundColor Cyan
    $t = [Diagnostics.Stopwatch]::StartNew()
    $ok = $false; $note = ''
    try { $ok = [bool](& $body) } catch { $note = $_.Exception.Message }
    if (-not $ok -and -not $note) { $note = "exit $LASTEXITCODE" }
    $results.Add([pscustomobject]@{ Check = $name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Seconds = [math]::Round($t.Elapsed.TotalSeconds, 1); Note = $note })
    Write-Host ("   {0} ({1:N1} s) {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $t.Elapsed.TotalSeconds, $note) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}
function Native { param([string]$exe, [string[]]$arguments) & $exe @arguments | Out-Host; $LASTEXITCODE -eq 0 }

Step 'C++ replay suite (build + run)' {
    $exePath = Join-Path $testsDir 'verify_proxy_tests.exe'
    $cmd = "call `"$vcvars`" x64 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc `"$testsDir\verify_proxy_tests.cpp`" `"$unifiedDir\shader_traits.cpp`" `"$unifiedDir\camera_solver.cpp`" `"$unifiedDir\pass_graph.cpp`" /Fe:`"$exePath`" /Fo:`"$testsDir\\`" /link /NOLOGO"
    & $env:ComSpec /d /s /c $cmd | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "compile failed (exit $LASTEXITCODE)" }
    Native $exePath @()
}
$gateLock = Join-Path $remixDir 'play-setup\AGENT_GAME_LOCK.txt'
foreach ($name in 'verify_config.py', 'verify_lights_and_materials.py') {
    Step $name { Native $py @((Join-Path $testsDir $name), '--self-test') }
    # A gate run (verify_runtime.ps1) changes play-setup\d3d9_proxy.ini while it holds the game lock and puts it back
    # at the end: a verify_config failure then is the run's temporary ini, not settings left behind (Nov 16).
    if ($name -eq 'verify_config.py' -and $results[-1].Result -eq 'FAIL' -and (Test-Path $gateLock)) {
        $results[-1].Note = "a gate run holds the game lock ($((Get-Content $gateLock -Raw).Trim())), the setup ini is temporarily changed; rerun after it ends. $($results[-1].Note)"
        Write-Host "   $($results[-1].Note)" -ForegroundColor Yellow
    }
}
foreach ($rel in 'analysis\brightness\pfm_brightness.py', 'analysis\brightness\align_sessions.py', 'analysis\brightness\flicker_audit.py', 'analysis\compile_wait.py', 'analysis\fog_fade.py', 'analysis\crushed_blown_vs_raster.py', 'analysis\glow_blobs.py', 'analysis\freeze_scan.py', 'analysis\picture_cost.py') {
    $p = Join-Path $testsDir $rel
    if (Test-Path $p) { Step (Split-Path $rel -Leaf) { Native $py @($p, '--self-test') } }
}
foreach ($file in Get-ChildItem $testsDir -Filter 'verify_materials_*.py' | Sort-Object Name) {
    Step $file.Name { Native $py @($file.FullName) }
}
foreach ($dir in 'remix_source', 'runtime') {
    $folder = Join-Path $testsDir $dir
    if (Get-ChildItem $folder -Filter 'test_*.py' -ErrorAction SilentlyContinue) {
        Step "Python unit tests: tests\$dir" { Native $py @('-m', 'unittest', 'discover', '-s', $folder, '-t', $folder, '-p', 'test_*.py') }
    }
}
foreach ($file in Get-ChildItem (Join-Path $testsDir 'runtime') -Filter 'test_*.ps1' -ErrorAction SilentlyContinue) {
    Step "PowerShell test: $($file.Name)" { Native 'powershell.exe' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $file.FullName) }
}
# A folder's own runner (e.g. tests\remix_source\run_remix_source_tests.ps1 builds and runs Track B's C++ checks).
$hostExe = (Get-Process -Id $PID).Path
foreach ($file in Get-ChildItem $testsDir -Directory | ForEach-Object { Get-ChildItem $_.FullName -Filter 'run_*.ps1' } | Sort-Object FullName) {
    Step "Runner: $($file.Directory.Name)\$($file.Name)" { Native $hostExe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $file.FullName) }
}
Step 'PowerShell parse check (play-setup, tests)' {
    $bad = @()
    foreach ($file in @(Get-ChildItem (Join-Path $remixDir 'play-setup') -Filter '*.ps1') + @(Get-ChildItem $testsDir -Filter '*.ps1' -Recurse)) {
        $errs = $null
        [void][System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$null, [ref]$errs)
        if ($errs) { $bad += "$($file.Name): $($errs[0].Message)" }
    }
    if ($bad) { throw ($bad -join '; ') }
    $true
}

$sw.Stop()
$results | Format-Table -AutoSize | Out-Host
$failed = @($results | Where-Object Result -ne 'PASS')
if ($failed.Count) {
    Write-Host ("OFFLINE GATES FAILED: {0} of {1} checks ({2:N1} s)." -f $failed.Count, $results.Count, $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    exit 1
}
Write-Host ("ALL {0} OFFLINE GATES PASSED in {1:N1} s." -f $results.Count, $sw.Elapsed.TotalSeconds) -ForegroundColor Cyan
exit 0
