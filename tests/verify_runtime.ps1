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
      [string]$Previous = '', [string]$Reference = '', [switch]$NoRemix, [string]$Variants = '', [string]$RestoreFrom = '',
      [string]$SuiteNote = '', [string[]]$IniKeys = @(), [switch]$Record)
$ErrorActionPreference = 'Stop'
# -Record -Spots <one spot>: a recording for the frame replay rig (tests\replay). The original game as in -NoRemix,
# but the proxy chains the apitrace recorder ($RecorderDll, the one file drive.py accepts as a recorder), which
# writes every Direct3D 9 call to $TraceDir\<stamp>-<spot>.trace, and the proxy writes its side recording (camera
# uploads, decal draws, lamps, flares; [Replay] Record=1) into the spot folder. The run folder is
# $RecordingDir\recording-<stamp> with recording.json: outside $ResultsRoot and never named reference-<stamp>, so no
# later run can take it for a reference; it is not gated.
$RecorderDll = 'D:\BlacklistOverhaul\experiments\replay-rig\tools\apitrace-14.0-win32\lib\wrappers\d3d9.dll'
$TraceDir = 'D:\BlacklistOverhaul\experiments\replay-rig\traces'
$RecordingDir = 'D:\BlacklistOverhaul\experiments\replay-rig\recordings'
if ($Record) { $NoRemix = $true }
$Tests = $PSScriptRoot
$Runtime = Join-Path $Tests 'runtime'
$Py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'
$Library = 'D:\BlacklistOverhaul\experiments\save-backups\gate-checkpoints'
$SaveDir = 'D:\Ubisoft\Ubisoft Game Launcher\savegames\d36412d1-6246-4703-adbf-8fbd46c6da34\449'
$InputIni = Join-Path ([Environment]::GetFolderPath('MyDocuments')) 'Ubisoft\Blacklist\inputTranslations.ini'
# One lock for every worktree, in the main tree. A lock beside this script is seen only by launches from the same
# worktree (Nov 19 05:27: such a launch ran into another thread's replay). RemixPlayCommon.ps1 names the same file.
$GameLock = 'D:\BlacklistOverhaul\tools\remix\play-setup\AGENT_GAME_LOCK.txt'
$ResultsRoot = 'D:\BlacklistOverhaul\experiments\remix-gate'
# run_all_offline.ps1 writes its last run and last pass here; the manifest compares them with this launch's tree.
$SuiteRecord = Join-Path $ResultsRoot 'offline_suite_last.json'
$RemixSrc = Join-Path (Split-Path $Tests) 'src\dxvk-remix'
$Stamp = Get-Date -Format 'yyyyMMdd-HHmmss'

# gate.py compares a run with the previous run (FPS) and with the no-Remix reference (lag, brightness). Unless given,
# --previous is the newest earlier Remix run whose manifest lists the same spots, and --reference the newest
# reference-* run (no Remix) earlier than this one.
function Get-RunSpots($folder) {
    $m = Join-Path $folder 'manifest.json'
    if (Test-Path $m) { @((Get-Content $m -Raw | ConvertFrom-Json).spots | Sort-Object) } else { @() }
}
# The launch-wide ini keys a run was made with (-IniKeys, manifest iniKeys), as one text; '' for a run without them.
function Get-RunIniKeys($folder) {
    $m = Join-Path $folder 'manifest.json'
    if (Test-Path $m) { @((Get-Content $m -Raw | ConvertFrom-Json).iniKeys | Where-Object { $_ }) -join ';' } else { '' }
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
# The run gate.py's FPS check compares with: -Previous when given, else the newest earlier run of the same kind with the
# same spots. Either way only a run in the same display state (runtime\display_state.py compare: same class, not
# unknown), since plug off ran about 12 % faster per frame than plug on (Nov 17 00:21). Returns @{ prev; note }: prev
# $null and a NOT COMPARABLE note when the given or every candidate run is in another state.
function Select-Previous($folder, [string]$given) {
    $name = Split-Path $folder -Leaf
    $stamp = $name -replace '^reference-', ''
    $spotsHere = (Get-RunSpots $folder) -join ','
    # a reference run is compared only with earlier reference runs, a Remix run only with earlier Remix runs
    $kind = if ($name -like 'reference-*') { '^reference-\d{8}-\d{6}$' } else { '^\d{8}-\d{6}$' }
    $cands = if ($given) { @($given) } else {
        @(Get-ChildItem $ResultsRoot -Directory | Where-Object { $_.Name -match $kind -and ($_.Name -replace '^reference-', '') -lt $stamp -and
            (Test-Path (Join-Path $_.FullName 'result.json')) -and ((Get-RunSpots $_.FullName) -join ',') -eq $spotsHere -and
            # a run made with launch-wide ini keys (-IniKeys) is only the previous run of one made with the same keys
            (Get-RunIniKeys $_.FullName) -eq (Get-RunIniKeys $folder) } |
            Sort-Object Name -Descending | ForEach-Object FullName) }
    $first = $null
    foreach ($c in $cands) {
        $why = & $Py -I (Join-Path $Runtime 'display_state.py') compare $folder $c
        if ($LASTEXITCODE -eq 0) { return @{ prev = $c; note = $null } }
        if (-not $first) { $first = $why }
    }
    if (-not $cands.Count) { return @{ prev = $null; note = $null } }
    @{ prev = $null; note = "fps_regression $first$(if (-not $given) { " (and no other earlier run with the same spots is in the same display state)" })" }
}
function Gate($folder) {
    $name = Split-Path $folder -Leaf
    $stamp = $name -replace '^reference-', ''
    $pick = Select-Previous $folder $Previous
    $prev = $pick.prev
    $ref = if ($Reference) { $Reference } elseif ($name -like 'reference-*') { $null } else { Select-Reference $folder $stamp }
    $gateArgs = @((Join-Path $Runtime 'gate.py'), $folder)
    if ($prev) { $gateArgs += @('--previous', $prev) }
    if ($ref) { $gateArgs += @('--reference', $ref) }
    Write-Host "  gate: previous run $(if ($prev) { $prev } elseif ($pick.note) { '(none in the same display state)' } else { '(none with the same spots)' }); no-Remix reference $(if ($ref) { $ref } else { '(none)' })"
    & $Py @gateArgs | Out-Host
    $code = $LASTEXITCODE
    # Windows taken across a frame gap over 500 ms are named in result.md (coordinator, Nov 17: 125334's y0_p30 still
    # held a 1.6 s gap and nothing said so). Marks only; the gate's exit code is unchanged.
    & $Py -I (Join-Path $Runtime 'stall_windows.py') $folder --md (Join-Path $folder 'result.md') | Out-Host
    if ($pick.note) {
        Write-Host "  gate: $($pick.note)" -ForegroundColor Yellow
        $md = Join-Path $folder 'result.md'
        if (Test-Path $md) { Add-Content $md "`r`n**Display state:** $($pick.note)" }
    }
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
    $installed = { param($m) if ($m.remixInstalledCommit) { $m.remixInstalledCommit } else { $m.remixCommit } }   # older manifests
    if ($diff.Count) { return "the previous run $(Split-Path $prev -Leaf) used other builds ($($diff -join ', ') differ; dxvk-remix installed $(& $installed $b) then, $(& $installed $a) now), so the FPS comparison measures the build change too." }
    $null
}
# The dxvk-remix build pkg holds now: the commit play-setup\STAGED_REMIX.json records, and only while pkg's runtime
# still has the hash it records. Not the dxvk-remix source HEAD, which may be built but not staged. run_all_offline.ps1
# loads this same function from this file for its suite record.
function Get-InstalledRemixCommit([string]$stagedPath, [string]$runtimeDll) {
    if (-not (Test-Path -LiteralPath $stagedPath)) { return 'unknown: no play-setup\STAGED_REMIX.json' }
    $rec = Get-Content -LiteralPath $stagedPath -Raw | ConvertFrom-Json
    $hash = if (Test-Path -LiteralPath $runtimeDll) { (Get-FileHash -LiteralPath $runtimeDll -Algorithm SHA256).Hash }
    $short = "$($rec.commit)".Substring(0, [Math]::Min(7, "$($rec.commit)".Length))
    if ($rec.runtimeSha256 -ne $hash) { return "unknown: pkg\.trex\d3d9.dll is not the build STAGED_REMIX.json records ($short)" }
    $short
}

if ($DryRun) {
    $out = Join-Path $ResultsRoot "dryrun-$Stamp"
    & $Py (Join-Path $Runtime 'fakerun.py') $out
    exit $LASTEXITCODE
}
if ($Evaluate) { exit (Gate $Evaluate) }

. (Join-Path (Split-Path $Tests) 'play-setup\RemixPlayCommon.ps1')
. (Join-Path $Runtime 'run_files.ps1')
# True after removing $path, when it is a Play-script lock ($LockTag) whose start time is not before $since (the start
# of this run's own Play script). Corey's lock, or one written before this run's Play script started, stays.
function Remove-OwnPlayLock([string]$path, [datetime]$since) {
    $text = Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue
    if (-not $text -or -not $text.StartsWith($LockTag) -or $text -notmatch '\(started (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC') { return $false }
    $started = [datetime]::SpecifyKind([datetime]::ParseExact($Matches[1], 'yyyy-MM-dd HH:mm:ss', $null), 'Utc')
    if ($started -lt $since.ToUniversalTime().AddSeconds(-1)) { return $false }
    Remove-Item -LiteralPath $path
    $true
}
# -RestoreFrom of a killed run: the Play script dies with it and leaves its GAME_IN_USE lock (Nov 17 18:43: run 183718
# was stopped, -RestoreFrom removed only the agent lock, and the next launch refused on "lock present"). The run's
# latest play-HHmmss.log names when its last Play script started (a time before the run folder's means after
# midnight); the lock is removed only by Remove-OwnPlayLock's rule against that time.
function Remove-RunPlayLock([string]$path, [string]$runDir) {
    # No-Remix runs are named reference-<stamp> (Nov 18 00:04: 233607's lock stayed and was removed by hand).
    if ((Split-Path -Leaf $runDir) -notmatch '^(?:reference-|recording-)?(\d{8})-(\d{6})') { return $false }
    $day = $Matches[1]; $runStart = [datetime]::ParseExact("$day$($Matches[2])", 'yyyyMMddHHmmss', $null)
    $starts = @(Get-ChildItem -LiteralPath $runDir -Filter 'play-*.log' -File -ErrorAction SilentlyContinue |
        Where-Object Name -match '^play-(\d{6})\.log$' | ForEach-Object {
            $t = [datetime]::ParseExact("$day$($_.Name.Substring(5, 6))", 'yyyyMMddHHmmss', $null)
            if ($t -lt $runStart) { $t.AddDays(1) } else { $t } })
    if (-not $starts.Count) { return $false }
    Remove-OwnPlayLock $path ($starts | Sort-Object | Select-Object -Last 1)
}
# The values proxy.log prints for [Proxy] key $name: "Name=value", or the load line "Proxy keys: Name N, Other M"
# (200741: QueryStatsFrames was printed only that way and read "not proven"). A number followed by a word, as in
# "DumpStageTexturesPs 0 ids", is a count and not the key's value, so it is not read.
function Get-ProxyKeySeen([string]$log, [string]$name) {
    if (-not (Test-Path -LiteralPath $log)) { return }
    Select-String -LiteralPath $log -Pattern "\b$([Regex]::Escape($name))(?:=(\S+)| (\d+)(?=,|\s*$))" | ForEach-Object {
        $g = $_.Matches[0].Groups; if ($g[1].Success) { $g[1].Value } else { $g[2].Value } } | Select-Object -Unique
}
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
    if (Remove-RunPlayLock $Lock $RestoreFrom) { Write-Step "GAME_IN_USE lock left by that run's killed Play script removed." }
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
  // runtime\display.py watch reads the ticks noted here to tell these taps from outside input (display.csv input).
  public static string SendLog;
  public static void Tap(ushort scan) {
    foreach (uint up in new uint[] { 0, 2 }) { var i = new INPUT(); i.type = 1; i.ki.scan = scan; i.ki.flags = 0x8u | up;
      SendInput(1, new[] { i }, Marshal.SizeOf(typeof(INPUT)));
      if (SendLog != null) System.IO.File.AppendAllText(SendLog, ((uint)Environment.TickCount).ToString() + "\r\n");
      Thread.Sleep(100); } }
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
        try {
            if (-not $script:PlayProcess.WaitForExit(120000)) {
                $since = $script:PlayProcess.StartTime
                $script:PlayProcess.Kill()
                # A killed Play script never removes its GAME_IN_USE lock (Nov 16 08:51: the lock stayed and blocked
                # the next run). Remove it only when this run's Play script wrote it.
                if (Remove-OwnPlayLock $Lock $since) { Write-Step 'Play script killed; removed the GAME_IN_USE lock it wrote.' }
            }
        } catch { Write-Step "Play script: $($_.Exception.Message)" }
    }
    $script:PlayProcess = $null
}

# Why a no-Remix run cannot record a picture, or $null. With every monitor switched off the original game draws its
# window black (Nov 16: references 045456 and 054905 were black after Corey turned his monitors off at about 2 to 3 AM;
# Remix captures still worked). Windows then lists each monitor with Present False; a monitor that is on has Present True.
function Get-DisplayProblem($monitors) {
    if (@($monitors | Where-Object Present).Count) { return $null }
    "No monitor is switched on (Windows lists $(@($monitors).Count) monitors, none present). The original game draws a black picture without a display: turn a monitor on, then run again."
}

# Free space on C: (coordinator, Nov 16: it hit 0 at 09:39 and moved by GBs within minutes during a sweep). Sampled
# every 2 s from 12:14 to 12:30 over five launches: it follows the page file (c:\pagefile.sys, set to 8 GB growing to
# 64 GB), which grows with the memory all programs hold (54 GB committed on 32.6 GB of RAM with the game running; the
# Remix bridge holds 8.3 GB of it) and shrinks after the game quits. The largest fall in one launch was 8,801 MB
# (12:22:49 to 12:25:01; the page file grew 8,275 MB of it), and the largest change the page file did not explain was
# 1,548 MB. Each launch records C: before it and at its end in <run>\cdrive.csv, and none starts below $MinCFreeMB.
$MinCFreeMB = 10500
# Memory: Windows keeps the commit limit about 6 GB above what is committed by growing the page file, so what runs
# out is the page file's room to grow: RAM + the smaller of its maximum (registry PagingFiles) and its size + C: free,
# less what is committed. The largest rise in committed memory during one launch was 12,652 MB (12:31:07 to
# 12:32:35, nine launches sampled 12:14 to 12:37); $MinCommitRoomMB adds the same 1,548 MB margin as the disk limit.
$MinCommitRoomMB = 14200
function Get-CDriveSample {
    $pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | Where-Object Name -like 'c:*')
    $os = Get-CimInstance Win32_OperatingSystem
    $setting = [string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management').PagingFiles
    [ordered]@{ time = (Get-Date -Format 'HH:mm:ss'); freeMB = [int]((Get-PSDrive C).Free / 1MB)
                pagefileMB = [int](($pf | Measure-Object AllocatedBaseSize -Sum).Sum)
                pagefileMaxMB = $(if ($setting -match '^c:\\pagefile\.sys \d+ (\d+)') { [int]$Matches[1] } else { 0 })
                ramMB = [int]($os.TotalVisibleMemorySize / 1KB)
                commitMB = [int](($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1KB) }
}
function Add-CDriveSample([string]$runDir, [string]$launch, [string]$when) {
    $s = Get-CDriveSample
    $csv = Join-Path $runDir 'cdrive.csv'
    if (-not (Test-Path $csv)) { Set-Content $csv 'launch,when,time,freeMB,pagefileMB,pagefileMaxMB,ramMB,commitMB' }
    Add-Content $csv "$launch,$when,$($s.time),$($s.freeMB),$($s.pagefileMB),$($s.pagefileMaxMB),$($s.ramMB),$($s.commitMB)"
    $s
}
function Get-CSpaceProblem([int]$freeMB, [int]$limitMB) {
    if ($freeMB -ge $limitMB) { return $null }
    "C: has $freeMB MB free, below $limitMB MB: a launch can take that much (the page file on C: grows with the memory the game and the Remix bridge hold). Free space on C: (or close programs), then run again."
}
# $s: a Get-CDriveSample. A page file maximum of 0 (system managed, or not read) is taken as room up to C:'s free space.
function Get-MemoryProblem($s, [int]$needMB) {
    $grow = if ($s.pagefileMaxMB) { [math]::Min($s.pagefileMaxMB, $s.pagefileMB + $s.freeMB) } else { $s.pagefileMB + $s.freeMB }
    $room = $s.ramMB + $grow - $s.commitMB
    if ($room -ge $needMB) { return $null }
    "Memory: $($s.commitMB) MB is committed and RAM ($($s.ramMB) MB) plus the largest the page file can become ($grow MB) leaves $room MB, below the $needMB MB a launch can add. Close programs, then run again."
}
# Which monitors are on and the GPU's idle power just before a launch (coordinator, Nov 16: launches up to 10:24 ran
# about 35 ms and launches from 11:31 about 42 ms whatever the build, with idle GPU power about 6 W higher; the guess is
# that the monitors were switched on in between). A monitor that is on is listed Present (see Get-DisplayProblem).
# Five nvidia-smi readings 0.4 s apart; idleGpuPowerW is their median.
# Present only says a monitor is plugged in and switched on, not awake (16:46: 2 present while Windows had them asleep).
# The coordinator measured at 16:41 that idle GPU power follows whether the displays are awake (77.4 W asleep, 81.5 W
# awake, both ways twice), and Corey plays with them awake, so before every launch runtime\display.py wakes them (a
# mouse move of 0 counts) and reads Windows' console display state; a launch starts only when it reads "on".
function Get-MachineState {
    $wake = try { & $Py -I (Join-Path $Runtime 'display.py') wake 2>$null | Select-Object -Last 1 | ConvertFrom-Json } catch { $null }
    Start-Sleep -Seconds 3   # the idle power below is read with the displays settled awake
    $on = @(Get-PnpDevice -Class Monitor -ErrorAction SilentlyContinue | Where-Object Present | ForEach-Object InstanceId)
    # Nov 17 00:01: with both monitors off at the plug, Windows still drives a display named "ASUS MG278" as primary at
    # 60 Hz while no monitor is Present, so the active layout (name, primary, size, refresh) is recorded too.
    $displays = try { @(& $Py -I (Join-Path $Runtime 'monitors.py') layout 2>$null | Select-Object -Last 1 | ConvertFrom-Json) } catch { @() }
    $rows = @(foreach ($i in 1..5) {
        & nvidia-smi --query-gpu=power.draw,clocks.gr,clocks.mem,pstate,utilization.gpu,temperature.gpu,memory.used --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        Start-Sleep -Milliseconds 400 })
    $w = @($rows | ForEach-Object { $v = 0.0; if ([double]::TryParse(($_ -split ',')[0].Trim(), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$v)) { $v } } | Sort-Object)
    [ordered]@{ time = (Get-Date -Format 'HH:mm:ss'); display = $wake.display; displayWake = $wake
                monitorsOn = $on.Count; monitors = $on; displays = $displays
                idleGpuPowerW = $(if ($w.Count) { $w[[int][math]::Floor($w.Count / 2)] } else { $null })
                gpuSamples = 'power.draw W, clocks.gr MHz, clocks.mem MHz, pstate, utilization.gpu %, temperature.gpu C, memory.used MiB'
                gpu = $rows }
}
# Records Get-MachineState in <launch>\machine_before.json and under machineBefore.<launch> in the run's manifest.json.
function Add-MachineState([string]$runDir, [string]$launch, [string]$spotDir, [bool]$monitorsOff = $false) {
    $m = Get-MachineState
    $m.monitorsOffAsked = $monitorsOff
    $mb = Join-Path $spotDir 'machine_before.json'
    $m | ConvertTo-Json | Set-Content $mb
    # Display state class (runtime\display_state.py; Nov 17: plug off ran about 12 % faster per frame than plug on),
    # per launch here and for the whole run under manifest displayState; gate frame-time comparisons need it equal.
    $cls = & $Py -I (Join-Path $Runtime 'display_state.py') launch $mb | ConvertFrom-Json
    $m.displayState = $cls.class; $m.displayStateWhy = $cls.why
    $m | ConvertTo-Json | Set-Content $mb
    $path = Join-Path $runDir 'manifest.json'
    $man = Get-Content $path -Raw | ConvertFrom-Json
    if (-not $man.PSObject.Properties['machineBefore']) { $man | Add-Member machineBefore ([pscustomobject]@{}) }
    $man.machineBefore | Add-Member $launch ([pscustomobject]$m) -Force
    $man | Add-Member displayState (& $Py -I (Join-Path $Runtime 'display_state.py') run $runDir | ConvertFrom-Json) -Force
    $man | ConvertTo-Json -Depth 6 | Set-Content $path
    Write-Step "$launch`: display state $($m.displayState) ($($m.displayStateWhy)); displays $($m.display) ($(@($m.displays | ForEach-Object { "$($_.name) $($_.refreshHz) Hz$(if ($_.primary) { ' primary' })" }) -join ', ')), $($m.monitorsOn) monitor(s) present, idle GPU power $($m.idleGpuPowerW) W$(if ($monitorsOff) { ', monitors off asked' })"
    $why = Get-MonitorStateProblem $m $monitorsOff
    if ($why) { throw $why }
}
# Before a launch: the displays awake (Get-DisplayAwakeProblem), or, for a launch with bench "monitorsOff": true
# (coordinator, Nov 16 23:50: Corey's monitors are switched off at the plug overnight), every monitor switched off,
# that is none listed Present, whatever the display state reads. One monitor on is neither state, so it refuses both.
function Get-MonitorStateProblem($m, [bool]$monitorsOff) {
    if (-not $monitorsOff) { return Get-DisplayAwakeProblem $m.displayWake }
    if ($m.monitorsOn -eq 0) { return $null }
    "This launch is asked with the monitors off (bench monitorsOff), but $($m.monitorsOn) monitor(s) are present ($($m.monitors -join ', ')). Switch them off at the plug, then run again."
}
# True for a launch asked with every monitor switched off (bench "monitorsOff": true).
function Test-MonitorsOff($spot) { [bool]($spot.bench -and $spot.bench.PSObject.Properties['monitorsOff'] -and $spot.bench.monitorsOff) }
# $wake: display.py wake's record, $null when it did not run.
function Get-DisplayAwakeProblem($wake) {
    if ($wake -and $wake.display -eq 'on') { return $null }
    "The displays did not wake (Windows reports '$($wake.display)' after a mouse move). Only launches with the displays awake count (idle GPU power and frame time follow the display state); wake them, then run again."
}
# display.csv (display.py watch, every 2 s through a launch): whether the displays stayed awake the whole launch, and
# the longest time without keyboard or mouse input (Windows turns them off after 1,800 s of it).
function Get-DisplayDuring([string]$csv) {
    $rows = @(if (Test-Path -LiteralPath $csv) { Import-Csv -LiteralPath $csv })
    $off = @($rows | Where-Object display -ne 'on')
    [ordered]@{ samples = $rows.Count; awakeThroughout = ($rows.Count -gt 0 -and $off.Count -eq 0); notAwake = $off.Count
                # System-wide flag: set by any program, not only the gate's hold. Run 20261116-171502 did not ask for the
                # hold, yet 158 of 183 samples had it set while the game ran, so this cannot prove whose hold it is.
                displayRequiredSamples = @($rows | Where-Object displayRequired -eq '1').Count
                firstNotAwake = $(if ($off) { "$($off[0].time) $($off[0].display)" } else { $null })
                maxIdleSeconds = $(if ($rows) { ($rows | ForEach-Object { [double]$_.idleSeconds } | Measure-Object -Maximum).Maximum } else { $null })
                # display.py's input column: samples whose new input no gate send explains (null for an older csv)
                outsideInput = $(if ($rows -and $rows[0].PSObject.Properties['input']) { @($rows | Where-Object input -eq 'outside' | ForEach-Object { "$($_.time) $($_.foreground)" }) } else { $null }) }
}
# True for a launch whose bench live steps put the displays to sleep on purpose (the awake / asleep proof): it must
# not hold them awake.
function Test-SleepsDisplays($spot) {
    [bool]@($spot.bench.liveSteps | Where-Object { $_ -and $_.PSObject.Properties['display'] -and $_.display -eq 'asleep' }).Count
}
function Get-LaunchProblem($s) {
    $why = @((Get-CSpaceProblem $s.freeMB $MinCFreeMB), (Get-MemoryProblem $s $MinCommitRoomMB)) | Where-Object { $_ }
    if ($why) { $why -join ' ' }
}

# Ubisoft's launcher can close the first game exe and start it again (Nov 16 08:49: pid 42432 exited before the Remix
# bridge started and pid 35200 replaced it; the gate called that a load failure and the next spot ran in 35200).
# Returns a $name process other than $oldId started no earlier than $oldStart, waiting up to $seconds, else $null.
function Wait-RestartedGame([int]$oldId, [datetime]$oldStart, [double]$seconds, [string]$name = 'Blacklist_game') {
    $t = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        $new = Get-Process $name -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $oldId -and $_.StartTime -ge $oldStart } | Select-Object -First 1
        if ($new -or $t.Elapsed.TotalSeconds -ge $seconds) { return $new }
        Start-Sleep 1
    }
}

# d3d9_proxy.log is shared by every launch and Windows reuses pids: run 213930's game was pid 50916 like the 18:31
# game, whose "AutoRaytracing -> on" line made the level read live 12 s after the start, so the menu keys stopped and
# drive.py timed out at the menu. A line of game $id carries its pid and a time at or after the process start $since.
function Test-GameLogLine($path, $id, [datetime]$since, [string]$pattern) {
    $from = $since.ToString('yyyy-MM-dd HH:mm:ss.fff')
    foreach ($m in @(Select-String -LiteralPath $path -Pattern "\[pid $id\] $pattern" -ErrorAction SilentlyContinue)) {
        if ([string]::CompareOrdinal($m.Line, $from) -ge 0) { return $true }
    }
    $false
}

# A level is rendering in game process $id: with Remix, the proxy logged that ray tracing switched on in it; without
# Remix (no such line), the proxy's frame log has a real camera, which the menus never publish (Benghazi, Nov 15:
# 2527 menu frames without one). The frame log is new for each launch (the Play script moves the old one away).
function Test-LevelLive($id, [datetime]$since) {
    if (-not $NoRemix) {
        return Test-GameLogLine "$S\d3d9_proxy.log" $id $since 'lightpub: AutoRaytracing -> on'
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

# Ubisoft Connect can die while it starts the game: on Nov 16 at 10:11 and 10:17 it logged "FATAL Cannot switch
# modes after logging in" and exited, Steam's launcher exited too, and no game came. A new Steam request at 10:22
# worked. True once that start has been dead for $grace seconds: the launcher or Ubisoft Connect ($starterRunning)
# was seen, and none of them has run since. $watch is @{ seen; goneAt }, kept by the caller between polls.
function Test-LaunchDead($watch, [bool]$starterRunning, [double]$now, [double]$grace = 20) {
    if ($starterRunning) { $watch.seen = $true; $watch.goneAt = $null; return $false }
    if (-not $watch.seen) { return $false }
    if ($null -eq $watch.goneAt) { $watch.goneAt = $now }
    ($now - $watch.goneAt) -ge $grace
}

# ubi_login.ps1 throws when Ubisoft Connect ("upc") is not running. In run 190131 (Nov 16 19:07) Ubisoft Connect had
# died with "FATAL Cannot switch modes after logging in", that throw ended the spot, and the Steam re-ask never ran.
# A failed sign-in is now only reported; Test-LaunchDead still decides whether the start is dead.
function Invoke-UbiLogin([string]$script) {
    try { & $script | Out-Null; $null } catch { "Ubisoft Connect sign-in failed, carrying on: $($_.Exception.Message)" }
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
    $asked = [Diagnostics.Stopwatch]::StartNew(); $watch = @{ seen = $false; goneAt = $null }; $asks = 0
    while (-not ($game = Get-Process Blacklist_game -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        if ($script:PlayProcess.HasExited) {
            throw "The Play script exited ($($script:PlayProcess.ExitCode)) before the game started: " +
                ((@(Get-Content $playLog, "$playLog.err" -ErrorAction SilentlyContinue) | Select-Object -Last 3) -join ' | ')
        }
        $starter = [bool](Get-Process Blacklist_Launcher, upc, UbisoftGameLauncher -ErrorAction SilentlyContinue)
        if (Test-LaunchDead $watch $starter $t.Elapsed.TotalSeconds) {
            $fatal = @(Get-Content 'D:\Ubisoft\Ubisoft Game Launcher\logs\launcher_log.txt' -Tail 300 -ErrorAction SilentlyContinue |
                       Where-Object { $_ -match '\bFATAL\b' }) | Select-Object -Last 1
            $why = "the launcher and Ubisoft Connect exited without starting the game ($(if ($fatal) { ($fatal -replace '\s+', ' ').Trim() } else { 'no FATAL line in launcher_log.txt' }))"
            if ($asks -ge 2) { throw "The game did not start: $why, after $asks repeated Steam requests." }
            $asks++; $watch.seen = $false; $watch.goneAt = $null; $asked.Restart()
            Write-Step "$why; asking Steam again ($asks of 2)."
            Start-Process $SteamGameUrl
        }
        if ($asked.Elapsed.TotalSeconds -gt 300) { throw 'The game did not start within 5 minutes.' }
        if (-not $loginTried -and $t.Elapsed.TotalSeconds -gt 60 -and (Get-Process UbisoftGameLauncher -ErrorAction SilentlyContinue)) {
            $loginTried = $true
            $loginFail = Invoke-UbiLogin (Join-Path (Split-Path $Tests) 'stutter-recorder\ubi_login.ps1')   # password from 1Password, never printed
            if ($loginFail) { Write-Step $loginFail }
        }
        Start-Sleep 2
    }
    $gameStart = $game.StartTime
    Start-Sleep 12; $k = 0
    while (-not (Test-LevelLive $game.Id $game.StartTime)) {
        if ($t.Elapsed.TotalSeconds -gt 480) { throw 'The game did not reach the level within 8 minutes.' }
        if ($game.HasExited) {
            $restarted = Wait-RestartedGame $game.Id $gameStart 120
            if ($restarted) {
                Write-Step "The launcher restarted the game: pid $($game.Id) exited, following pid $($restarted.Id)."
                $game = $restarted; $gameStart = $game.StartTime
                Start-Sleep 12
                continue
            }
            # The next launch overwrites the Remix and bridge logs: keep them (Nov 16 07:07, a launch died inside
            # CreateDevice and nothing showed why).
            $keep = Join-Path $runDir "load-failure-$($game.Id)"
            New-Item -ItemType Directory -Force $keep | Out-Null
            Copy-Item "$S\rtx-remix\logs\*.log" $keep -ErrorAction SilentlyContinue
            $from = $game.StartTime.ToString('yyyy-MM-dd HH:mm:ss.fff')
            Get-Content $log -Tail 20000 -ErrorAction SilentlyContinue |
                Where-Object { $_ -match "\[pid $($game.Id)\]" -and [string]::CompareOrdinal($_, $from) -ge 0 } |
                Set-Content (Join-Path $keep 'd3d9_proxy.log')
            throw "The game exited while loading (logs in $keep)."
        }
        # Only a bridge started after this game counts: on Nov 16 a bridge from elsewhere stopped con_bright_hall
        # although the proxy log showed the game chained the Windows d3d9.
        $bridge = Get-Process NvRemixBridge -ErrorAction SilentlyContinue | Where-Object { $_.StartTime -ge $game.StartTime } | Select-Object -First 1
        if ($NoRemix -and ((Test-GameLogLine $log $game.Id $game.StartTime 'chained RTX Remix DLL') -or $bridge)) {
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

# Spot configs nest deeper than ConvertTo-Json's depth limit: worker B's see-through variants keep [section, key, value]
# triples under bench.switchPoses[].switches[].ini, and -Depth 5 wrote them into spot_config.json as the text
# "System.Object[]" (20261117-165257-settings: drive.py died in switch_test before any probe step). Every copy and
# write of a spot goes through ConvertTo-SpotJson, which refuses output where a nested value became text.
function Test-JsonCut([string]$json) {
    $m = [regex]::Match($json, '.{0,60}"(System\.Object\[\]|@\{[^"]*)".{0,20}')
    if ($m.Success) { $m.Value.Trim() } else { $null }
}
function ConvertTo-SpotJson($o) {
    $json = $o | ConvertTo-Json -Depth 100
    $cut = Test-JsonCut $json
    if ($cut) { throw "spot config cut short by ConvertTo-Json (a nested value became text): $cut" }
    $json
}

$sweep = $null
if ($Variants) {
    if ($NoRemix -or $CaptureCheckpoint) { throw '-Variants measures Remix settings at an existing spot.' }
    $sweep = Get-Content $Variants -Raw | ConvertFrom-Json
    $Spots = @($sweep.spot)
}
$runDir = if ($Record) { Join-Path $RecordingDir "recording-$Stamp" } else { Join-Path $ResultsRoot $(if ($NoRemix) { "reference-$Stamp" } elseif ($sweep) { "$Stamp-settings" } else { $Stamp }) }
if ($NoRemix -and $CaptureCheckpoint) { throw '-NoRemix records the reference at existing spots; capture checkpoints with Remix.' }
$modeNote = if ($Record) { 'recording.json' } else { 'reference.json' }
if ($Record) {
    if ($Spots.Count -ne 1) { throw '-Record records one spot per run: -Record -Spots <name>.' }
    if (-not (Test-Path -LiteralPath $RecorderDll)) { throw "The apitrace recorder is missing: $RecorderDll" }
    $RecordTrace = Join-Path $TraceDir "$Stamp-$($Spots[0]).trace"
    New-Item -ItemType Directory -Force $TraceDir | Out-Null
    $IniKeys = @("Proxy.ChainDll=$RecorderDll", 'Replay.Record=1', "Replay.TraceFile=$RecordTrace") + $IniKeys
}
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
# -IniKeys "Section.Key=Value" (coordinator, Nov 17: the FlareStandIns=0 Remix run at four spots): set in this run's
# proxy ini for every spot of the launch and recorded in manifest iniKeys; both ini copies are written back from the
# backups at the end like every run's ini. Section names hold letters only, so "RemixConfig.rtx.a.b=1" is key rtx.a.b.
# One object per argument: launch 20261117-183718 kept each as a 3-item array, PowerShell unrolled the only one into
# its three strings, and each string was then split into letters, so the ini got "[L] i=g", "[F] l=a", "[0] =" and
# no FlareStandIns=0 (proxy.log: "flare stand-ins 1"); the read-back passed on the letters.
function ConvertFrom-IniKeyArg([string[]]$items) {
    foreach ($s in $items) {
        if ($s -notmatch '^([A-Za-z]+)\.([^=\s]+)=([^\r\n]*)$') { throw "-IniKeys '$s' is not Section.Key=Value" }
        [pscustomobject]@{ Section = $Matches[1]; Key = $Matches[2]; Value = $Matches[3] }
    }
}
# The -IniKeys arguments set in an ini text, each read back by Set-IniKey.
function Set-IniKeys([string]$text, [string[]]$args_) {
    foreach ($i in @(ConvertFrom-IniKeyArg $args_)) { $text = Set-IniKey $text $i.Section $i.Key $i.Value }
    $text
}
# The value of key in [section] of an ini text, $null when absent (the proxy reads a key only inside its own section).
function Get-IniKey([string]$text, [string]$section, [string]$key) {
    $h = [regex]::Match($text, "(?m)^[ \t]*\[$([regex]::Escape($section))\][ \t]*(?=\r?$)")
    if (-not $h.Success) { return $null }
    $nx = ([regex]'(?m)^[ \t]*\[').Match($text, $h.Index + $h.Length)
    $body = $text.Substring($h.Index + $h.Length, $(if ($nx.Success) { $nx.Index } else { $text.Length }) - $h.Index - $h.Length)
    $m = [regex]::Match($body, "(?m)^$([regex]::Escape($key))[ \t]*=[ \t]*([^\r\n]*)")
    if ($m.Success) { $m.Groups[1].Value.TrimEnd() } else { $null }
}
# Sets key=value in [section] (drive.py ini_with's rule: the section is added at the end when absent), then reads it back.
function Set-IniKey([string]$text, [string]$section, [string]$key, [string]$value) {
    $line = if ($section -eq 'RemixConfig') { "$key = $value" } else { "$key=$value" }
    $h = [regex]::Match($text, "(?m)^[ \t]*\[$([regex]::Escape($section))\][ \t]*(?=\r?$)")
    if (-not $h.Success) { $text = $text.TrimEnd() + "`r`n[$section]`r`n$line`r`n" }
    else {
        $start = $h.Index + $h.Length
        $nx = ([regex]'(?m)^[ \t]*\[').Match($text, $start)
        $end = if ($nx.Success) { $nx.Index } else { $text.Length }
        $body = $text.Substring($start, $end - $start)
        $pat = [regex]"(?m)^$([regex]::Escape($key))[ \t]*=[^\r\n]*"
        $body = if ($pat.IsMatch($body)) { $pat.Replace($body, $line, 1) } else { "`r`n$line" + $body }
        $text = $text.Substring(0, $start) + $body + $text.Substring($end)
    }
    if ((Get-IniKey $text $section $key) -ne $value) { throw "Could not set [$section] $key=$value" }
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
        $copy = ConvertTo-SpotJson $selected[0] | ConvertFrom-Json
        $bench = ConvertTo-SpotJson $sweep.bench | ConvertFrom-Json   # a variant's own bench keys win
        foreach ($p in @(if ($v.bench) { $v.bench.PSObject.Properties })) { $bench | Add-Member $p.Name $p.Value -Force }
        # The proxy prints some keys only on its load line, before the spot starts (200741), so a variant's proxy.log
        # keeps the whole game's lines unless the file says otherwise.
        if (-not $bench.PSObject.Properties['proxyLogWhole']) { $bench | Add-Member proxyLogWhole $true }
        $copy | Add-Member bench $bench -Force
        [pscustomobject]@{ dir = $v.name; spot = $copy; variant = $v }
    }
} else { foreach ($one in $selected) { [pscustomobject]@{ dir = $one.name; spot = $one; variant = $null } } })
# Every spot's config is checked before the game starts, so a config that cannot be written whole stops here.
foreach ($cfgRun in $runs) { $null = ConvertTo-SpotJson $cfgRun.spot }
# This run closes the game between spots. It must never close a game it did not start (Corey's own session).
$running = Get-Process Blacklist_game, Blacklist_DX11_game, Blacklist_Launcher, NvRemixBridge -ErrorAction SilentlyContinue | Select-Object -First 1
if ($running) { throw "$($running.Name) is already running (pid $($running.Id), started $($running.StartTime.ToString('HH:mm:ss'))). Close it yourself or wait: this script does not close a game it did not start." }
$otherLock = 'D:\BlacklistOverhaul\experiments\GAME_IN_USE.txt'
if (Test-Path $otherLock) { throw "Someone else has the game: $(Get-Content $otherLock -Raw)" }
if (Test-Path $GameLock) { throw "Someone else has the game: $(Get-Content $GameLock -Raw)" }
if ($NoRemix) { $displayProblem = Get-DisplayProblem @(Get-PnpDevice -Class Monitor -ErrorAction SilentlyContinue); if ($displayProblem) { throw $displayProblem } }
$cSpace = Get-LaunchProblem (Get-CDriveSample); if ($cSpace) { throw $cSpace }
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
$remixCommit = Get-InstalledRemixCommit (Join-Path $Setup 'STAGED_REMIX.json') (Join-Path $RuntimeSource 'd3d9.dll')
$remixSourceCommit = git -C $RemixSrc rev-parse --short HEAD 2>$null
# remixInstalledCommit is the build the game loads; remixSourceCommit is the dxvk-remix checkout, which may hold a
# newer build that is not staged (coordinator, Nov 17: one "remixCommit" read as the installed build when it was not).
$build = [ordered]@{ remixInstalledCommit = $remixCommit; remixSourceCommit = $remixSourceCommit
                     remixRuntimeSha256 = $runtimeHash; remixClientSha256 = $RemixClientHash
                     proxySha256 = Get-Hash (Join-Path $Setup 'd3d9.dll') }
# The last full offline suite (run_all_offline.ps1 writes $SuiteRecord when it ends) against the tree this launch uses.
# sameTree only when that run passed, the tools commit, dxvk-remix commit, proxy and staged runtime all match, and no
# tracked file was modified then or now (coordinator, Nov 16: any difference means the suite runs).
function Get-SuiteRecord([string]$path, $now) {
    $rec = if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } else { $null }
    $pass = if ($rec) { $rec.lastPass } else { $null }
    $diff = @()
    if (-not $pass) { $diff += 'no recorded full-suite pass' }
    else {
        if ($rec.lastRun -and -not $rec.lastRun.passed) { $diff += "the last run, at $($rec.lastRun.finished), failed" }
        foreach ($k in 'toolsCommit', 'remixSourceCommit', 'remixInstalledCommit', 'proxySha256', 'runtimeSha256') {
            $was = if ("$($pass.$k)") { $pass.$k } else { 'not recorded' }
            if ("$($pass.$k)" -ne "$($now.$k)") { $diff += "$k $was then, $($now.$k) now" }
        }
        if (@($pass.toolsModified).Count) { $diff += "modified when it passed: $(@($pass.toolsModified).Trim() -join ', ')" }
        if (@($now.toolsModified).Count) { $diff += "modified now: $(@($now.toolsModified).Trim() -join ', ')" }
    }
    $same = -not $diff
    $line = if (-not $pass) { 'full suite: no recorded pass' }
            else { "full suite last passed at $($pass.finished) on $($pass.toolsCommit); same tree now: " +
                   $(if ($same) { 'yes' } else { "no ($($diff -join '; '))" }) }
    [ordered]@{ lastPass = $pass; lastRun = $(if ($rec) { $rec.lastRun }); sameTree = $same; differences = $diff; line = $line }
}
# offlineSuite (coordinator, Nov 16): which offline suite this launch relies on (-SuiteNote for anything more, such as
# "only the touched tests ran on <commit>"), the last recorded full-suite run, and the tests commit with modified files.
$testsModified = @(git -C (Split-Path $Tests) status --porcelain --untracked-files=no 2>$null)
$suiteRec = Get-SuiteRecord $SuiteRecord ([ordered]@{ toolsCommit = (git -C (Split-Path $Tests) rev-parse --short HEAD 2>$null)
    remixSourceCommit = $remixSourceCommit; remixInstalledCommit = $remixCommit; proxySha256 = $build.proxySha256
    runtimeSha256 = $runtimeHash; toolsModified = $testsModified })
$suite = [ordered]@{ note = $SuiteNote; record = $suiteRec; testsCommit = (git -C (Split-Path $Tests) rev-parse --short HEAD 2>$null)
                     testsModified = $testsModified }
$iniItems = @(ConvertFrom-IniKeyArg $IniKeys)
@{ spots = @($selected | ForEach-Object name); started = $Stamp; noRemix = [bool]$NoRemix; build = $build; offlineSuite = $suite
   variants = @(if ($sweep) { $runs | ForEach-Object dir }); iniKeys = @($IniKeys); recording = [bool]$Record } | ConvertTo-Json -Depth 6 | Set-Content (Join-Path $runDir 'manifest.json')
Write-Step "Builds: dxvk-remix installed $remixCommit (source checkout $remixSourceCommit), proxy $($build.proxySha256.Substring(0, 12))"
Write-Step $suiteRec.line

# The pid line tells the Play script this run is alive (it refuses Corey's launch) and that the Play script this run
# starts is its own (it goes through); a lock whose process is gone counts as left by a crash.
$lockOwner = "pid $PID started $((Get-Process -Id $PID).StartTime.ToUniversalTime().ToString('o'))"
try { New-Item -ItemType File -Path $GameLock -Value "verify_runtime.ps1 runtime gate $([DateTime]::UtcNow.ToString('u')) run $runDir`r`n$lockOwner" | Out-Null }
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
           check = $(if ($Record) { "drive.py fails the spot unless the proxy log says this game process chained $RecorderDll (spot.json renderer 'recorder')" }
                     else { 'drive.py fails a spot unless the proxy log says this game process chained \Windows\SysWOW64\d3d9.dll (spot.json renderer); the load wait throws if Remix or NvRemixBridge loads' })
           recording = $(if ($Record) { [ordered]@{ note = 'a recording for the frame replay rig, drawn through the apitrace recorder: not a reference'
                                                    recorder = $RecorderDll; trace = $RecordTrace; iniKeys = @($IniKeys) } })
           restore = 'both ini copies are written back from the backups taken at the start; see restored below' } |
            ConvertTo-Json -Depth 4 | Set-Content (Join-Path $runDir $modeNote)
    }
    $gateIni = Set-IniKeys $gateIni $IniKeys
    if ($iniItems.Count) {
        Write-Step "ini keys for the whole launch, read back: $(($iniItems | ForEach-Object { "[$($_.Section)] $($_.Key)=$(Get-IniKey $gateIni $_.Section $_.Key)" }) -join ', ')"
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
        $smi = $null; $mem = $null; $eng = $null; $disp = $null; $hitchList = "$S\d3d9_proxy_hitches.txt"
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
                    @(if ($NoRemix) { '--signal', 'camera' }) +
                    # "cinematicDrawDumpAt": one proxy draw dump that many seconds into the cutscene (worker F, Nov 16).
                    @(if ($spot.cinematicDrawDumpAt) { '--dump-at', [string]$spot.cinematicDrawDumpAt, '--ini', "$S\d3d9_proxy.ini", '--system', $S }) |
                    ForEach-Object { '"' + $_ + '"' }) }
            # GPU clocks, temperature, power, clock-limit reasons and memory once a second for the whole launch
            # (worker C, Nov 16: what puts a launch in the slow Ray Reconstruction mode).
            # One nvidia-smi call per sample (runtime\gpu_sampler.ps1): -lms buffers 4 KB and the kill below lost the tail.
            $smi = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ('"' + (Join-Path $Runtime 'gpu_sampler.ps1') + '"'), '-Out', ('"' + (Join-Path $spotDir 'gpu_run.csv') + '"'),
                '-Query', 'timestamp,clocks.gr,clocks.mem,clocks.sm,temperature.gpu,power.draw,power.limit,clocks_throttle_reasons.active,memory.used,memory.total,pstate')
            # GPU use per program (pid and engine type) once a second for the whole launch (coordinator, Nov 17: GPU time
            # other programs such as dwm.exe or the NVIDIA overlay use, against the plug-on vs plug-off frame time). Read by
            # runtime\gpu_engines.py; about 1 % of one CPU core once running.
            $eng = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ('"' + (Join-Path $Runtime 'gpu_engines_sampler.ps1') + '"'), '-Out', ('"' + (Join-Path $spotDir 'gpu_engines.csv') + '"'))
            # Memory, page file and C: every 2 s for the whole launch (coordinator, Nov 16: lined up against freezes).
            $mem = Start-Process powershell.exe -WindowStyle Hidden -PassThru -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', ('"' + (Join-Path $Runtime 'mem_sampler.ps1') + '"'), '-Out', ('"' + (Join-Path $spotDir 'memory.csv') + '"'))
            # The proxy appends its hitch list across launches (worker C, Nov 16): each launch starts it empty, and one
            # left from outside this run is kept beside this launch.
            if (Test-Path -LiteralPath $hitchList) { Move-Item -LiteralPath $hitchList (Join-Path $spotDir 'proxy_hitches_before_launch.txt') -Force }
            $cSpace = Get-LaunchProblem (Add-CDriveSample $runDir $run.dir 'before')
            if ($cSpace) { throw $cSpace }
            Add-MachineState $runDir $run.dir $spotDir (Test-MonitorsOff $spot)
            # Display state and time since the last input every 2 s (coordinator, Nov 16: a launch whose displays fall
            # asleep halfway must be marked; manifest displayDuring.<launch>). The same process keeps the displays awake
            # for the launch (ES_DISPLAY_REQUIRED, dropped by Windows when it is killed below), except in a launch whose
            # live steps put them to sleep on purpose. It starts after Add-MachineState's wake, so its first sample is not the idle
            # time from before the launch (run 20261116-174116 began with 1235.6 s, then 1.9 s).
            $keepAwake = -not (Test-SleepsDisplays $spot)
            $disp = Start-Process $Py -WindowStyle Hidden -PassThru -ArgumentList (@('-I', ('"' + (Join-Path $Runtime 'display.py') + '"'),
                'watch', '--out', ('"' + (Join-Path $spotDir 'display.csv') + '"')) + @(if ($keepAwake) { '--keep-awake' }))
            [GateInput]::SendLog = Join-Path $spotDir 'gate_sends_verify.csv'   # the checkpoint-load taps are the gate's
            $game = Start-GameAtCheckpoint
            # Other programs holding the GPU (Windows reports their names; per-process memory reads N/A).
            & nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv 2>&1 | Set-Content (Join-Path $spotDir 'gpu_apps.csv')
            if ($cine) {
                if (-not $cine.WaitForExit(120000)) { $cine.Kill(); Write-Step "$($spot.name): cutscene recorder timed out" }
                Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'cinematic.log') -Tail 1))"
            }
            ConvertTo-SpotJson $spot | Set-Content (Join-Path $spotDir 'spot_config.json')
            $driveArgs = @((Join-Path $Runtime 'drive.py'), 'spot', '--pid', $game.Id) + $hashArgs + @(
                '--spot', (Join-Path $spotDir 'spot_config.json'), '--checkpoint', (Join-Path $cp 'checkpoint.json'),
                '--system', $S, '--root', $Root, '--out', $spotDir) + @(if ($NoRemix) { '--no-remix' }) + @(if ($Record) { '--recording' })
            $drive = Start-Process $Py -ArgumentList ($driveArgs | ForEach-Object { '"' + $_ + '"' }) -NoNewWindow -PassThru `
                -RedirectStandardOutput (Join-Path $spotDir 'drive.log') -RedirectStandardError (Join-Path $spotDir 'drive.err')
            if (-not $drive.WaitForExit(40 * 60 * 1000)) { $drive.Kill(); Write-Step "$($spot.name): drive.py timed out (40 min)" }
            Write-Step "$($spot.name): $((Get-Content (Join-Path $spotDir 'drive.log') -Tail 1))"
            Add-CDriveSample $runDir $run.dir 'end' | Out-Null   # the game still runs: the page file is at its largest
            Stop-Game   # the next launch starts remix-dxvk.log over; gate.py's clean_quit reads this spot's copy
            Copy-Item -LiteralPath "$S\rtx-remix\logs\remix-dxvk.log" $spotDir -ErrorAction SilentlyContinue
            if (Test-Path -LiteralPath $hitchList) { Move-Item -LiteralPath $hitchList (Join-Path $spotDir 'proxy_hitches.txt') -Force }
            # A quit spot (worker C): bridge32.log and bridge64.log show the window focus and the Terminate command.
            if ($spot.quit) { Copy-Item "$S\rtx-remix\logs\*.log" $spotDir -ErrorAction SilentlyContinue }
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
                    $proxySeen[$p.Name] = @(Get-ProxyKeySeen $proxyLog $p.Name)
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
            try { Add-CDriveSample $runDir $run.dir 'end' | Out-Null } catch { }
            if (-not (Test-Path (Join-Path $spotDir 'spot.json'))) {
                @{ name = $spot.name; error = $_.Exception.Message; windows = @() } | ConvertTo-Json | Set-Content (Join-Path $spotDir 'spot.json')
            }
        } finally {
            foreach ($p in $smi, $mem, $eng, $disp) { if ($p) { try { if (-not $p.HasExited) { $p.Kill() } } catch { } } }
            if ($disp) { try {
                $d = Get-DisplayDuring (Join-Path $spotDir 'display.csv'); $d.keepAwakeAsked = $keepAwake
                $path = Join-Path $runDir 'manifest.json'; $man = Get-Content $path -Raw | ConvertFrom-Json
                if (-not $man.PSObject.Properties['displayDuring']) { $man | Add-Member displayDuring ([pscustomobject]@{}) }
                $man.displayDuring | Add-Member $run.dir ([pscustomobject]$d) -Force
                $man | ConvertTo-Json -Depth 6 | Set-Content $path
                Write-Step "$($run.dir): displays awake throughout: $($d.awakeThroughout) ($($d.samples) samples, $($d.notAwake) not awake$(if ($d.firstNotAwake) { ', first ' + $d.firstNotAwake }), longest without input $($d.maxIdleSeconds) s, display-required flag set by some program in $($d.displayRequiredSamples) samples$(if (-not $keepAwake) { ', not asked: this launch sleeps them on purpose' })); outside input: $(if ($null -eq $d.outsideInput) { 'not recorded' } elseif ($d.outsideInput.Count) { "$($d.outsideInput.Count) samples, first $($d.outsideInput[0])" } else { 'none' })"
            } catch { Write-Step "$($run.dir): display log not read: $($_.Exception.Message)" } }
            $smi = $null; $mem = $null; $eng = $null; $disp = $null
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
    if ($Record) { try {   # the game is closed: the proxy's side recording goes beside the spot's other files
        foreach ($f in 'd3d9_proxy_replay_calls.csv', 'd3d9_proxy_replay_lights.bin') {
            if (Test-Path -LiteralPath "$S\$f") { Move-Item -LiteralPath "$S\$f" (Join-Path (Join-Path $runDir $Spots[0]) $f) -Force }
        }
    } catch { $putBackFailed += "moving the side recording: $($_.Exception.Message)" } }
    if ($NoRemix -and (Test-Path (Join-Path $runDir $modeNote))) { try {
        $note = Get-Content (Join-Path $runDir $modeNote) -Raw | ConvertFrom-Json
        $note | Add-Member restored ([ordered]@{
            setupIniAsBefore = ([IO.File]::ReadAllText($setupIniPath) -eq $setupIniBackup)
            gameIniAsBefore  = ([IO.File]::ReadAllText("$S\d3d9_proxy.ini") -eq $(if ($SkipInstall) { $iniBackup } else { $setupIniBackup }))
            playMismatches   = @(Get-PlayMismatches); launcherApi = (Get-StartupApi) }) -Force
        $note | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $runDir $modeNote)
        Write-Step "Remix settings put back: $(($note.restored | ConvertTo-Json -Compress))"
    } catch { Write-Step "Could not record the restore check: $($_.Exception.Message)" } }
    Show-PutBackFailures $putBackFailed
    if ($putBackFailed.Count) { Write-Step "Put the rest back with: verify_runtime.ps1 -RestoreFrom $runDir" }
    Remove-Item -LiteralPath $GameLock -ErrorAction SilentlyContinue
}
if ($putBackFailed.Count -and -not $sweep -and -not $CaptureCheckpoint -and -not $Record) { [void](Gate $runDir); exit 3 }
if ($putBackFailed.Count) { exit 3 }
if ($CaptureCheckpoint) { exit 0 }
if ($Record) {   # a recording is not gated: the replay rig's own checks (tests\replay) decide what it is good for
    $spotDir = Join-Path $runDir $Spots[0]
    $have = [ordered]@{ trace = $RecordTrace; traceMB = $(if (Test-Path -LiteralPath $RecordTrace) { [int]((Get-Item -LiteralPath $RecordTrace).Length / 1MB) } else { 0 })
                        calls = (Test-Path (Join-Path $spotDir 'd3d9_proxy_replay_calls.csv')); lights = (Test-Path (Join-Path $spotDir 'd3d9_proxy_replay_lights.bin'))
                        renderer = $(try { (Get-Content (Join-Path $spotDir 'spot.json') -Raw | ConvertFrom-Json).renderer.kind } catch { $null }) }
    "Recording: $($have | ConvertTo-Json -Compress)"
    "Result: $runDir"
    exit $(if ($have.traceMB -and $have.calls -and $have.renderer -eq 'recorder') { 0 } else { 1 })
}
if ($sweep) {   # numbers for the requester to weigh, not a pass/fail
    & $Py (Join-Path $Runtime 'settings_report.py') $runDir | Out-Host
    "Result: $(Join-Path $runDir 'settings.md')"
    exit $LASTEXITCODE
}
$code = Gate $runDir
"Result: $(Join-Path $runDir 'result.md')"
exit $code
