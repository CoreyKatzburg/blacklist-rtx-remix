# Put-back for verify_runtime.ps1 (coordinator review, Nov 16). The run used to keep its ini and rtx.conf backups
# only in memory, so a killed run (cancelled call, timeout, crash: its finally block never runs) left
# play-setup\d3d9_proxy.ini in its no-Remix form for Corey's next play and for the next run's "before" copy.
# Now every file a run changes is copied into <run>\file-backup\ before the first change, restore.json says which
# copy goes where, the changed setup ini carries a marker line naming the run, and a new run refuses to start while
# such a line is left. verify_runtime.ps1 -RestoreFrom <run folder> puts a killed run's files back.
# Dot-sourced by verify_runtime.ps1 and test_run_files.ps1; Windows PowerShell 5.1 compatible.

$RunMarkerText = 'verify_runtime.ps1 run in progress, put back with: verify_runtime.ps1 -RestoreFrom '

# $targets: ordered map of file to put back -> file holding its "before" text now (usually the same file; the game's
# ini copy is put back from the setup ini when the run installs the setup). $saveDir: folder whose *.save files the
# run replaces, copied to <run>\save-backup.
function Save-RunFiles([string]$runDir, $targets, [string]$saveDir) {
    $dir = Join-Path $runDir 'file-backup'
    New-Item -ItemType Directory -Force $dir | Out-Null
    $files = [ordered]@{}
    $i = 0
    foreach ($t in @($targets.Keys)) {
        $copy = Join-Path $dir ('{0:D2}_{1}' -f $i, (Split-Path $targets[$t] -Leaf))
        Copy-Item -LiteralPath $targets[$t] $copy -Force
        $files[$t] = $copy
        $i++
    }
    $saves = $null
    if ($saveDir) {
        $saves = [ordered]@{ dir = $saveDir; backup = (Join-Path $runDir 'save-backup') }
        New-Item -ItemType Directory -Force $saves.backup | Out-Null
        Copy-Item (Join-Path $saveDir '*') $saves.backup -Force
    }
    [ordered]@{ files = $files; saves = $saves } | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $dir 'restore.json')
}

# Puts every recorded file back, each step on its own so one failure (a locked file) cannot skip the rest. Returns
# the failures as text; empty when everything is back.
function Restore-RunFiles([string]$runDir) {
    $failed = @()
    $recFile = Join-Path $runDir 'file-backup\restore.json'
    if (-not (Test-Path -LiteralPath $recFile)) { return @("no $recFile") }
    $rec = Get-Content -LiteralPath $recFile -Raw | ConvertFrom-Json
    foreach ($p in @($rec.files.PSObject.Properties)) {
        try { Copy-Item -LiteralPath $p.Value $p.Name -Force -ErrorAction Stop }
        catch { $failed += "$($p.Name): $($_.Exception.Message)" }
    }
    if ($rec.saves) {
        try {
            Get-ChildItem -LiteralPath $rec.saves.dir -Filter '*.save' -ErrorAction Stop | Remove-Item -Force -ErrorAction Stop
            Copy-Item (Join-Path $rec.saves.backup '*') $rec.saves.dir -Force -ErrorAction Stop
        } catch { $failed += "saves in $($rec.saves.dir): $($_.Exception.Message)" }
    }
    $failed
}

# The proxy ini text with the marker line right under [Proxy].
function Add-RunMarker([string]$text, [string]$runDir) {
    $out = [Regex]::Replace($text, '(?m)^\[Proxy\][ \t]*\r?\n', "[Proxy]`r`n; $RunMarkerText$runDir`r`n", 1)
    if ($out -eq $text) { throw 'No [Proxy] section in the setup proxy ini.' }
    $out
}

# Messages for files that still carry a run's marker (or the older -NoRemix marker, which named no folder).
function Get-LeftOverRuns($paths) {
    foreach ($p in @($paths)) {
        if (-not (Test-Path -LiteralPath $p)) { continue }
        foreach ($m in @(Select-String -LiteralPath $p -SimpleMatch $RunMarkerText)) {
            $folder = $m.Line.Substring($m.Line.IndexOf($RunMarkerText) + $RunMarkerText.Length).Trim()
            "$p still has the changes of run $folder; put them back with: verify_runtime.ps1 -RestoreFrom $folder"
        }
        if (Select-String -LiteralPath $p -SimpleMatch '; verify_runtime.ps1 -NoRemix' -Quiet) {
            if (-not (Select-String -LiteralPath $p -SimpleMatch $RunMarkerText -Quiet)) {
                "$p still has a no-Remix reference run's settings (older run, no folder recorded): copy the ini back from that run's file-backup or play-setup\backups"
            }
        }
    }
}
