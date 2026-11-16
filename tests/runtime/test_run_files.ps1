# Game-free test of verify_runtime.ps1's put-back (runtime\run_files.ps1), against scratch files only. Exit 0 = all
# checks pass. Run with Windows PowerShell 5.1: powershell -File test_run_files.ps1
# The fakes: a run "killed" after changing the files (only the on-disk copies can put them back, and the next run must
# see its marker), and one file locked during the put-back (the other files must still go back, and the failure must
# be reported, not swallowed).
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'run_files.ps1')
$scratch = Join-Path ([IO.Path]::GetTempPath()) ('remix-runfiles-test-' + [Guid]::NewGuid().ToString('N').Substring(0, 8))
$failures = 0
function Check($name, $ok, $detail = '') {
    if ($ok) { Write-Host "[PASS] $name" } else { Write-Host "[FAIL] $name $detail"; $script:failures++ }
}
$lock = $null
try {
    $setup = Join-Path $scratch 'setup\d3d9_proxy.ini'; $game = Join-Path $scratch 'game\d3d9_proxy.ini'
    $inputIni = Join-Path $scratch 'docs\inputTranslations.ini'; $saves = Join-Path $scratch 'saves'
    $rtxSetup = Join-Path $scratch 'setup\rtx.conf'; $rtxGame = Join-Path $scratch 'game\rtx.conf'
    $run = Join-Path $scratch 'runs\20261116-010000'
    New-Item -ItemType Directory -Force (Split-Path $setup), (Split-Path $game), (Split-Path $inputIni), $saves, $run | Out-Null
    $setupText = "[Proxy]`r`nEnableDebuggingTelemetry=0`r`n"
    [IO.File]::WriteAllText($setup, $setupText)
    [IO.File]::WriteAllText($game, 'old game copy')
    [IO.File]::WriteAllText($inputIni, "[Engine.Input]`r`nF9=Old`r`n")
    [IO.File]::WriteAllText($rtxSetup, "rtx.qualityDLSS = 3`r`n")
    [IO.File]::WriteAllText($rtxGame, "rtx.qualityDLSS = 3`r`n")
    [IO.File]::WriteAllText((Join-Path $saves '1.save'), 'checkpoint A')

    Check 'clean files: no left-over run' (@(Get-LeftOverRuns @($setup, $game)).Count -eq 0)
    $targets = [ordered]@{ $setup = $setup; $inputIni = $inputIni; $game = $setup; $rtxSetup = $rtxSetup; $rtxGame = $rtxSetup }
    Save-RunFiles $run $targets $saves

    # the run changes everything, then is killed (no finally)
    $marked = Add-RunMarker "[Proxy]`r`nEnableDebuggingTelemetry=1`r`nChainDll=x`r`n" $run
    [IO.File]::WriteAllText($setup, $marked); [IO.File]::WriteAllText($game, $marked)
    [IO.File]::WriteAllText($inputIni, 'changed'); [IO.File]::WriteAllText($rtxSetup, 'rtx.qualityDLSS = 0'); [IO.File]::WriteAllText($rtxGame, 'x')
    Remove-Item (Join-Path $saves '1.save'); [IO.File]::WriteAllText((Join-Path $saves '1.save'), 'checkpoint B')

    $left = @(Get-LeftOverRuns @($setup, $game))
    Check 'a killed run is found by its marker' ($left.Count -eq 2 -and $left[0].Contains("-RestoreFrom $run")) ($left -join ' | ')
    $older = Join-Path $scratch 'older.ini'
    [IO.File]::WriteAllText($older, "[Proxy]`r`n; verify_runtime.ps1 -NoRemix (reference run)`r`nChainDll=x`r`n")
    Check 'the older -NoRemix marker is found too' (@(Get-LeftOverRuns $older).Count -eq 1)

    # one file locked during the put-back: the rest still goes back, and the failure names that file
    $lock = [IO.File]::Open($inputIni, 'Open', 'ReadWrite', 'None')
    $failed = @(Restore-RunFiles $run)
    Check 'a locked file is reported' ($failed.Count -eq 1 -and $failed[0].StartsWith($inputIni)) ($failed -join ' | ')
    Check 'the other files went back anyway' (([IO.File]::ReadAllText($setup) -eq $setupText) -and ([IO.File]::ReadAllText($game) -eq $setupText) -and
        ([IO.File]::ReadAllText($rtxGame) -eq "rtx.qualityDLSS = 3`r`n") -and ([IO.File]::ReadAllText((Join-Path $saves '1.save')) -eq 'checkpoint A'))
    $lock.Close(); $lock = $null

    $failed = @(Restore-RunFiles $run)
    Check 'after unlocking, everything is back' ($failed.Count -eq 0 -and [IO.File]::ReadAllText($inputIni) -eq "[Engine.Input]`r`nF9=Old`r`n") ($failed -join ' | ')
    Check 'no marker left after the put-back' (@(Get-LeftOverRuns @($setup, $game)).Count -eq 0)
    Check 'a folder without a backup says so' (@(Restore-RunFiles (Join-Path $scratch 'none'))[0] -like 'no *restore.json')
} catch {
    Check 'no exception' $false $_.Exception.Message
} finally {
    if ($lock) { $lock.Close() }
    if (Test-Path -LiteralPath $scratch) { [IO.Directory]::Delete($scratch, $true) }
}
if ($failures) { Write-Host "RUN FILES TEST FAILED ($failures)"; exit 1 }
Write-Host 'RUN FILES TEST PASSED'
exit 0
