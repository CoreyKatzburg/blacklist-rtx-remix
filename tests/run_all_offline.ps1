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
# Each step's console output (stdout and stderr) also goes to a file; a failed step keeps it there, a passing one deletes
# it. Out-Host alone left nothing behind when test_installer.ps1 failed once and passed on the rerun (Nov 17 10:37).
$logDir = Join-Path (Split-Path (Split-Path $remixDir)) ('experiments\remix-gate\offline_suite_logs\' + (Get-Date -Format 'yyyyMMdd-HHmmss'))

function Step($name, [scriptblock]$body) {
    Write-Host "== $name" -ForegroundColor Cyan
    $t = [Diagnostics.Stopwatch]::StartNew()
    $ok = $false; $note = ''
    [void](New-Item -ItemType Directory -Force $logDir)
    $script:stepLog = Join-Path $logDir (($name -replace '[^\w.-]+', '_') + '.log')
    Set-Content -LiteralPath $script:stepLog -Value "== $name $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
    try { $ok = [bool](& $body) } catch { $note = $_.Exception.Message }
    if (-not $ok -and -not $note) { $note = "exit $LASTEXITCODE" }
    if ($ok) { Remove-Item -LiteralPath $script:stepLog } else {
        Add-Content -LiteralPath $script:stepLog -Value "== FAIL: $note"
        $note = "$note (output in $script:stepLog)"
    }
    $results.Add([pscustomobject]@{ Check = $name; Result = $(if ($ok) { 'PASS' } else { 'FAIL' }); Seconds = [math]::Round($t.Elapsed.TotalSeconds, 1); Note = $note })
    Write-Host ("   {0} ({1:N1} s) {2}" -f $(if ($ok) { 'PASS' } else { 'FAIL' }), $t.Elapsed.TotalSeconds, $note) -ForegroundColor $(if ($ok) { 'Green' } else { 'Red' })
}
function Native {
    param([string]$exe, [string[]]$arguments)
    $ErrorActionPreference = 'Continue'   # Windows PowerShell 5.1 would turn a stderr line into a stopping error
    & $exe @arguments 2>&1 | ForEach-Object { "$_"; Add-Content -LiteralPath $script:stepLog -Value "$_" } | Out-Host
    $LASTEXITCODE -eq 0
}

Step 'C++ replay suite (build + run)' {
    $exePath = Join-Path $testsDir 'verify_proxy_tests.exe'
    $cmd = "call `"$vcvars`" x64 >nul && cl.exe /nologo /std:c++17 /O2 /MT /W4 /WX /EHsc `"$testsDir\verify_proxy_tests.cpp`" `"$unifiedDir\shader_traits.cpp`" `"$unifiedDir\camera_solver.cpp`" `"$unifiedDir\pass_graph.cpp`" /Fe:`"$exePath`" /Fo:`"$testsDir\\`" /link /NOLOGO"
    & $env:ComSpec /d /s /c "$cmd 2>&1" | ForEach-Object { "$_"; Add-Content -LiteralPath $script:stepLog -Value "$_" } | Out-Host
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
foreach ($rel in 'analysis\brightness\pfm_brightness.py', 'analysis\brightness\align_sessions.py', 'analysis\brightness\flicker_audit.py', 'analysis\compile_wait.py', 'analysis\fog_fade.py', 'analysis\crushed_blown_vs_raster.py', 'analysis\glow_blobs.py', 'analysis\freeze_scan.py', 'analysis\picture_cost.py', 'analysis\bench_bump.py', 'analysis\monitor_steps.py', 'analysis\remix_draw_log.py', 'analysis\capture\cap_join_material.py', 'analysis\capture\cap_replace.py', 'analysis\capture\cap_albedo_per_draw.py', 'analysis\capture\p1_insideout_depth.py', 'analysis\capture\switch_owner_blocks.py', 'runtime\stall_windows.py') {
    $p = Join-Path $testsDir $rel
    if (Test-Path $p) { Step (Split-Path $rel -Leaf) { Native $py @($p, '--self-test') } }
}
# Live, no game: the Amp-window watchdog a launch starts must open no window and leave the focus alone. In
# 20261117-122718 its console window took the focus from the game and the mouse calibration read no turn.
Step 'amp_window.py --quiet-check' { Native $py @((Join-Path $testsDir 'runtime\amp_window.py'), '--quiet-check') }
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
# Record this run for verify_runtime.ps1's manifest (offlineSuite.record): when it ended, whether it passed, and the tree
# it ran on. lastPass keeps the newest passing run, so a later failure on the same tree still shows.
$record = Join-Path (Split-Path (Split-Path $remixDir)) 'experiments\remix-gate\offline_suite_last.json'
$hashOf = { param($p) if (Test-Path -LiteralPath $p) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash } }
# remixSourceCommit: the dxvk-remix checkout; remixInstalledCommit: the build pkg holds (verify_runtime.ps1's own
# Get-InstalledRemixCommit, loaded from that file so both read the same rule).
$vrAst = [Management.Automation.Language.Parser]::ParseFile((Join-Path $testsDir 'verify_runtime.ps1'), [ref]$null, [ref]$null)
. ([scriptblock]::Create($vrAst.Find({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and
    $a.Name -eq 'Get-InstalledRemixCommit' }, $true).Extent.Text))
$run = [ordered]@{ finished = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'); passed = (-not $failed.Count); checks = $results.Count
    failed = @($failed | ForEach-Object Check); toolsCommit = (git -C $remixDir rev-parse --short HEAD)
    toolsModified = @(git -C $remixDir status --porcelain --untracked-files=no)
    remixSourceCommit = (git -C (Join-Path $remixDir 'src\dxvk-remix') rev-parse --short HEAD)
    remixInstalledCommit = (Get-InstalledRemixCommit (Join-Path $remixDir 'play-setup\STAGED_REMIX.json') (Join-Path $remixDir 'pkg\.trex\d3d9.dll'))
    proxySha256 = (& $hashOf (Join-Path $remixDir 'play-setup\d3d9.dll')); runtimeSha256 = (& $hashOf (Join-Path $remixDir 'pkg\.trex\d3d9.dll')) }
$old = if (Test-Path -LiteralPath $record) { Get-Content -LiteralPath $record -Raw | ConvertFrom-Json }
[ordered]@{ lastRun = $run; lastPass = $(if ($run.passed) { $run } elseif ($old) { $old.lastPass }) } |
    ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $record
Write-Host "Recorded in $record"
if ($failed.Count) {
    Write-Host ("OFFLINE GATES FAILED: {0} of {1} checks ({2:N1} s)." -f $failed.Count, $results.Count, $sw.Elapsed.TotalSeconds) -ForegroundColor Red
    exit 1
}
Write-Host ("ALL {0} OFFLINE GATES PASSED in {1:N1} s." -f $results.Count, $sw.Elapsed.TotalSeconds) -ForegroundColor Cyan
exit 0
