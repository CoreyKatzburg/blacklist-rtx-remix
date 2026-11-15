# Play Blacklist with RTX Remix: installs the play setup if needed, then starts the game through
# Steam (so Corey's Steam launch options apply). Steam runs Blacklist_Launcher.exe, which starts
# the DX9 exe only when videoSettings.ini says StartupGraphicsApi=4, so this script switches that
# line to 4 for the launch and puts the original (normally 5 = DX11) back as soon as the game
# has started, and checks it again after the game quits. Leave this window open while you play
# (minimising it is fine): it also clears the "game in use" note when the game closes.
#   -NoLaunch  install and verify only     -NoPause  never wait for Enter (for scripted tests)
param([switch]$NoLaunch, [switch]$NoPause)
. (Join-Path $PSScriptRoot 'RemixPlayCommon.ps1')

function Stop-WithMessage($text) {
    if (Resume-Dx11Mod) { Write-Host '  DX11 lighting mod put back.' }
    $restored = Restore-LauncherApi
    if ($restored) { Write-Host "  Launcher setting put back: $restored" }
    Write-Host ''
    Write-Host $text -ForegroundColor Yellow
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
    exit 1
}

try {
    Write-Host 'Play Blacklist with Remix' -ForegroundColor Cyan

    $running = Get-RunningGame
    if ($running) {
        Stop-WithMessage ("Blacklist (or the Remix bridge) is already running: " +
            (($running | ForEach-Object { "$($_.Name) $($_.Id)" }) -join ', ') +
            ". Close it first, or switch to its window.")
    }
    # A leftover DX9 switch from a run whose window was closed early.
    $restored = Restore-LauncherApi
    if ($restored) { Write-Step "Put back the launcher setting left from last time: $restored" }

    $lockText = Get-LockText
    if ($lockText -and -not (Test-OwnLock)) {
        Write-Host ''
        Write-Host 'Another test says it is using the game folder:' -ForegroundColor Yellow
        Write-Host $lockText
        if ($NoPause) { Stop-WithMessage 'Not starting (game folder in use).' }
        $answer = Read-Host 'Start anyway? Type Y and Enter to continue, anything else to stop'
        if ($answer -ne 'Y' -and $answer -ne 'y') { Stop-WithMessage 'Not starting.' }
    }

    if ($GameExeHashes -notcontains (Get-Hash "$S\Blacklist_game.exe")) {
        Stop-WithMessage 'Blacklist_game.exe has changed (game update or Steam repair?). The camera proxy is matched to the old exe, so not starting with Remix.'
    }
    if (-not (Test-Path -LiteralPath $RemixPkgDll)) { Stop-WithMessage 'Remix package file missing: pkg\d3d9.dll.' }
    if (-not (Test-Path -LiteralPath (Join-Path $RuntimeSource 'NvRemixBridge.exe'))) {
        Stop-WithMessage 'Remix runtime source is missing: pkg\.trex\NvRemixBridge.exe.'
    }
    foreach ($file in @('d3d9.dll', 'resting\d3d9.dll.off') + @($Configs.Values)) {
        if (-not (Test-Path -LiteralPath (Join-Path $Setup $file))) { Stop-WithMessage "Setup file missing: $file" }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RemixDir 'materials\mod.usda'))) {
        Stop-WithMessage 'Remix materials mod is missing: materials\mod.usda.'
    }
    if ((Get-Hash "$Setup\d3d9.dll") -ne $ProxyHash) { Stop-WithMessage 'Setup copy of d3d9.dll has an unexpected hash.' }

    $mismatches = @(Get-PlayMismatches)
    if ($mismatches.Count -eq 0) {
        Write-Step 'Remix play setup already installed.'
    } else {
        Write-Step ('Installing Remix play setup (needed: ' + ($mismatches -join ', ') + ')')
        try { $left = @(Install-PlaySetup) }
        catch { Stop-WithMessage ("Could not install the Remix play setup: " + $_.Exception.Message) }
        if ($left.Count) { Stop-WithMessage ('Install check failed: ' + ($left -join ', ') + '. Run "Turn Remix off".') }
        Write-Step 'Installed and verified (all file hashes match).'
    }

    if ($NoLaunch) { Write-Step 'NoLaunch: not starting the game.'; exit 0 }

    # Keep the previous session's logs, in case it froze and you want them looked at.
    $logDir = Join-Path $Setup ('logs\before-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
    foreach ($log in $LogFiles) {
        if (Test-Path -LiteralPath $log) {
            Copy-Item -LiteralPath $log -Destination (Join-Path $logDir ((Get-Label $log) -replace '\\', '_'))
        }
    }
    # Per-frame traces (EnableDebuggingTelemetry=1) move with the logs, so each
    # session starts fresh instead of leaving stale traces from an earlier run.
    foreach ($trace in "$S\d3d9_proxy_frames.csv", "$S\d3d9_proxy_calls.csv", "$S\d3d9_proxy_gate.csv", "$S\rtx_telemetry.jsonl") {
        if (Test-Path -LiteralPath $trace) { Move-Item -LiteralPath $trace -Destination $logDir }
    }
    Get-ChildItem -LiteralPath (Join-Path $Setup 'logs') -Directory | Sort-Object Name -Descending |
        Select-Object -Skip 10 | Remove-Item -Recurse -Force

    $startedUtc = [DateTime]::UtcNow.ToString('yyyy-MM-dd HH:mm:ss')
    Set-Content -LiteralPath $Lock -Value "$LockTag Corey is playing Blacklist with Remix (started $startedUtc UTC by the 'Play Blacklist with Remix' shortcut). Do not change the game folder or launch the game."

    if (Suspend-Dx11Mod) { Write-Step 'DX11 lighting mod parked while Remix plays.' }
    Switch-LauncherToDx9
    Start-Process $SteamGameUrl
    Write-Host ''
    Write-Host 'Starting through Steam (DX9 with Remix, ray tracing OFF).' -ForegroundColor Green
    Write-Host '  If Ubisoft Connect asks you to sign in, do that; the game continues after.'
    Write-Host '  Alt+Home opens the Remix menu. Leave this window open (minimising it is fine).'

    Write-Host '  Steam first shows a window with the game''s CD key: click through it to continue.'

    # Steam (CD-key window) -> Blacklist_Launcher.exe -> game exe -> (Ubisoft launcher, which can
    # wait for a sign-in) -> game. Put the launcher setting back as soon as a game exe is running.
    $deadline = (Get-Date).AddMinutes(5)
    $game = $null
    while (-not $game) {
        $game = @(Get-Process -Name 'Blacklist_game', 'Blacklist_DX11_game' -ErrorAction SilentlyContinue) | Select-Object -First 1
        if ($game) { break }
        $waiting = @(Get-Process -Name 'UbisoftGameLauncher', 'Blacklist_Launcher' -ErrorAction SilentlyContinue)
        if ($waiting.Count -or (Test-SteamLaunchWaiting)) { $deadline = (Get-Date).AddMinutes(5) }
        elseif ((Get-Date) -gt $deadline) { break }
        Start-Sleep -Seconds 1
    }
    $restored = Restore-LauncherApi -KeepNote
    if ($game) { Write-Step "Game started ($($game.Name)); launcher setting put back: $restored" }
    else { Write-Step "No game process appeared within 5 minutes; launcher setting put back: $restored" }
    if ($game -and $game.Name -eq 'Blacklist_DX11_game') {
        Write-Host '  Note: the DX11 version started, so Remix is NOT active this time.' -ForegroundColor Yellow
    }

    # Wait for the game to quit: Ubisoft can restart the exe, so wait for every game/bridge
    # process (and Ubisoft's launcher, while it is waiting on a sign-in) to stay gone for 30 s.
    $quietSince = $null
    while ($true) {
        $busy = (Get-RunningGame) -or @(Get-Process -Name 'UbisoftGameLauncher' -ErrorAction SilentlyContinue).Count
        if ($busy) { $quietSince = $null }
        elseif (-not $quietSince) { $quietSince = Get-Date }
        elseif (((Get-Date) - $quietSince).TotalSeconds -ge 30) { break }
        Start-Sleep -Seconds 3
    }
    # The game may write videoSettings.ini on exit; make sure the original line is back.
    $restored = Restore-LauncherApi
    if (Resume-Dx11Mod) { Write-Step 'DX11 lighting mod put back.' }
    if (Test-OwnLock) { Remove-Item -LiteralPath $Lock }
    Write-Host "Game closed. Launcher setting: $(Get-StartupApi). Remix is still installed; use ""Turn Remix off"" to go back to normal."
    if (-not $NoPause) { Start-Sleep -Seconds 5 }
} catch {
    Stop-WithMessage ("Error: " + $_.Exception.Message)
}
