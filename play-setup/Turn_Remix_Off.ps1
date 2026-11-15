# Turn Remix off: puts the game back to its normal (resting) state. Both DLLs go back to .off,
# the Remix config files are removed (SYSTEM and game root), and the launcher setting is put
# back if a Play run left it on DX9. The mod links (glow pack, materials) are removed; captures and the .trex
# runtime stay where they are but do nothing while the DLLs are off (the game then loads Windows' own d3d9.dll).
#   -NoPause  never wait for Enter (for scripted tests)
param([switch]$NoPause)
. (Join-Path $PSScriptRoot 'RemixPlayCommon.ps1')

function Finish($text, $color, $code) {
    Write-Host ''
    Write-Host $text -ForegroundColor $color
    if (-not $NoPause) { Read-Host 'Press Enter to close' | Out-Null }
    exit $code
}

try {
    Write-Host 'Turn Remix off' -ForegroundColor Cyan

    $running = Get-RunningGame
    if ($running) {
        Finish ("Quit the game first (still running: " +
            (($running | ForEach-Object { "$($_.Name) $($_.Id)" }) -join ', ') + ').') Yellow 1
    }
    if ((Get-LockText) -and -not (Test-OwnLock)) {
        Write-Host 'Note: another test says it is using the game folder:' -ForegroundColor Yellow
        Write-Host (Get-LockText)
        if ($NoPause) { Finish 'Not changing anything while it is in use.' Yellow 1 }
        $answer = Read-Host 'Turn Remix off anyway? Type Y and Enter to continue'
        if ($answer -ne 'Y' -and $answer -ne 'y') { Finish 'Nothing changed.' Yellow 1 }
    }

    $mismatches = @(Get-OffMismatches)
    if ($mismatches.Count -eq 0) {
        Write-Step 'Remix is already off.'
    } else {
        $restored = Restore-LauncherApi
        if ($restored) { Write-Step "Launcher setting put back: $restored" }
        $backup = Uninstall-PlaySetup
        Write-Step "Backed up current files to $backup"
    }

    if (Resume-Dx11Mod) { Write-Step 'DX11 lighting mod put back.' }
    if (Test-OwnLock) { Remove-Item -LiteralPath $Lock; Write-Step 'Cleared the "game in use" note.' }

    $mismatches = @(Get-OffMismatches)
    if ($mismatches.Count) { Finish ('Check failed: ' + ($mismatches -join ', ')) Red 1 }
    Write-Step ('Launcher preference: ' + (Get-StartupApi) + '  (5 = DX11, the normal setting)')
    Finish 'Remix is OFF. Start Blacklist normally from Steam.' Green 0
} catch {
    Finish ("Error: " + $_.Exception.Message) Red 1
}
