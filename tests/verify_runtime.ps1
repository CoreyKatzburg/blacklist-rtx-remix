# Runtime gate for the Blacklist RTX Remix build: one command, numeric PASS/FAIL, exit code 0 only on PASS.
#   verify_runtime.ps1                     installs the current build (play-setup + pkg + materials), then for each of the
#                                          five spots in runtime\spots.json: restores that spot's saved checkpoint,
#                                          launches the game straight into it, and runs runtime\drive.py (24-stop sweep,
#                                          360 pan, goggle check; camera set in game memory). Then runtime\gate.py judges
#                                          everything and writes result.json + result.md in the run folder.
#   verify_runtime.ps1 -Spots a,b          only those spots
#   verify_runtime.ps1 -DryRun             no game: fake runs (good + every broken case) through the same gate
#   verify_runtime.ps1 -CaptureCheckpoint <spot>
#                                          records a spot: run it right after the game loaded the checkpoint you want
#                                          (Sam standing at the spawn, untouched). Saves the save files + map, spawn
#                                          position and camera direction to the checkpoint library.
#   verify_runtime.ps1 -CaptureCheckpoint <name> -Launch
#                                          launches the game from the saves in the save folder now (no restore),
#                                          records where they load (map, spawn, camera) and copies them into the run
#                                          folder's capture-<name>\. The library is not touched: copy the folder
#                                          into gate-checkpoints\<name> once its map is the one wanted.
#   verify_runtime.ps1 -CaptureCheckpoint <name> -Launch -SaveFrom <folder>
#                                          the same, launched from the .save files in <folder> (a backup) instead;
#                                          the save folder is put back afterwards as always.
#   verify_runtime.ps1 -Evaluate <run folder>   re-judge an existing run
#   -Previous <run folder> / -Reference <run folder>   what gate.py compares against (default: newest earlier run with
#                                          the same spots, and newest reference-* no-Remix run)
#   verify_runtime.ps1 -NoRemix -Spots a,b the original game as reference, in experiments\remix-gate\reference-<stamp>:
#                                          the proxy stays (camera log, frame timing) but chains Windows' own d3d9
#                                          (ChainDll) with every rewrite off, so Remix and its bridge never load. Same
#                                          spots, stops, mouse lag and cutscene recording; each still stop saves
#                                          gate\<tag>_raster.pfm (the screen, display-encoded 0..1) for worker F2's
#                                          brightness limits. How Remix was turned off is in reference.json.
#   verify_runtime.ps1 -Variants runtime\settings_sweep_custodial.json
#                                          settings sweep, in experiments\remix-gate\<stamp>-settings\<variant>\: one
#                                          clean launch per variant at the file's spot, each with one rtx.conf or proxy
#                                          ini change; drive.py only holds y0_p0 (frame times, nvidia-smi video memory
#                                          in gpu_mem.csv, one GateDump). Every config is put back after each launch.
#                                          variant.json says whether remix-dxvk.log lists each changed rtx key with
#                                          its new value; settings_report.py compares the variants. No gate verdict.
#   verify_runtime.ps1 -RestoreFrom <run folder>
#                                          puts back the files a killed run changed (inis, rtx.conf, saves) from the
#                                          copies in <run>\file-backup, the launcher setting and the DX11 mod, and
#                                          removes the game lock if that run took it. A run refuses to start while
#                                          the setup ini still carries a run's marker line (runtime\run_files.ps1).
# In Remix runs drive.py also saves <tag>_raster.pfm with ray tracing switched off for the stops in "rasterStops"
# (default y0_p0), which gate.py compares with Remix's picture.
# A spot in spots.json may name another spot's saves with "checkpoint" and move Sam elsewhere with "teleport"
# ({"location": [x, y, z], "yaw": deg}); drive.py fails the spot when the game does not keep him there.
# Needs: nobody else using the game (takes play-setup\AGENT_GAME_LOCK.txt), Steam running. Works with the monitors
# off (screen frames come from gdigrab; the camera is set in memory). Only the goggle keys and the title-screen
# keys need the game window in front, which this script does itself.
param([string[]]$Spots = @(), [switch]$DryRun, [string]$CaptureCheckpoint = '', [switch]$Launch, [string]$Evaluate = '', [switch]$SkipInstall, [string]$SaveFrom = '',
      [string]$Previous = '', [string]$Reference = '', [switch]$NoRemix, [string]$Variants = '', [string]$RestoreFrom = '')
$ErrorActionPreference = 'Stop'
$Tests = $PSScriptRoot
$Runtime = Join-Path $Tests 'runtime'
$Py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
$Library = 'D:\BlacklistOverhaul\experiments\save-backups\gate-checkpoints'
$SaveDir = 'D:\Ubisoft\Ubisoft Game Launcher\savegames\d36412d1-6246-4703-adbf-8fbd46c6da34\449'
$InputIni = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Blacklist\inputTranslations.ini'
$GameLock = Join-Path (Split-Path $Tests) 'play-setup\AGENT_GAME_LOCK.txt'
$ResultsRoot = 'D:\BlacklistOverhaul\experiments\remix-gate'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# gate.py compares a run with the previous run (FPS) and with the no-Remix reference (lag, brightness). Unless given,
# --previous is the newest earlier Remix run whose manifest lists the same spots, and --reference the newest
# reference-* run (no Remix) earlier than this one.
function Get-RunSpots($folder) {
    $m = Join-Path $folder 'manifest.json'
    if (Test-Path $m) { @((Get-Content $m -Raw | ConvertFrom-Json).spots | Sort-Object) } else { @() }
}
# The no-Remix reference, per spot: the newest earlier reference-* run that finished (has result.json) and holds that
# spot without an error (coordinator review, Nov 16: a newer broken reference hid an older good one). When the spots
# come from different runs, <run>\reference-chosen holds a junction per spot and a manifest naming each source.
function Select-Reference($folder, $stamp) {
    $refs = @(Get-ChildItem $ResultsRoot -Directory -Filter 'reference-*' | Where-Object {
        ($_.Name -replace '^reference-', '') -lt $stamp -and (Test-Path (Join-Path $_.FullName 'result.json')) } | Sort-Object Name -Descending)
    $chosen = [ordered]@{}
    foreach ($spot in Get-RunSpots $folder) {
        # ...and whose reference pictures are pictures: the no-Remix runs of Nov 16 04:58-06:22 grabbed only black
        # frames (every <tag>_raster.json luma 0), and the next Remix run was scored against them.
        $hit = $refs | Where-Object {
            $sj = Join-Path $_.FullName "$spot\spot.json"
            $rasters = @(Get-ChildItem (Join-Path $_.FullName "$spot\gate") -Filter '*_raster.json' -ErrorAction SilentlyContinue)
            (Get-RunSpots $_.FullName) -contains $spot -and (Test-Path $sj) -and -not (Get-Content $sj -Raw | ConvertFrom-Json).error -and
                $rasters.Count -and -not ($rasters | Where-Object { [double](Get-Content $_.FullName -Raw | ConvertFrom-Json).luma[2] -le 0 })
        } | Select-Object -First 1
        if ($hit) { $chosen[$spot] = $hit.FullName }
    }
    $sources = @($chosen.Values | Select-Object -Unique)
    if (-not $sources.Count) { return $null }
    if ($sources.Count -eq 1) { return $sources[0] }
    $merged = Join-Path $folder 'reference-chosen'
    if (Test-Path $merged) {   # a Gate rerun: delete only the links (a non-recursive delete never touches a target)
        Get-ChildItem $merged -Directory | ForEach-Object { [IO.Directory]::Delete($_.FullName, $false) }
    }
    New-Item -ItemType Directory -Force $merged | Out-Null
    foreach ($spot in $chosen.Keys) { New-Item -ItemType Junction -Path (Join-Path $merged $spot) -Target (Join-Path $chosen[$spot] $spot) | Out-Null }
    @{ spots = @($chosen.Keys); chosenFrom = $chosen } | ConvertTo-Json | Set-Content (Join-Path $merged 'manifest.json')
    $merged
}
function Gate($folder) {
    $name = Split-Path $folder -Leaf
    $stamp = $name -replace '^reference-', ''
    $spotsHere = (Get-RunSpots $folder) -join ','
    # a reference run is compared only with earlier reference runs, a Remix run only with earlier Remix runs
    $kind = if ($name -like 'reference-*') { '^reference-\d{8}-\d{6}$' } else { '^\d{8}-\d{6}$' }
    $prev = if ($Previous) { $Previous } else {
        Get-ChildItem $ResultsRoot -Directory | Where-Object { $_.Name -match $kind -and ($_.Name -replace '^reference-', '') -lt $stamp -and
            (Test-Path (Join-Path $_.FullName 'result.json')) -and ((Get-RunSpots $_.FullName) -join ',') -eq $spotsHere } |
            Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName }
    $ref = if ($Reference) { $Reference } elseif ($name -like 'reference-*') { $null } else { Select-Reference $folder $stamp }
    $gateArgs = @((Join-Path $Runtime 'gate.py'), $folder)
    if ($prev) { $gateArgs += @('--previous', $prev) }
    if ($ref) { $gateArgs += @('--reference', $ref) }
    Write-Host "  gate: previous run $(if ($prev) { $prev } else { '(none with the same spots)' }); no-Remix reference $(if ($ref) { $ref } else { '(none)' })"
    & $Py @gateArgs | Out-Host
    $code = $LASTEXITCODE
    # The FPS check against --previous is only like for like when both runs used the same builds: say so when not.
    $note = Get-BuildNote $folder $prev
    if ($note) {
        Write-Host "  gate: $note" -ForegroundColor Yellow
        $md = Join-Path $folder 'result.md'
        if (Test-Path $md) { Add-Content $md "`r`n**Build note:** $note" }
    }
    return $code
}
function Get-RunBuild($folder) {
    $m = Join-Path $folder 'manifest.json'
    if (Test-Path $m) { (Get-Content $m -Raw | ConvertFrom-Json).build } else { $null }
}
function Get-BuildNote($folder, $prev) {
    if (-not $prev) { return $null }
    $a, $b = (Get-RunBuild $folder), (Get-RunBuild $prev)
    if (-not $a -or -not $b) { return "the previous run $(Split-Path $prev -Leaf) and this run cannot be tied to builds (no build in a manifest), so the FPS comparison may mix builds." }
    $diff = @(foreach ($k in 'remixRuntimeSha256', 'remixClientSha256', 'proxySha256') { if ($a.$k -ne $b.$k) { $k } })
    if ($diff.Count) { return "the previous run $(Split-Path $prev -Leaf) used other builds ($($diff -join ', ') differ; dxvk-remix $($b.remixCommit) then, $($a.remixCommit) now), so the FPS comparison measures the build change too." }
    $null
}

if ($DryRun) {
    $out = Join-Path $ResultsRoot "dryrun-$Stamp"
    & $Py (Join-Path $Runtime 'fakerun.py') $out
    exit $LASTEXITCODE
}
if ($Evaluate) { exit (Gate $Evaluate) }

. (Join-Path (Split-Path $Tests) 'play-setup\RemixPlayCommon.ps1')
. (Join-Path $Runtime 'run_files.ps1')
# Puts back what a run changed: each step on its own, so one failure cannot skip the rest. Returns the failures.
function Restore-AfterRun([string]$runDir) {
    $failed = @()
    try { $failed += @(Restore-RunFiles $runDir) } catch { $failed += "files: $($_.Exception.Message)" }
    try { $r = Restore-LauncherApi; if ($r) { Write-Step "Launcher setting put back: $r" } } catch { $failed += "launcher setting: $($_.Exception.Message)" }
    try { if (Resume-Dx11Mod) { Write-Step 'DX11 lighting mod put back.' } } catch { $failed += "DX11 lighting mod: $($_.Exception.Message)" }
    $failed
}
function Show-PutBackFailures($failed) {
    if (-not @($failed).Count) { return }
    Write-Host "  NOT PUT BACK ($(@($failed).Count)):" -ForegroundColor Red
    foreach ($f in $failed) { Write-Host "    $f" -ForegroundColor Red }
}
if ($RestoreFrom) {
    $running = Get-Process Blacklist_game, Blacklist_DX11_game, Blacklist_Launcher, NvRemixBridge -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($running) { throw "$($running.Name) is running (pid $($running.Id)): close it first." }
    $failed = @(Restore-AfterRun $RestoreFrom)
    if ((Test-Path $GameLock) -and (Get-Content $GameLock -Raw).Contains($RestoreFrom)) { Remove-Item -LiteralPath $GameLock; Write-Step 'Game lock of that run removed.' }
    Show-PutBackFailures $failed
    if ($failed.Count) { exit 3 }
    Write-Step "Put back from $RestoreFrom; play-setup mismatches: $(@(Get-PlayMismatches) -join ', ')"
    exit 0
}
$config = Get-Content (Join-Path $Runtime 'spots.json') -Raw | ConvertFrom-Json
$hashArgs = @($GameExeHashes | ForEach-Object { '--exe-hash'; $_ })

if ($CaptureCheckpoint -and -not $Launch) {
    if (-not ($config.spots | Where-Object name -eq $CaptureCheckpoint)) { throw "No spot '$CaptureCheckpoint' in runtime\spots.json" }
    $game = Get-Process Blacklist_game -ErrorAction Stop | Select-Object -First 1
    $dest = Join-Path $Library $CaptureCheckpoint
    New-Item -ItemType Directory -Force $dest | Out-Null
    & $Py (Join-Path $Runtime 'drive.py') capture --pid $game.Id @hashArgs --out (Join-Path $dest 'checkpoint.json')
    if ($LASTEXITCODE) { throw 'Could not read the camera/map from the game (in a menu? still loading?).' }
    Get-ChildItem $dest -Filter '*.save' | Remove-Item
    Copy-Item (Join-Path $SaveDir '*.save') $dest
    "Checkpoint '$CaptureCheckpoint' recorded in $dest"
    exit 0
}

Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices; using System.Threading;
public static class GateInput {
  [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; public uint pad1, pad2; }
  [StructLayout(LayoutKind.Explicit)] struct INPUT { [FieldOffset(0)] public uint type; [FieldOffset(8)] public KEYBDINPUT ki; }
  [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] inputs, int size);
  [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int cmd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
  [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool attach);
  [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
  public static bool Front(IntPtr h) {
    var fg = GetForegroundWindow(); if (fg == h) return true;
    uint a = GetWindowThreadProcessId(fg, IntPtr.Zero), me = GetCurrentThreadId();
    AttachThreadInput(me, a, true); ShowWindow(h, 9); SetForegroundWindow(h); AttachThreadInput(me, a, false);
    Thread.Sleep(200); return GetForegroundWindow() == h; }
  public static void Tap(ushort scan) {
    foreach (uint up in new uint[] { 0, 2 }) { var i = new INPUT(); i.type = 1; i.ki.scan = scan; i.ki.flags = 0x8u | up;
      SendInput(1, new[] { i }, Marshal.SizeOf(typeof(INPUT))); Thread.Sleep(100); } }
}
"@

# Also waits for the Play script of the previous launch to finish (it waits 30 s after the game quits, then puts the
# launcher setting back to DX11; overlapping the next launch with that would start the DX11 exe).
$script:PlayProcess = $null
# A process that exits between Get-Process and the kill is fine (Stop-Process and Kill() threw there before).
function Stop-Game {
    Get-Process Blacklist_game, NvRemixBridge -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ((Get-RunningGame) -and $t.Elapsed.TotalSeconds -lt 30) { Start-Sleep 1 }
    if ($script:PlayProcess) {
        try { if (-not $script:PlayProcess.WaitForExit(120000)) { $script:PlayProcess.Kill() } } catch { Write-Step "Play script: $($_.Exception.Message)" }
    }
    $script:PlayProcess = $null
}

# A level is rendering in game process $id: with Remix, the proxy logged that ray tracing switched on in it; without
# Remix (no such line), the proxy's frame log has a real camera, which the menus never publish (Benghazi, Nov 15:
# 2527 menu frames without one). The frame log is new for each launch (the Play script moves the old one away).
function Test-LevelLive($id) {
    if (-not $NoRemix) {
        return [bool](Select-String -LiteralPath "$S\d3d9_proxy.log" -Pattern "\[pid $id\] lightpub: AutoRaytracing -> on" -Quiet -ErrorAction SilentlyContinue)
    }
    foreach ($line in @(Get-Content -LiteralPath "$S\d3d9_proxy_frames.csv" -Tail 40 -ErrorAction SilentlyContinue)) {
        $fields = $line -split ','
        if ($fields.Count -lt 7) { continue }
        foreach ($pub in $fields[6] -split '\|') {   # the same rule as cinematic.py camera_row
            $p = $pub -split ':'
            if ($p.Count -ge 9 -and $p[2] -eq '0') {
                if (@($p[3..5] | Where-Object { [math]::Abs(($_ -as [double])) -gt 1e-3 }).Count) { return $true }
                break
            }
        }
    }
    $false
}

# Steam -> launcher -> game; then Space, Space, Enter (title, "press any key", "resume at checkpoint") until
# Test-LevelLive says a level is rendering. Never Esc (quits from menus).
function Start-GameAtCheckpoint {
    $log = "$S\d3d9_proxy.log"
    # Windows PowerShell started from PowerShell 7 inherits 7's PSModulePath and then has no Get-FileHash, so the Play
    # script died silently and the game never started (Nov 15). Give it its own module path and keep its output.
    $playLog = Join-Path $runDir "play-$(Get-Date -Format HHmmss).log"
    $savedModulePath = $env:PSModulePath; $env:PSModulePath = "$env:windir\system32\WindowsPowerShell\v1.0\Modules"
    try {
        $script:PlayProcess = Start-Process powershell.exe -PassThru -WindowStyle Hidden -RedirectStandardOutput $playLog -RedirectStandardError "$playLog.err" `
            -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$(Join-Path $Setup 'Play_Blacklist_with_Remix.ps1')`"", '-NoPause'
    } finally { $env:PSModulePath = $savedModulePath }
    $t = [Diagnostics.Stopwatch]::StartNew(); $loginTried = $false
    while (-not ($game = Get-Process Blacklist_game -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        if ($script:PlayProcess.HasExited) {
            throw "The Play script exited ($($script:PlayProcess.ExitCode)) before the game started: " +
                ((@(Get-Content $playLog, "$playLog.err" -ErrorAction SilentlyContinue) | Select-Object -Last 3) -join ' | ')
        }
        if ($t.Elapsed.TotalSeconds -gt 300) { throw 'The game did not start within 5 minutes.' }
        if (-not $loginTried -and $t.Elapsed.TotalSeconds -gt 60 -and (Get-Process UbisoftGameLauncher -ErrorAction SilentlyContinue)) {
            $loginTried = $true
            & (Join-Path (Split-Path $Tests) 'stutter-recorder\ubi_login.ps1') | Out-Null   # password from 1Password, never printed
        }
        Start-Sleep 2
    }
    Start-Sleep 12; $k = 0
    while (-not (Test-LevelLive $game.Id)) {
        if ($t.Elapsed.TotalSeconds -gt 480) { throw 'The game did not reach the level within 8 minutes.' }
        if ($game.HasExited) {
            # The next launch overwrites the Remix and bridge logs: keep them (Nov 16 07:07, a launch died inside
            # CreateDevice and nothing showed why).
            $keep = Join-Path $runDir "load-failure-$($game.Id)"
            New-Item -ItemType Directory -Force $keep | Out-Null
            Copy-Item "$S\rtx-remix\logs\*.log" $keep -ErrorAction SilentlyContinue
            Get-Content $log -Tail 20000 -ErrorAction SilentlyContinue | Where-Object { $_ -match "\[pid $($game.Id)\]" } |
                Set-Content (Join-Path $keep 'd3d9_proxy.log')
            throw "The game exited while loading (logs in $keep)."
        }
        # Only a bridge started after this game counts: on Nov 16 a bridge from elsewhere stopped con_bright_hall
        # although the proxy log showed the game chained the Windows d3d9.
        $bridge = Get-Process NvRemixBridge -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $game.StartTime } | Select-Object -First 1
        if ($NoRemix -and ((Select-String -LiteralPath $log -Pattern "\[pid $($game.Id)\] chained RTX Remix DLL" -Quiet -ErrorAction SilentlyContinue) -or $bridge)) {
            throw "No-Remix run, but the game loaded Remix (proxy log, or NvRemixBridge $(if ($bridge) { "pid $($bridge.Id) $($bridge.Path) started $($bridge.StartTime)" }))."
        }
        $game.Refresh()
        [void][GateInput]::Front($game.MainWindowHandle)
        [GateInput]::Tap($(if ($k % 3 -eq 2) { 0x1C } else { 0x39 })); Start-Sleep -Milliseconds 1600; ++$k
    }
    Start-Sleep 8
    $game
}

# Binds two spare keys to the game's own goggle commands for the run (the file is put back afterwards). Whether
# they work is checked live: drive.py looks for the proxy's "Goggles: on" log line, else the check is NOT MEASURED.
function Set-GoggleBindings($path, $keys) {
    $text = [IO.File]::ReadAllText($path)
    foreach ($pair in @(@($keys.toggle, 'ToggleVisionMode'), @($keys.cycle, 'ChangeVisionMode'))) {
        $esc = [Regex]::Escape($pair[0])
        $text = [Regex]::Replace($text, "(?m)^$esc=[^\r\n]*(\r?\n|$)", '')
        $text = [Regex]::Replace($text, '(?m)^\[Engine\.Input\](\r?\n)', "`$0$($pair[0])=$($pair[1])`$1")
    }
    if ($text -notmatch "(?m)^$([Regex]::Escape($keys.cycle))=ChangeVisionMode") { throw "No [Engine.Input] section in $path" }
    [IO.File]::WriteAllText($path, $text)
}

$sweep = $null
if ($Variants) {
    if ($NoRemix -or $CaptureCheckpoint) { throw '-Variants measures Remix settings at an existing spot.' }
    $sweep = Get-Content $Variants -Raw | ConvertFrom-Json
    $Spots = @($sweep.spot)
}
$runDir = Join-Path $ResultsRoot $(if ($NoRemix) { "reference-$Stamp" } elseif ($sweep) { "$Stamp-settings" } else { $Stamp })
if ($NoRemix -and $CaptureCheckpoint) { throw '-NoRemix records the reference at existing spots; capture checkpoints with Remix.' }
# rtx.conf for one sweep variant: each key's line replaced, or appended when the baseline does not set it.
function Set-RtxKeys($text, $keys) {
    foreach ($p in @(if ($keys) { $keys.PSObject.Properties })) {
        $line = "$($p.Name) = $($p.Value)"
        $pat = "(?m)^[ \t]*$([Regex]::Escape($p.Name))[ \t]*=[^\r\n]*"
        $text = if ($text -match $pat) { [Regex]::Replace($text, $pat, $line.Replace('$', '$$')) } else { $text.TrimEnd() + "`r`n$line`r`n" }
    }
    $text
}
# [Proxy] keys for one sweep variant, set after the gate's EnableDebuggingTelemetry=1.
function Set-ProxyKeys($text, $keys) {
    foreach ($p in @(if ($keys) { $keys.PSObject.Properties })) {
        $pat = "(?m)^$([Regex]::Escape($p.Name))[ \t]*=[^\r\n]*"
        $text = if ($text -match $pat) { [Regex]::Replace($text, $pat, "$($p.Name)=$($p.Value)") }
                else { [Regex]::Replace($text, '(?m)^\[Proxy\][ \t]*\r?\n', "`$0$($p.Name)=$($p.Value)`r`n", 1) }
        if ($text -notmatch "(?m)^$([Regex]::Escape($p.Name))=$([Regex]::Escape([string]$p.Value))") { throw "Could not set [Proxy] $($p.Name)" }
    }
    $text
}
# -NoRemix: how Remix is turned off. Only this run's copy of the proxy ini changes (put back afterwards like every
# run's ini); the installed files stay as they are. ChainDll makes the proxy hand every call to Windows' 32-bit d3d9,
# so d3d9_remix.dll and the bridge never load; the rest switches off everything the proxy changes in the game's
# drawing for Remix (pass filter, skips, hand-off, texkill alpha, format guard, query shortcut, texture budget cap).
# Kept: the camera log and frame timing (EnableDebuggingTelemetry, PublishCamera) the measurements need.
$NoRemixProxyKeys = [ordered]@{
    ChainDll = 'C:\Windows\SysWOW64\d3d9.dll'; PassFilter = 0; ForwardScene = 0; SkipShadowMapDraws = 0; SkipGameLighting = 0
    SkipGamePost = 0; GogglesGameView = 0; HandoffBeforePost = 0; HandoffBeforeEffects = 0; AlphaTestFromTexkill = 0
    ModernFilter = 0; FormatGuard = 0; QueryMode = 0; TextureStreamBudgetMB = 0
}
function Get-NoRemixIni($text) {
    if ($text -match '(?m)^\[Lights\]') { throw 'The setup proxy ini already has a [Lights] section; -NoRemix would need to merge it.' }
    foreach ($key in $NoRemixProxyKeys.Keys) {
        if ($text -match "(?m)^$key[ \t]*=") { throw "The setup proxy ini already sets $key; -NoRemix would need to merge it." }
    }
    $keys = ($NoRemixProxyKeys.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join "`r`n"
    $out = [Regex]::Replace($text, '(?m)^\[Proxy\][ \t]*\r?\n', "[Proxy]`r`n; verify_runtime.ps1 -NoRemix (reference run)`r`n$keys`r`n", 1)
    if ($out -eq $text) { throw 'No [Proxy] section in the setup proxy ini.' }
    $out.TrimEnd() + "`r`n[Lights]`r`nEnable=0`r`n"
}
$selected = @(if (-not $CaptureCheckpoint) { $config.spots | Where-Object { -not $Spots.Count -or $Spots -contains $_.name } })
if (-not $selected.Count -and -not $CaptureCheckpoint) { throw "No spots selected (known: $(($config.spots | ForEach-Object name) -join ', '))." }
# One launch each: a spot, or for -Variants the same spot once per variant (drive.py runs only its "bench").
$runs = @(if ($sweep) {
    foreach ($v in $sweep.variants) {
        $copy = $selected[0] | ConvertTo-Json -Depth 6 | ConvertFrom-Json
        $bench = $sweep.bench | ConvertTo-Json -Depth 6 | ConvertFrom-Json   # a variant's own bench keys win
        foreach ($p in @(if ($v.bench) { $v.bench.PSObject.Properties })) { $bench | Add-Member $p.Name $p.Value -Force }
        $copy | Add-Member bench $bench -Force
        [pscustomobject]@{ dir = $v.name; spot = $copy; variant = $v }
    }
} else { foreach ($one in $selected) { [pscustomobject]@{ dir = $one.name; spot = $one; variant = $null } } })
# This run closes the game between spots. It must never close a game it did not start (Corey's own session).
$running = Get-Process Blacklist_game, Blacklist_DX11_game, Blacklist_Launcher, NvRemixBridge -ErrorAction SilentlyContinue | Select-Object -First 1
if ($running) { throw "$($running.Name) is already running (pid $($running.Id), started $($running.StartTime.ToString('HH:mm:ss'))). Close it yourself or wait: this script does not close a game it did not start." }
$otherLock = 'D:\BlacklistOverhaul\experiments\GAME_IN_USE.txt'
if (Test-Path $otherLock) { throw "Someone else has the game: $(Get-Content $otherLock -Raw)" }
if (Test-Path $GameLock) { throw "Someone else has the game: $(Get-Content $GameLock -Raw)" }
# A killed run leaves its marker line in the setup ini (its finally block never ran): its "before" files are in its
# file-backup folder, and this run would otherwise take the changed ini as its own "before".
$leftOver = @(Get-LeftOverRuns @((Join-Path $Setup 'd3d9_proxy.ini'), "$S\d3d9_proxy.ini"))
if ($leftOver.Count) { throw ("A gate run did not put its files back:`n  " + ($leftOver -join "`n  ")) }
New-Item -ItemType Directory -Force $runDir | Out-Null
# Each launch rotates the session logs and keeps only the newest 10 folders: copy any not yet kept out of rotation.
$evidence = 'D:\BlacklistOverhaul\experiments\playtest-evidence\play-setup-logs'
Get-ChildItem (Join-Path $Setup 'logs') -Directory -Filter 'before-*' -ErrorAction SilentlyContinue |
    Where-Object { -not (Test-Path (Join-Path $evidence $_.Name)) } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $evidence $_.Name) -Recurse; Write-Step "Kept session logs $($_.Name) in $evidence" }
# Which builds this run used, so a run can always be tied to a build (coordinator, Nov 16). The dxvk-remix commit
# comes from the record written when pkg was staged, and only counts while pkg's runtime still has the recorded hash.
$runtimeHash = Get-Hash (Join-Path $RuntimeSource 'd3d9.dll')
$staged = Join-Path $Setup 'STAGED_REMIX.json'
$stagedRec = if (Test-Path $staged) { Get-Content $staged -Raw | ConvertFrom-Json } else { $null }
$remixCommit = if (-not $stagedRec) { 'unknown: no play-setup\STAGED_REMIX.json' }
               elseif ($stagedRec.runtimeSha256 -ne $runtimeHash) { "unknown: pkg\.trex\d3d9.dll is not the build STAGED_REMIX.json records ($($stagedRec.commit))" }
               else { $stagedRec.commit }
$build = [ordered]@{ remixCommit = $remixCommit; remixRuntimeSha256 = $runtimeHash; remixClientSha256 = $RemixClientHash
                     proxySha256 = Get-Hash (Join-Path $Setup 'd3d9.dll') }
@{ spots = @($selected | ForEach-Object name); started = $Stamp; noRemix = [bool]$NoRemix; build = $build
   variants = @(if ($sweep) { $runs | ForEach-Object dir }) } | ConvertTo-Json | Set-Content (Join-Path $runDir 'manifest.json')
Write-Step "Builds: dxvk-remix $remixCommit, proxy $($build.proxySha256.Substring(0, 12))"

try { New-Item -ItemType File -Path $GameLock -Value "verify_runtime.ps1 runtime gate $([DateTime]::UtcNow.ToString('u')) run $runDir" | Out-Null }
catch { throw "Someone else has the game: $(Get-Content $GameLock -Raw)" }
$saveBackup = Join-Path $runDir 'save-backup'
$iniBackup = $null; $inputBackup = $null; $setupIniBackup = $null; $setupIniPath = Join-Path $Setup 'd3d9_proxy.ini'
# -Variants changes rtx.conf: the setup copy and both game copies get each variant, and the setup text back after it.
$rtxPaths = @((Join-Path $Setup 'rtx.conf')) + @($Configs.Keys | Where-Object { $Configs[$_] -eq 'rtx.conf' }); $rtxBackup = $null
$putBackFailed = @()
try {
    Stop-Game
    # Copies on disk before the first change (the text copies below are only what this run works from).
    $gameIni = "$S\d3d9_proxy.ini"
    $targets = [ordered]@{ $setupIniPath = $setupIniPath; $InputIni = $InputIni
                           $gameIni = $(if ($SkipInstall -and (Test-Path -LiteralPath $gameIni)) { $gameIni } else { $setupIniPath }) }
    if ($sweep) { foreach ($p in $rtxPaths) { $targets[$p] = $rtxPaths[0] } }
    Save-RunFiles $runDir $targets $SaveDir
    $inputBackup = [IO.File]::ReadAllText($InputIni)
    $setupIniBackup = [IO.File]::ReadAllText($setupIniPath)
    if ($sweep) { $rtxBackup = [IO.File]::ReadAllText($rtxPaths[0]) }
    $iniBackup = if (Test-Path -LiteralPath "$S\d3d9_proxy.ini") { [IO.File]::ReadAllText("$S\d3d9_proxy.ini") } else { $setupIniBackup }
    $gateIni = [Regex]::Replace($setupIniBackup, '(?m)^EnableDebuggingTelemetry[ \t]*=[ \t]*0', 'EnableDebuggingTelemetry=1')
    if ($NoRemix) {
        $gateIni = Get-NoRemixIni $gateIni
        @{ how = 'proxy kept, chained to Windows d3d9 with every rewrite off; written to play-setup\d3d9_proxy.ini and the game folder copy for this run only'
           proxyKeys = $NoRemixProxyKeys; lights = @{ Enable = 0 }; ini = $gateIni
           check = 'drive.py fails a spot unless the proxy log says this game process chained \Windows\SysWOW64\d3d9.dll (spot.json renderer); the load wait throws if Remix or NvRemixBridge loads'
           restore = 'both ini copies are written back from the backups taken at the start; see restored below' } |
            ConvertTo-Json -Depth 4 | Set-Content (Join-Path $runDir 'reference.json')
    }
    $gateIni = Add-RunMarker $gateIni $runDir
    [IO.File]::WriteAllText($setupIniPath, $gateIni)
    if (-not $SkipInstall) {
        $left = @(Get-PlayMismatches)
        if ($left.Count) {
            Write-Step ('Installing: ' + ($left -join ', '))
            $left = @(Install-PlaySetup)
            if ($left.Count) { throw ('Install check failed: ' + ($left -join ', ')) }
        }
        Write-Step 'Play setup installed and hash-checked (proxy, configs, pkg bridge client + runtime, mod links).'
    }
    if ($CaptureCheckpoint) {
        $dest = Join-Path $runDir "capture-$CaptureCheckpoint"
        New-Item -ItemType Directory -Force $dest | Out-Null
        [IO.File]::WriteAllText("$S\d3d9_proxy.ini", $gateIni)
        $from = if ($SaveFrom) { $SaveFrom } else { $saveBackup }
        if (-not (Get-ChildItem $from -Filter '*.save' -ErrorAction SilentlyContinue)) { throw "No .save files in $from" }
        if ($SaveFrom) {
            Get-ChildItem $SaveDir -Filter '*.save' | Remove-Item
            Copy-Item (Join-Path $SaveFrom '*.save') $SaveDir
        }
        $game = Start-GameAtCheckpoint
        & $Py (Join-Path $Runtime 'drive.py') capture --pid $game.Id @hashArgs --out (Join-Path $dest 'checkpoint.json')
        if ($LASTEXITCODE) { throw 'Could not read the camera/map from the game.' }
        Copy-Item (Join-Path $from '*.save') $dest
        Write-Step "Checkpoint recorded in $dest (not in the library)"
    }
    foreach ($run in $runs) {
        $spot = $run.spot
        $spotDir = Join-Path $runDir $run.dir
        $smi = $null
        $cp = Join-Path $Library $(if ($spot.checkpoint) { $spot.checkpoint } else { $spot.name })
        New-Item -ItemType Directory -Force $spotDir | Out-Null
        if (-not (Test-Path (Join-Path $cp 'checkpoint.json')) -or -not (Get-ChildItem $cp -Filter '*.save' -ErrorAction SilentlyContinue)) {
            @{ name = $spot.name; error = "no checkpoint recorded: load it in game, then run verify_runtime.ps1 -CaptureCheckpoint $($spot.name)"; windows = @() } |
                ConvertTo-Json | Set-Content (Join-Path $spotDir 'spot.json')
            Write-Step "$($spot.name): no checkpoint recorded, skipped (fails the gate)"
            continue
        }
        Write-Step "$($spot.name): loading the checkpoint"
        try {
            Stop-Game
            Get-ChildItem $SaveDir -Filter '*.save' | Remove-Item
            Copy-Item (Join-Path $cp '*.save') $SaveDir
            Set-GoggleBindings $InputIni $spot.goggleKeys
            $spotIni = $gateIni
            if ($run.variant) {   # the Play script copies setup files over game files that differ, so change both
                $rtxText = Set-RtxKeys $rtxBackup $run.variant.rtx
                foreach ($p in $rtxPaths) { [IO.File]::WriteAllText($p, $rtxText) }
                $spotIni = Set-ProxyKeys $gateIni $run.variant.proxy
                [IO.File]::WriteAllText($setupIniPath, $spotIni)
                Write-Step "$($run.dir): $(@(@($run.variant.rtx, $run.variant.proxy) | Where-Object { $_ } | ForEach-Object { $_.PSObject.Properties } | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ', ')"
            }
            [IO.File]::WriteAllText("$S\d3d9_proxy.ini", $spotIni)
            # "cinematic": the level opens with a cutscene; cinematic.py records it as numbers before drive.py moves Sam.
            $cine = if ($spot.cinematic) {
                Start-Process $Py -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $spotDir 'cinematic.log') `
                    -RedirectStandardError (Join-Path $spotDir 'cinematic.err') -ArgumentList (@((Join-Path $Runtime 'cinematic.py'),
                    '--log', "$S\d3d9_proxy.log", '--frames', "$S\d3d9_proxy_frames.csv", '--out', (Join-Path $spotDir 'cinematic')) +
                    @(if ($NoRemix) { '--signal', 'camera' }) |
                    ForEach-Object { '"' + $_ + '"' }) }
            # GPU clocks, temperature, power, clock-limit reasons and memory once a second for the whole launch
            # (worker C, Nov 16: what puts a launch in the slow Ray Reconstruction mode).
            # One nvidia-smi call per sample (runtime\gpu_sampler.ps1): -lms buffers 4 KB and the kill below lost the tail.
            $smi = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ('"' + (Join-Path $Runtime 'gpu_sampler.ps1') + '"'), '-Out', ('"' + (Join-Path $spotDir 'gpu_run.csv') + '"'),
                '-Query', 'timestamp,clocks.gr,clocks.mem,clocks.sm,temperature.gpu,power.draw,power.limit,clocks_throttle_reasons.active,memory.used,memory.total,pstate')
            $game = Start-GameAtCheckpoint
            # Other programs holding the GPU (Windows reports their names; per-process memory reads N/A).
            & nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv 2>&1 | Set-Content (Join-Path $spotDir 'gpu_apps.csv')
            if ($cine) {
                if (-not $cine.WaitForExit(120000)) { $cine.Kill(); Write-Step "$($spot.name): cutscene recorder timed out" }
                Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'cinematic.log') -Tail 1))"
            }
            $spot | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $spotDir 'spot_config.json')
            $driveArgs = @((Join-Path $Runtime 'drive.py'), 'spot', '--pid', $game.Id) + $hashArgs + @(
                '--spot', (Join-Path $spotDir 'spot_config.json'), '--checkpoint', (Join-Path $cp 'checkpoint.json'),
                '--system', $S, '--root', $Root, '--out', $spotDir) + @(if ($NoRemix) { '--no-remix' })
            $drive = Start-Process $Py -ArgumentList ($driveArgs | ForEach-Object { '"' + $_ + '"' }) -NoNewWindow -PassThru `
                -RedirectStandardOutput (Join-Path $spotDir 'drive.log') -RedirectStandardError (Join-Path $spotDir 'drive.err')
            if (-not $drive.WaitForExit(40 * 60 * 1000)) { $drive.Kill(); Write-Step "$($spot.name): drive.py timed out (40 min)" }
            Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'drive.log') -Tail 1))"
            Stop-Game   # the next launch starts remix-dxvk.log over; gate.py's clean_quit reads this spot's copy
            Copy-Item -LiteralPath "$S\rtx-remix\logs\remix-dxvk.log" $spotDir -ErrorAction SilentlyContinue
            if ($run.variant) {
                # Proof the change loaded: Remix lists every option it read ("info:    rtx.qualityDLSS = 2").
                $remixLog = Join-Path $spotDir 'remix-dxvk.log'
                $loaded = [ordered]@{}
                foreach ($p in @(if ($run.variant.rtx) { $run.variant.rtx.PSObject.Properties })) {
                    $loaded[$p.Name] = @(if (Test-Path $remixLog) { Select-String -LiteralPath $remixLog -Pattern "info:\s+$([Regex]::Escape($p.Name)) = (.*?)\s*$" |
                        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Unique })
                }
                $ok = -not @($loaded.Keys | Where-Object { $loaded[$_].Count -ne 1 -or $loaded[$_][0] -ne [string]$run.variant.rtx.$_ }).Count
                # Proxy keys: this launch's proxy.log prints its settings ("diagnostics: EnableDebuggingTelemetry=0
                # ... CallTiming=0", "features: ... HitchSampler=0"). A key it does not print is "not proven", never
                # true (coordinator review, Nov 16: 12_telemetry_off was reported loaded with nothing checked).
                $proxyLog = Join-Path $spotDir 'proxy.log'
                $proxySeen = [ordered]@{}
                foreach ($p in @(if ($run.variant.proxy) { $run.variant.proxy.PSObject.Properties })) {
                    $proxySeen[$p.Name] = @(if (Test-Path $proxyLog) { Select-String -LiteralPath $proxyLog -Pattern "\b$([Regex]::Escape($p.Name))=(\S+)" |
                        ForEach-Object { $_.Matches[0].Groups[1].Value } | Select-Object -Unique })
                }
                $proxyOk = if (-not $proxySeen.Count) { $true }
                           elseif (@($proxySeen.Keys | Where-Object { -not $proxySeen[$_].Count }).Count) { 'not proven' }
                           else { -not @($proxySeen.Keys | Where-Object { $proxySeen[$_].Count -ne 1 -or $proxySeen[$_][0] -ne [string]$run.variant.proxy.$_ }).Count }
                # Which Ray Reconstruction mode this launch got (worker C, Nov 16: launches run RR at ~11.1 or ~13.3 ms).
                $rrCreated = @(if (Test-Path $remixLog) { Select-String -LiteralPath $remixLog -SimpleMatch '[RTX Ray Reconstruction] created' | ForEach-Object Line })
                [ordered]@{ name = $run.dir; rtx = $run.variant.rtx; proxy = $run.variant.proxy; remixLogValues = $loaded; rtxLoaded = $ok
                            proxyLogValues = $proxySeen; proxyLoaded = $proxyOk; rrCreated = $rrCreated; build = $build
                            proxyIni = $spotIni } | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $spotDir 'variant.json')
                Write-Step "$($run.dir): rtx $(if ($ok) { 'keys listed with the new values in remix-dxvk.log' } else { "NOT LOADED as asked: $($loaded | ConvertTo-Json -Compress)" }); proxy keys $(if ($proxyOk -eq $true) { 'as asked' } elseif ($proxyOk -eq 'not proven') { "NOT PROVEN (no log line): $($proxySeen | ConvertTo-Json -Compress)" } else { "NOT LOADED as asked: $($proxySeen | ConvertTo-Json -Compress)" }); RR: $($rrCreated -join ' | ')"
            } elseif ($spot.switchTest -or $spot.lightSwitch) {
                & $Py (Join-Path $Runtime 'switch_report.py') $spotDir | Out-Null
                Write-Step "$($spot.name): switch test numbers in $(Join-Path $spotDir 'switch.md')"
            }
        } catch {
            if ($cine -and -not $cine.HasExited) { $cine.Kill() }
            Write-Step "$($spot.name): $($_.Exception.Message)"
            if (-not (Test-Path (Join-Path $spotDir 'spot.json'))) {
                @{ name = $spot.name; error = $_.Exception.Message; windows = @() } | ConvertTo-Json | Set-Content (Join-Path $spotDir 'spot.json')
            }
        } finally {
            if ($smi) { try { if (-not $smi.HasExited) { $smi.Kill() } } catch { }; $smi = $null }
            if ($run.variant) {   # back to the baseline before the next variant (the game is closed by now)
                Stop-Game
                foreach ($p in $rtxPaths) { [IO.File]::WriteAllText($p, $rtxBackup) }
                [IO.File]::WriteAllText($setupIniPath, $gateIni)
            }
        }
    }
} finally {
    # Each step on its own: one that throws (a game exiting mid-kill, a locked save) must not skip the rest, and the
    # lock goes last. Before the backups exist nothing was changed, so there is nothing to put back.
    try { Stop-Game } catch { $putBackFailed += "closing the game: $($_.Exception.Message)" }
    if (Test-Path (Join-Path $runDir 'file-backup\restore.json')) { $putBackFailed += @(Restore-AfterRun $runDir) }
    if ($NoRemix -and (Test-Path (Join-Path $runDir 'reference.json'))) { try {
        $note = Get-Content (Join-Path $runDir 'reference.json') -Raw | ConvertFrom-Json
        $note | Add-Member restored ([ordered]@{
            setupIniAsBefore = ([IO.File]::ReadAllText($setupIniPath) -eq $setupIniBackup)
            gameIniAsBefore  = ([IO.File]::ReadAllText("$S\d3d9_proxy.ini") -eq $(if ($SkipInstall) { $iniBackup } else { $setupIniBackup }))
            playMismatches   = @(Get-PlayMismatches); launcherApi = (Get-StartupApi) }) -Force
        $note | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $runDir 'reference.json')
        Write-Step "Remix settings put back: $(($note.restored | ConvertTo-Json -Compress))"
    } catch { Write-Step "Could not record the restore check: $($_.Exception.Message)" } }
    Show-PutBackFailures $putBackFailed
    if ($putBackFailed.Count) { Write-Step "Put the rest back with: verify_runtime.ps1 -RestoreFrom $runDir" }
    Remove-Item -LiteralPath $GameLock -ErrorAction SilentlyContinue
}
if ($putBackFailed.Count -and -not $sweep -and -not $CaptureCheckpoint) { [void](Gate $runDir); exit 3 }
if ($putBackFailed.Count) { exit 3 }
if ($CaptureCheckpoint) { exit 0 }
if ($sweep) {   # numbers for the requester to weigh, not a pass/fail
    & $Py (Join-Path $Runtime 'settings_report.py') $runDir | Out-Host
    "Result: $(Join-Path $runDir 'settings.md')"
    exit $LASTEXITCODE
}
$code = Gate $runDir
"Result: $(Join-Path $runDir 'result.md')"
exit $code
