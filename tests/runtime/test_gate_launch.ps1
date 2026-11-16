# Fake cases for verify_runtime.ps1's launch recovery (Nov 16 08:49 run 084921): a game exe the launcher restarts is
# followed, and a lock left by a killed Play script is removed only when this run's Play script wrote it.
# Run: pwsh -File test_gate_launch.ps1 ; exit code 1 on any failure.
param([string]$Gate = (Join-Path (Split-Path $PSScriptRoot) 'verify_runtime.ps1'),
      [string]$Common = (Join-Path $PSScriptRoot '..\..\play-setup\RemixPlayCommon.ps1'))
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Gate, [ref]$null, [ref]$null)
foreach ($n in 'Remove-OwnPlayLock', 'Wait-RestartedGame', 'Get-DisplayProblem', 'Test-LaunchDead', 'Get-CSpaceProblem', 'Get-MemoryProblem',
           'Get-DisplayAwakeProblem', 'Get-DisplayDuring', 'Test-SleepsDisplays') {
    $f = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true) | Select-Object -First 1
    if (-not $f) { throw "missing $n" }
    . ([scriptblock]::Create($f.Extent.Text))
}
$LockTag = 'PLAY-SETUP'
$fail = 0
function Check($name, $ok) { if ($ok) { "PASS $name" } else { "FAIL $name"; $script:fail++ } }
$dir = Join-Path $env:TEMP "restartcheck-$PID"; New-Item -ItemType Directory $dir | Out-Null
$lock = Join-Path $dir 'GAME_IN_USE.txt'
$since = Get-Date
function Write-Lock($tag, $utc) { Set-Content $lock "$tag Corey is playing Blacklist with Remix (started $($utc.ToString('yyyy-MM-dd HH:mm:ss')) UTC by the 'Play Blacklist with Remix' shortcut)." }
Write-Lock 'PLAY-SETUP' $since.ToUniversalTime().AddSeconds(1)
Check 'own lock written after our Play script started is removed' ((Remove-OwnPlayLock $lock $since) -and -not (Test-Path $lock))
Write-Lock 'PLAY-SETUP' $since.ToUniversalTime().AddMinutes(-10)
Check 'lock written before our Play script started stays' ((-not (Remove-OwnPlayLock $lock $since)) -and (Test-Path $lock))
Set-Content $lock 'AGENT worker X has the game (started 2026-11-16 23:59:59 UTC)'
Check 'lock with another tag stays' ((-not (Remove-OwnPlayLock $lock $since)) -and (Test-Path $lock))
Remove-Item $lock
Check 'no lock: nothing removed' (-not (Remove-OwnPlayLock $lock $since))

# Restart: a fake "game" (ping.exe) exits and a new one starts 3 s later.
$old = Start-Process ping -ArgumentList '-n', '2', '127.0.0.1' -PassThru -WindowStyle Hidden
$oldStart = $old.StartTime; $old.WaitForExit()
$older = Get-Process PING -ErrorAction SilentlyContinue   # none expected; any found would predate the fake game
$job = Start-Job { Start-Sleep 3; Start-Process ping -ArgumentList '-n', '15', '127.0.0.1' -WindowStyle Hidden }
$new = Wait-RestartedGame $old.Id $oldStart 20 'PING'
Check 'follows the restarted exe' ($new -and $new.Id -ne $old.Id)
if ($new) { Stop-Process -Id $new.Id -Force -ErrorAction SilentlyContinue }
Receive-Job $job -Wait | Out-Null; Remove-Job $job
$t = [Diagnostics.Stopwatch]::StartNew()
Check 'no restart: returns nothing after the wait' ($null -eq (Wait-RestartedGame $old.Id $oldStart 3 'PING') -and $t.Elapsed.TotalSeconds -ge 3)
# A process that was already running before the fake game started must not count as its restart.
$pre = Start-Process ping -ArgumentList '-n', '15', '127.0.0.1' -PassThru -WindowStyle Hidden
Start-Sleep 1
$g = Start-Process ping -ArgumentList '-n', '2', '127.0.0.1' -PassThru -WindowStyle Hidden; $gStart = $g.StartTime; $g.WaitForExit()
Check 'an older process is not taken for the restart' ($null -eq (Wait-RestartedGame $g.Id $gStart 2 'PING'))
Stop-Process -Id $pre.Id -Force -ErrorAction SilentlyContinue
Remove-Item $dir -Recurse -Force
# Display check for -NoRemix (Nov 16 black references): the monitor list as Windows gave it with Corey's monitors off,
# then the same list with one monitor on.
$off = @('ACI27A8', 'HKC2731', 'DEFAULT_MONITOR') | ForEach-Object { [pscustomobject]@{ InstanceId = "DISPLAY\$_"; Present = $false } }
Check 'all monitors off: refused' ([bool](Get-DisplayProblem $off))
Check 'no monitors listed: refused' ([bool](Get-DisplayProblem @()))
$on = @($off[0], [pscustomobject]@{ InstanceId = 'DISPLAY\HKC2731'; Present = $true })
Check 'one monitor on: allowed' ($null -eq (Get-DisplayProblem $on))
# Dead start (Nov 16 10:11): launcher and Ubisoft Connect run, then both exit with no game. Each case is one poll per
# second, the seconds the starter ran, and the first second the start may count as dead (0: never).
function FirstDead([int[]]$runningAt, [int]$until) {
    $w = @{ seen = $false; goneAt = $null }
    foreach ($s in 0..$until) { if (Test-LaunchDead $w ($s -in $runningAt) $s) { return $s } }
    0
}
Check 'starter exits at 11 s: dead 20 s later, not sooner' ((FirstDead (0..10) 120) -eq 31)
Check 'starter runs until the game: never dead' ((FirstDead (0..120) 120) -eq 0)
Check 'starter not seen yet (slow Steam): never dead' ((FirstDead @() 120) -eq 0)
Check 'starter back after a gap: the gap is forgotten' ((FirstDead (@(0..10) + @(20..24)) 120) -eq 45)
# C: space (Nov 16 12:22:49): a launch that started with 17,100 MB free fell to 8,299 MB. Started with less than the
# 8,801 MB it took, it would have run C: out; the gate's limit must refuse that, and allow the 17,100 MB start.
$limit = [int]((Select-String -LiteralPath $Gate -Pattern '^\$MinCFreeMB = (\d+)').Matches[0].Groups[1].Value)
Check 'C: limit covers the largest measured fall in one launch (8,801 MB)' ($limit -gt 8801)
Check 'C: below the limit: refused' ([bool](Get-CSpaceProblem 8000 $limit))
Check 'C: as at the start of the 12:22 launch (17,100 MB): allowed' ($null -eq (Get-CSpaceProblem 17100 $limit))
# Memory room (Nov 16): the largest rise in committed memory during one launch was 12,652 MB. The machine as at the
# start of the 12:33:46 launch (32,621 MB RAM, 26,496 MB page file of 65,536 at most, 16,719 MB free on C:, 47,445
# committed) has room; the same with 2,000 MB free on C:, or with the page file at its maximum, does not.
$need = [int]((Select-String -LiteralPath $Gate -Pattern '^\$MinCommitRoomMB = (\d+)').Matches[0].Groups[1].Value)
function Machine($commit, $pagefile, $cFree) { [ordered]@{ ramMB = 32621; pagefileMB = $pagefile; pagefileMaxMB = 65536; freeMB = $cFree; commitMB = $commit } }
Check 'memory limit covers the largest measured rise in one launch (12,652 MB)' ($need -gt 12652)
Check 'memory as at the 12:33:46 launch start: allowed' ($null -eq (Get-MemoryProblem (Machine 47445 26496 16719) $need))
Check 'memory: C: too full for the page file to grow: refused' ([bool](Get-MemoryProblem (Machine 47445 26496 2000) $need))
Check 'memory: page file at its 65,536 MB maximum, C: has room: refused' ([bool](Get-MemoryProblem (Machine 84000 64000 100000) $need))

# Displays (coordinator, Nov 16 16:41: idle GPU power 77.4 W asleep against 81.5 W awake; Corey plays with them awake).
# A launch starts only when the wake left them on; one that falls asleep halfway is marked. Each rule is then removed
# from a copy of the function, and its case must fail.
function DisplayCases {
    @{ 'displays on after the wake: allowed' = ($null -eq (Get-DisplayAwakeProblem ([pscustomobject]@{ display = 'on' })))
       'displays still off after the wake: refused' = [bool](Get-DisplayAwakeProblem ([pscustomobject]@{ display = 'off' }))
       'wake did not run: refused' = [bool](Get-DisplayAwakeProblem $null) }
}
foreach ($c in (DisplayCases).GetEnumerator()) { Check $c.Key $c.Value }
$ddir = Join-Path $env:TEMP "displaycheck-$PID"; New-Item -ItemType Directory -Force $ddir | Out-Null
$csv = Join-Path $ddir 'display.csv'
Set-Content $csv "time,display,idleSeconds`r`n17:00:00,on,3.0`r`n17:00:02,on,5.0"
$d = Get-DisplayDuring $csv
Check 'display log all on: awake throughout' ($d.awakeThroughout -and $d.samples -eq 2 -and $d.maxIdleSeconds -eq 5)
Add-Content $csv "17:00:04,off,1801.0`r`n17:00:06,on,0.5"
$d = Get-DisplayDuring $csv
Check 'display log asleep once: not awake throughout, first 17:00:04' (-not $d.awakeThroughout -and $d.notAwake -eq 1 -and $d.firstNotAwake -eq '17:00:04 off' -and $d.maxIdleSeconds -eq 1801)
Check 'no display log: not awake throughout' (-not (Get-DisplayDuring (Join-Path $ddir 'none.csv')).awakeThroughout)
$awakeText = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Get-DisplayAwakeProblem' }, $true)[0].Extent.Text
. ([scriptblock]::Create($awakeText.Replace("`$wake.display -eq 'on'", '$true')))
Check 'rule removed (any wake result accepted): the off case fails' (-not (DisplayCases)['displays still off after the wake: refused'])
. ([scriptblock]::Create($awakeText))
Remove-Item $ddir -Recurse -Force
# Keep-awake (coordinator, Nov 16): every launch holds the displays awake, except the proof launch whose live steps put
# them to sleep on purpose.
$proof = [pscustomobject]@{ bench = [pscustomobject]@{ liveSteps = @([pscustomobject]@{ display = 'awake' }, [pscustomobject]@{ display = 'asleep' }) } }
$rtxSteps = [pscustomobject]@{ bench = [pscustomobject]@{ liveSteps = @([pscustomobject]@{}, [pscustomobject]@{ 'rtx.qualityDLSS' = '1' }) } }
function SleepCases { @{ 'awake/asleep proof: not kept awake' = (Test-SleepsDisplays $proof)
                         'live steps without display: kept awake' = -not (Test-SleepsDisplays $rtxSteps)
                         'normal spot: kept awake' = -not (Test-SleepsDisplays ([pscustomobject]@{ name = 'con_custodial' })) } }
foreach ($c in (SleepCases).GetEnumerator()) { Check $c.Key $c.Value }
$sleepText = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Test-SleepsDisplays' }, $true)[0].Extent.Text
. ([scriptblock]::Create($sleepText.Replace("-eq 'asleep'", "-eq 'never'")))
Check 'rule removed (asleep steps not seen): the proof case fails' (-not (SleepCases)['awake/asleep proof: not kept awake'])
. ([scriptblock]::Create($sleepText))# Play script lock rules (Corey, Nov 16): a running test refuses his launch, the gate's own launch goes through, a lock
# left by a crashed test is cleared after its settings are put back, a clean state is untouched. Each rule is then
# removed from a copy of the functions, and its case must fail.
. (Join-Path $PSScriptRoot 'run_files.ps1')
$commonAst = [Management.Automation.Language.Parser]::ParseFile((Resolve-Path $Common), [ref]$null, [ref]$null)
$lockFns = foreach ($n in 'Get-TestLocks', 'Restore-LeftOverTests', 'Resolve-TestLocks') {
    $f = $commonAst.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true) | Select-Object -First 1
    if (-not $f) { throw "missing $n in $Common" }
    $f.Extent.Text
}
$live = Start-Process ping -ArgumentList '-n', '600', '127.0.0.1' -PassThru -WindowStyle Hidden
$dead = Start-Process ping -ArgumentList '-n', '1', '127.0.0.1' -PassThru -WindowStyle Hidden; $dead.WaitForExit()
$corey = "[Proxy]`r`nEnable=1`r`n"
function Owner($p, [double]$shift = 0) { "pid $($p.Id) started $($p.StartTime.ToUniversalTime().AddSeconds($shift).ToString('o'))" }
# One fake setup: an ini and a save the run changed, the run's file-backup, and the lock ($owner $null: no lock).
function LockCase([string]$owner, [int]$parent, [bool]$changed, [bool]$answer = $true) {
    $d = Join-Path $env:TEMP "playlock-$PID-$([guid]::NewGuid().ToString('N').Substring(0, 8))"
    $run = Join-Path $d 'run'; $saves = Join-Path $d 'saves'; $ini = Join-Path $d 'd3d9_proxy.ini'; $lockFile = Join-Path $d 'AGENT_GAME_LOCK.txt'
    New-Item -ItemType Directory $saves | Out-Null
    [IO.File]::WriteAllText($ini, $corey); Set-Content (Join-Path $saves 'a.save') 'corey'
    Save-RunFiles $run ([ordered]@{ $ini = $ini }) $saves
    if ($changed) { [IO.File]::WriteAllText($ini, (Add-RunMarker "[Proxy]`r`nEnable=0`r`n" $run)); Set-Content (Join-Path $saves 'a.save') 'test' }
    if ($owner) { Set-Content $lockFile "verify_runtime.ps1 runtime gate 2026-11-16 12:00:00Z run $run`r`n$owner" }
    $script:asked = 0
    # An error stops the Play script (its catch prints it and exits), so here it counts as a refusal.
    try { $r = Resolve-TestLocks @($lockFile) $parent @($ini) { param($l) $script:asked++; $answer } }
    catch { $r = @{ refuse = "error: $($_.Exception.Message)"; restored = @() } }
    $o = [pscustomobject]@{ refuse = $r.refuse; restored = @($r.restored).Count; asked = $script:asked
        coreyIni = [IO.File]::ReadAllText($ini) -eq $corey; coreySave = (Get-Content (Join-Path $saves 'a.save')) -eq 'corey'; lock = Test-Path $lockFile }
    Remove-Item $d -Recurse -Force
    $o
}
function LockCases {
    $c = LockCase (Owner $live) $PID $true
    [ordered]@{
        'live lock: refused, nothing changed' = [bool]$c.refuse -and $c.refuse -match 'runtime gate' -and $c.lock -and -not $c.coreyIni -and $c.asked -eq 0
        'gate''s own launch: allowed, its settings kept' = $(
            $c = LockCase (Owner $live) $live.Id $true; -not $c.refuse -and $c.lock -and -not $c.coreyIni -and -not $c.coreySave)
        'leftover lock: settings and saves put back, lock removed, launches' = $(
            $c = LockCase (Owner $dead) $PID $true; -not $c.refuse -and $c.coreyIni -and $c.coreySave -and -not $c.lock -and $c.restored -ge 2)
        'pid now used by another process (other start time): leftover' = $(
            $c = LockCase (Owner $live -60) $PID $true; -not $c.refuse -and $c.coreyIni -and -not $c.lock)
        'clean state: launches, nothing changed' = $(
            $c = LockCase $null $PID $false; -not $c.refuse -and $c.restored -eq 0 -and $c.coreyIni -and $c.coreySave -and $c.asked -eq 0)
        'lock without pid: asks, and No refuses' = $(
            $c = LockCase 'no pid recorded' $PID $true $false; [bool]$c.refuse -and $c.asked -eq 1 -and $c.lock -and -not $c.coreyIni)
    }
}
function Use-LockFns($texts) { foreach ($t in $texts) { . ([scriptblock]::Create($t)) } }
# Use-LockFns defines them in its own scope; dot-source the call so they land here.
. Use-LockFns $lockFns
$cases = LockCases
foreach ($k in $cases.Keys) { Check $k $cases[$k] }
# Each mutant removes one rule; the case it names must fail.
$mutants = [ordered]@{
    'live lock: refused, nothing changed' = @('if ($live.Count) {', 'if ($false) {')
    'gate''s own launch: allowed, its settings kept' = @("if (`$kind -eq 'live' -and `$id -eq `$parentPid) { `$kind = 'gate' }", '')
    'leftover lock: settings and saves put back, lock removed, launches' = @("`$kind = if (`$proc) { 'live' } else { 'leftover' }", "`$kind = 'live'")
    'pid now used by another process (other start time): leftover' = @("-gt 2) { `$kind = 'leftover' }", '-gt 1e9) { }')
    'clean state: launches, nothing changed' = @('-SimpleMatch $RunMarkerText', "-Pattern 'Proxy'")
    'lock without pid: asks, and No refuses' = @('if (-not (& $askUnknown $l))', 'if ($false)')
}
$mutantNames = @($mutants.Keys)
for ($m = 0; $m -lt $mutantNames.Count; $m++) {
    $name = $mutantNames[$m]; $from, $to = $mutants[$name]
    $text = $lockFns -join "`n"
    if (-not $text.Contains($from)) { Check "mutant ${m}: rule text found" $false; continue }
    . Use-LockFns @($text.Replace($from, $to))
    $mc = LockCases
    Check "rule removed, case fails: $name" (-not $mc[$name])
}
. Use-LockFns $lockFns
Stop-Process -Id $live.Id -Force -ErrorAction SilentlyContinue
"failures $fail"
exit [int]($fail -gt 0)

