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
# A spot in spots.json may name another spot's saves with "checkpoint" and move Sam elsewhere with "teleport"
# ({"location": [x, y, z], "yaw": deg}); drive.py fails the spot when the game does not keep him there.
# Needs: nobody else using the game (takes play-setup\AGENT_GAME_LOCK.txt), Steam running. Works with the monitors
# off (screen frames come from gdigrab; the camera is set in memory). Only the goggle keys and the title-screen
# keys need the game window in front, which this script does itself.
param([string[]]$Spots = @(), [switch]$DryRun, [string]$CaptureCheckpoint = '', [switch]$Launch, [string]$Evaluate = '', [switch]$SkipInstall, [string]$SaveFrom = '',
      [string]$Previous = '', [string]$Reference = '')
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
function Gate($folder) {
    $name = Split-Path $folder -Leaf
    $stamp = $name -replace '^reference-', ''
    $spotsHere = (Get-RunSpots $folder) -join ','
    $prev = if ($Previous) { $Previous } else {
        Get-ChildItem $ResultsRoot -Directory | Where-Object { $_.Name -match '^\d{8}-\d{6}$' -and $_.Name -lt $stamp -and
            (Test-Path (Join-Path $_.FullName 'result.json')) -and ((Get-RunSpots $_.FullName) -join ',') -eq $spotsHere } |
            Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName }
    $ref = if ($Reference) { $Reference } elseif ($name -like 'reference-*') { $null } else {
        Get-ChildItem $ResultsRoot -Directory -Filter 'reference-*' | Where-Object { ($_.Name -replace '^reference-', '') -lt $stamp } |
            Sort-Object Name | Select-Object -Last 1 -ExpandProperty FullName }
    $gateArgs = @((Join-Path $Runtime 'gate.py'), $folder)
    if ($prev) { $gateArgs += @('--previous', $prev) }
    if ($ref) { $gateArgs += @('--reference', $ref) }
    Write-Host "  gate: previous run $(if ($prev) { $prev } else { '(none with the same spots)' }); no-Remix reference $(if ($ref) { $ref } else { '(none)' })"
    & $Py @gateArgs | Out-Host
    return $LASTEXITCODE
}

if ($DryRun) {
    $out = Join-Path $ResultsRoot "dryrun-$Stamp"
    & $Py (Join-Path $Runtime 'fakerun.py') $out
    exit $LASTEXITCODE
}
if ($Evaluate) { exit (Gate $Evaluate) }

. (Join-Path (Split-Path $Tests) 'play-setup\RemixPlayCommon.ps1')
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
function Stop-Game {
    Get-Process Blacklist_game, NvRemixBridge -ErrorAction SilentlyContinue | Stop-Process -Force
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ((Get-RunningGame) -and $t.Elapsed.TotalSeconds -lt 30) { Start-Sleep 1 }
    if ($script:PlayProcess -and -not $script:PlayProcess.WaitForExit(120000)) { $script:PlayProcess.Kill() }
    $script:PlayProcess = $null
}

# Steam -> launcher -> game; then Space, Space, Enter (title, "press any key", "resume at checkpoint") until the
# proxy log says ray tracing switched on in this process, i.e. a level is rendering. Never Esc (quits from menus).
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
    while (-not (Select-String -LiteralPath $log -Pattern "\[pid $($game.Id)\] lightpub: AutoRaytracing -> on" -Quiet -ErrorAction SilentlyContinue)) {
        if ($t.Elapsed.TotalSeconds -gt 480) { throw 'The game did not reach the level within 8 minutes.' }
        if ($game.HasExited) { throw 'The game exited while loading.' }
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

$runDir = Join-Path $ResultsRoot $Stamp
$selected = @(if (-not $CaptureCheckpoint) { $config.spots | Where-Object { -not $Spots.Count -or $Spots -contains $_.name } })
if (-not $selected.Count -and -not $CaptureCheckpoint) { throw "No spots selected (known: $(($config.spots | ForEach-Object name) -join ', '))." }
# This run closes the game between spots. It must never close a game it did not start (Corey's own session).
$running = Get-Process Blacklist_game, Blacklist_DX11_game, Blacklist_Launcher, NvRemixBridge -ErrorAction SilentlyContinue | Select-Object -First 1
if ($running) { throw "$($running.Name) is already running (pid $($running.Id), started $($running.StartTime.ToString('HH:mm:ss'))). Close it yourself or wait: this script does not close a game it did not start." }
$otherLock = 'D:\BlacklistOverhaul\experiments\GAME_IN_USE.txt'
if (Test-Path $otherLock) { throw "Someone else has the game: $(Get-Content $otherLock -Raw)" }
New-Item -ItemType Directory -Force $runDir | Out-Null
# Each launch rotates the session logs and keeps only the newest 10 folders: copy any not yet kept out of rotation.
$evidence = 'D:\BlacklistOverhaul\experiments\playtest-evidence\play-setup-logs'
Get-ChildItem (Join-Path $Setup 'logs') -Directory -Filter 'before-*' -ErrorAction SilentlyContinue |
    Where-Object { -not (Test-Path (Join-Path $evidence $_.Name)) } |
    ForEach-Object { Copy-Item $_.FullName (Join-Path $evidence $_.Name) -Recurse; Write-Step "Kept session logs $($_.Name) in $evidence" }
@{ spots = @($selected | ForEach-Object name); started = $Stamp } | ConvertTo-Json | Set-Content (Join-Path $runDir 'manifest.json')

try { New-Item -ItemType File -Path $GameLock -Value "verify_runtime.ps1 runtime gate $([DateTime]::UtcNow.ToString('u'))" | Out-Null }
catch { throw "Someone else has the game: $(Get-Content $GameLock -Raw)" }
$saveBackup = Join-Path $runDir 'save-backup'
$iniBackup = $null; $inputBackup = $null; $setupIniBackup = $null; $setupIniPath = Join-Path $Setup 'd3d9_proxy.ini'
try {
    Stop-Game
    New-Item -ItemType Directory -Force $saveBackup | Out-Null
    Copy-Item (Join-Path $SaveDir '*') $saveBackup
    $inputBackup = [IO.File]::ReadAllText($InputIni)
    $setupIniBackup = [IO.File]::ReadAllText($setupIniPath)
    $iniBackup = if (Test-Path -LiteralPath "$S\d3d9_proxy.ini") { [IO.File]::ReadAllText("$S\d3d9_proxy.ini") } else { $setupIniBackup }
    $gateIni = [Regex]::Replace($setupIniBackup, '(?m)^EnableDebuggingTelemetry[ \t]*=[ \t]*0', 'EnableDebuggingTelemetry=1')
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
    foreach ($spot in $selected) {
        $spotDir = Join-Path $runDir $spot.name
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
            [IO.File]::WriteAllText("$S\d3d9_proxy.ini", $gateIni)
            # "cinematic": the level opens with a cutscene; cinematic.py records it as numbers before drive.py moves Sam.
            $cine = if ($spot.cinematic) {
                Start-Process $Py -NoNewWindow -PassThru -RedirectStandardOutput (Join-Path $spotDir 'cinematic.log') `
                    -RedirectStandardError (Join-Path $spotDir 'cinematic.err') -ArgumentList (@((Join-Path $Runtime 'cinematic.py'),
                    '--log', "$S\d3d9_proxy.log", '--frames', "$S\d3d9_proxy_frames.csv", '--out', (Join-Path $spotDir 'cinematic')) |
                    ForEach-Object { '"' + $_ + '"' }) }
            $game = Start-GameAtCheckpoint
            if ($cine) {
                if (-not $cine.WaitForExit(120000)) { $cine.Kill(); Write-Step "$($spot.name): cutscene recorder timed out" }
                Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'cinematic.log') -Tail 1))"
            }
            $spot | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $spotDir 'spot_config.json')
            $driveArgs = @((Join-Path $Runtime 'drive.py'), 'spot', '--pid', $game.Id) + $hashArgs + @(
                '--spot', (Join-Path $spotDir 'spot_config.json'), '--checkpoint', (Join-Path $cp 'checkpoint.json'),
                '--system', $S, '--root', $Root, '--out', $spotDir)
            $drive = Start-Process $Py -ArgumentList ($driveArgs | ForEach-Object { '"' + $_ + '"' }) -NoNewWindow -PassThru `
                -RedirectStandardOutput (Join-Path $spotDir 'drive.log') -RedirectStandardError (Join-Path $spotDir 'drive.err')
            if (-not $drive.WaitForExit(20 * 60 * 1000)) { $drive.Kill(); Write-Step "$($spot.name): drive.py timed out (20 min)" }
            Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'drive.log') -Tail 1))"
            Stop-Game   # the next launch starts remix-dxvk.log over; gate.py's clean_quit reads this spot's copy
            Copy-Item -LiteralPath "$S\rtx-remix\logs\remix-dxvk.log" $spotDir -ErrorAction SilentlyContinue
            if ($spot.switchTest -or $spot.lightSwitch) {
                & $Py (Join-Path $Runtime 'switch_report.py') $spotDir | Out-Null
                Write-Step "$($spot.name): switch test numbers in $(Join-Path $spotDir 'switch.md')"
            }
        } catch {
            if ($cine -and -not $cine.HasExited) { $cine.Kill() }
            Write-Step "$($spot.name): $($_.Exception.Message)"
            if (-not (Test-Path (Join-Path $spotDir 'spot.json'))) {
                @{ name = $spot.name; error = $_.Exception.Message; windows = @() } | ConvertTo-Json | Set-Content (Join-Path $spotDir 'spot.json')
            }
        }
    }
} finally {
    Stop-Game
    if (Test-Path $saveBackup) {
        Get-ChildItem $SaveDir -Filter '*.save' | Remove-Item
        Copy-Item (Join-Path $saveBackup '*') $SaveDir -Force
    }
    if ($inputBackup) { [IO.File]::WriteAllText($InputIni, $inputBackup) }
    if ($setupIniBackup) { [IO.File]::WriteAllText($setupIniPath, $setupIniBackup) }
    if ($iniBackup) { [IO.File]::WriteAllText("$S\d3d9_proxy.ini", $(if ($SkipInstall) { $iniBackup } else { $setupIniBackup })) }
    $restored = Restore-LauncherApi
    if ($restored) { Write-Step "Launcher setting put back: $restored" }
    if (Resume-Dx11Mod) { Write-Step 'DX11 lighting mod put back.' }
    Remove-Item -LiteralPath $GameLock -ErrorAction SilentlyContinue
}
if ($CaptureCheckpoint) { exit 0 }
$code = Gate $runDir
"Result: $(Join-Path $runDir 'result.md')"
exit $code
