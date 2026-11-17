# Fake cases for verify_runtime.ps1's launch recovery (Nov 16 08:49 run 084921): a game exe the launcher restarts is
# followed, and a lock left by a killed Play script is removed only when this run's Play script wrote it.
# Run: pwsh -File test_gate_launch.ps1 ; exit code 1 on any failure.
param([string]$Gate = (Join-Path (Split-Path $PSScriptRoot) 'verify_runtime.ps1'),
      [string]$Common = (Join-Path $PSScriptRoot '..\..\play-setup\RemixPlayCommon.ps1'))
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Gate, [ref]$null, [ref]$null)
foreach ($n in 'Remove-OwnPlayLock', 'Wait-RestartedGame', 'Get-DisplayProblem', 'Test-LaunchDead', 'Get-CSpaceProblem', 'Get-MemoryProblem',
           'Get-DisplayAwakeProblem', 'Get-DisplayDuring', 'Test-SleepsDisplays', 'Invoke-UbiLogin', 'Get-SuiteRecord', 'Get-MonitorStateProblem', 'Test-MonitorsOff',
           'Get-RunSpots', 'Get-RunIniKeys', 'Select-Previous', 'Get-InstalledRemixCommit') {
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
# Sign-in with Ubisoft Connect gone (run 190131, Nov 16 19:07): ubi_login.ps1 throws 'Cannot find a process with the
# name "upc"'. The launch must carry on to the dead-start check and only report the failure.
$fakeLogin = Join-Path $env:TEMP "ubilogin-$PID.ps1"
Set-Content $fakeLogin '$ErrorActionPreference = ''Stop''; Get-Process upc-not-running-fake -ErrorAction Stop | Out-Null'
$loginMsg = try { Invoke-UbiLogin $fakeLogin } catch { 'THROWN' }
Check 'sign-in script throws (no upc): reported, launch carries on' ($loginMsg -and $loginMsg -ne 'THROWN' -and $loginMsg -match 'Cannot find a process')
Set-Content $fakeLogin '"signed in"'
Check 'sign-in works: nothing reported, its output dropped' ($null -eq (Invoke-UbiLogin $fakeLogin))
Remove-Item $fakeLogin
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
# The display watcher starts after the pre-launch wake (run 20261116-174116 started it before, and its first sample,
# 1235.6 s without input from before the launch, became the launch's "longest without input").
function WatchAfterWake([string]$text) {
    $wake = $text.IndexOf('Add-MachineState $runDir $run.dir $spotDir'); $watch = $text.IndexOf("'watch', '--out'")
    $wake -ge 0 -and $watch -gt $wake
}
$gateText = Get-Content -Raw $Gate
Check 'display watcher starts after the pre-launch wake' (WatchAfterWake $gateText)
$i = $gateText.IndexOf('Add-MachineState $runDir $run.dir $spotDir')
Check 'rule broken (watcher moved before the wake): the order check fails' `
    (-not (WatchAfterWake ($gateText.Substring(0, $i) + "'watch', '--out' " + $gateText.Substring($i).Replace("'watch', '--out'", "'w'"))))
# The manifest's suite record (coordinator, Nov 16 23:19: the note must come from the recorded pass, not fixed text).
$suiteFile = Join-Path $env:TEMP "suiterecord-$PID.json"
function Rec([hashtable]$change = @{}) {
    $n = [ordered]@{ finished = '2026-11-16 22:57:00'; passed = $true; checks = 35; failed = @(); toolsCommit = '13ef9b5'
                     toolsModified = @(); remixSourceCommit = '1d1cb86'; remixInstalledCommit = 'ee695ff'; proxySha256 = 'FC8E'
                     runtimeSha256 = 'B72B' }
    foreach ($k in $change.Keys) { $n[$k] = $change[$k] }; $n
}
$passRec = Rec
function Write-SuiteFile($lastRun, $lastPass) { [ordered]@{ lastRun = $lastRun; lastPass = $lastPass } | ConvertTo-Json -Depth 4 | Set-Content $suiteFile }
function Now([hashtable]$change = @{}) {
    $n = [ordered]@{ toolsCommit = '13ef9b5'; remixSourceCommit = '1d1cb86'; remixInstalledCommit = 'ee695ff'; proxySha256 = 'FC8E'
                     runtimeSha256 = 'B72B'; toolsModified = @() }
    foreach ($k in $change.Keys) { $n[$k] = $change[$k] }; $n
}
Write-SuiteFile $passRec $passRec
$r = Get-SuiteRecord $suiteFile (Now)
Check 'suite record: same tree reads yes with the recorded time and commit' ($r.sameTree -and $r.line -eq 'full suite last passed at 2026-11-16 22:57:00 on 13ef9b5; same tree now: yes')
foreach ($c in @(@{ toolsCommit = '0f76bd9' }, @{ remixSourceCommit = 'ee695ff' }, @{ remixInstalledCommit = '1d1cb86' },
                 @{ proxySha256 = 'AAAA' }, @{ runtimeSha256 = 'CCCC' },
                 @{ toolsModified = @(' M tests/runtime/gate.py') })) {
    $r = Get-SuiteRecord $suiteFile (Now $c)
    $k = @($c.Keys)[0]
    Check "suite record: $k differs, same tree no" ((-not $r.sameTree) -and $r.line -like '*same tree now: no (*' -and ($r.differences -join ' ') -match $(if ($k -eq 'toolsModified') { 'modified now: M tests/runtime/gate.py' } else { $k }))
}
$dirty = Rec @{ toolsModified = @(' M tests/runtime/gate.py') }
Write-SuiteFile $dirty $dirty
Check 'suite record: a pass on a tree with a modified file is not the same tree' (-not (Get-SuiteRecord $suiteFile (Now)).sameTree)
$failRun = Rec @{ passed = $false; finished = '2026-11-16 23:30:00' }
Write-SuiteFile $failRun $passRec
$r = Get-SuiteRecord $suiteFile (Now)
Check 'suite record: a failed run after the pass is not the same tree' ((-not $r.sameTree) -and $r.line -like '*the last run, at 2026-11-16 23:30:00, failed*')
Write-SuiteFile $failRun $null
$r = Get-SuiteRecord $suiteFile (Now)
Check 'suite record: never passed reads no recorded pass' ((-not $r.sameTree) -and $r.line -eq 'full suite: no recorded pass')
Remove-Item $suiteFile
$r = Get-SuiteRecord $suiteFile (Now)
Check 'suite record: no file reads no recorded pass' ((-not $r.sameTree) -and $r.line -eq 'full suite: no recorded pass')
# A record from before the split (one "remixCommit", Nov 17 00:20) cannot read as the same tree: both new keys are
# missing, and the line says "not recorded" rather than an empty commit.
$oldRec = Rec; $oldRec.Remove('remixSourceCommit'); $oldRec.Remove('remixInstalledCommit'); $oldRec['remixCommit'] = '1d1cb86'
Write-SuiteFile $oldRec $oldRec
$r = Get-SuiteRecord $suiteFile (Now)
Check 'suite record: an old record with only remixCommit is not the same tree' ((-not $r.sameTree) -and
    $r.line -like '*remixSourceCommit not recorded then, 1d1cb86 now*' -and $r.line -like '*remixInstalledCommit not recorded then, ee695ff now*')
# The installed build (coordinator, Nov 17: the source HEAD 1d1cb86 was read as the installed build ee695ff): the commit
# STAGED_REMIX.json records, only while pkg's runtime has the recorded hash.
$stDir = Join-Path $env:TEMP "staged-$PID"; New-Item -ItemType Directory -Force $stDir | Out-Null
$stJson = Join-Path $stDir 'STAGED_REMIX.json'; $stDll = Join-Path $stDir 'd3d9.dll'
[IO.File]::WriteAllText($stDll, 'runtime bytes')
$dllHash = (Get-FileHash -LiteralPath $stDll -Algorithm SHA256).Hash
@{ commit = 'ee695ffff4a02007ac983dd35ed4859c17ffefb3'; runtimeSha256 = $dllHash } | ConvertTo-Json | Set-Content $stJson
Check 'installed commit: the staged record when pkg has its runtime' ((Get-InstalledRemixCommit $stJson $stDll) -eq 'ee695ff')
[IO.File]::WriteAllText($stDll, 'a newer build copied into pkg')
Check 'installed commit: unknown when pkg holds another runtime' ((Get-InstalledRemixCommit $stJson $stDll) -like 'unknown: *(ee695ff)')
Check 'installed commit: unknown without STAGED_REMIX.json' ((Get-InstalledRemixCommit (Join-Path $stDir 'none.json') $stDll) -like 'unknown: no *')
Remove-Item -Recurse -Force $stDir
$suiteText = Get-Content -Raw (Join-Path (Split-Path $Gate) 'run_all_offline.ps1')
Check 'the suite records the source and installed dxvk-remix commits, with the gate''s own installed rule' (
    $suiteText.Contains('remixSourceCommit = ') -and $suiteText.Contains('remixInstalledCommit = (Get-InstalledRemixCommit ') -and
    $suiteText.Contains("`$a.Name -eq 'Get-InstalledRemixCommit'") -and -not $suiteText.Contains('remixCommit = ') -and
    $gateText.Contains('remixInstalledCommit = $remixCommit; remixSourceCommit = $remixSourceCommit'))
Check 'the suite writes the record the gate reads' ($suiteText.Contains("'experiments\remix-gate\offline_suite_last.json'") -and
    $gateText.Contains("`$ResultsRoot = 'D:\BlacklistOverhaul\experiments\remix-gate'") -and $gateText.Contains("Join-Path `$ResultsRoot 'offline_suite_last.json'"))

# Monitors-off launches (coordinator, Nov 16 23:50): bench monitorsOff accepts any display state with no monitor
# present and refuses one present; without it the displays must read on, as before.
$offWake = [pscustomobject]@{ display = 'off' }; $onWake = [pscustomobject]@{ display = 'on' }
$none = @{ monitorsOn = 0; monitors = @(); displayWake = $offWake }
$one = @{ monitorsOn = 1; monitors = @('DISPLAY\HKC2731\x'); displayWake = $onWake }
$both = @{ monitorsOn = 2; monitors = @('DISPLAY\HKC2731\x', 'DISPLAY\ACI27A8\y'); displayWake = $onWake }
Check 'monitors off asked, none present, display off: launches' ($null -eq (Get-MonitorStateProblem $none $true))
Check 'monitors off asked, one present: refused' ((Get-MonitorStateProblem $one $true) -like '*1 monitor(s) are present*')
Check 'monitors off asked, both present and awake: refused' ((Get-MonitorStateProblem $both $true) -like '*2 monitor(s) are present*')
Check 'normal launch, none present, display off: refused as before' ((Get-MonitorStateProblem $none $false) -like '*did not wake*')
Check 'normal launch, both present and awake: launches' ($null -eq (Get-MonitorStateProblem $both $false))
Check 'bench monitorsOff true reads as asked' (Test-MonitorsOff ([pscustomobject]@{ bench = [pscustomobject]@{ monitorsOff = $true } }))
Check 'no monitorsOff key reads as not asked' (-not (Test-MonitorsOff ([pscustomobject]@{ bench = [pscustomobject]@{ liveSteps = @() } })))
Check 'monitorsOff false reads as not asked' (-not (Test-MonitorsOff ([pscustomobject]@{ bench = [pscustomobject]@{ monitorsOff = $false } })))
Check 'Add-MachineState passes the spot''s monitorsOff on' ($gateText.Contains('Add-MachineState $runDir $run.dir $spotDir (Test-MonitorsOff $spot)'))

# Display state (coordinator, Nov 17 00:30: plug off ran about 12 % faster per frame than plug on): gate.py's FPS check
# gets a previous run only in the same display state class; a given -Previous in another state reads NOT COMPARABLE.
$Py = 'D:\BlacklistOverhaul\tools\upscalers\py314\Scripts\python.exe'; $Runtime = $PSScriptRoot
$ResultsRoot = Join-Path ([IO.Path]::GetTempPath()) "gate-display-$PID"
$monOn = '{"display":"on","monitorsOn":2,"monitors":["DISPLAY\\ACI27A8\\a","DISPLAY\\HKC2731\\b"]}'
$monOff = '{"display":"on","monitorsOn":0,"monitors":[],"displays":{"name":"ASUS MG278","primary":true,"refreshHz":60.0}}'
function FakeRun($name, $machine) {
    $d = Join-Path $ResultsRoot $name
    New-Item -ItemType Directory -Force (Join-Path $d 'con_custodial') | Out-Null
    '{"spots":["con_custodial"]}' | Set-Content (Join-Path $d 'manifest.json')
    '{}' | Set-Content (Join-Path $d 'result.json')
    if ($machine) { $machine | Set-Content (Join-Path $d 'con_custodial\machine_before.json') }
    $d
}
$onOld = FakeRun '20990101-000001' $monOn
$offNew = FakeRun '20990101-000002' $monOff
$onNow = FakeRun '20990101-000003' $monOn
$offNow = FakeRun '20990101-000004' $monOff
$oldRun = FakeRun '20990101-000005' $null
$p = Select-Previous $onNow ''
Check 'plug-on run: the newest earlier plug-on run, not the newer plug-off one' ($p.prev -eq $onOld -and -not $p.note)
$p = Select-Previous $offNow ''
Check 'plug-off run: the plug-off run' ($p.prev -eq $offNew -and -not $p.note)
$p = Select-Previous $onNow $offNew
Check 'plug-on run with -Previous a plug-off run: refused, NOT COMPARABLE' (-not $p.prev -and $p.note -like 'fps_regression NOT COMPARABLE: display state plug on*plug off*')
$p = Select-Previous $onNow $onOld
Check 'plug-on run with -Previous a plug-on run: compared' ($p.prev -eq $onOld)
$p = Select-Previous $oldRun ''
Check 'run without a display record: no previous run, NOT COMPARABLE' (-not $p.prev -and $p.note -like '*NOT COMPARABLE: display state unknown*')
# -IniKeys (coordinator, Nov 17: the FlareStandIns=0 run at four spots): a run made with launch-wide ini keys is the
# previous run only of a run with the same keys, so a normal run never takes its FPS from it, nor it from a normal one.
foreach ($n in 'ConvertFrom-IniKeyArg', 'Get-IniKey', 'Set-IniKey', 'Set-IniKeys') {
    $f = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true) | Select-Object -First 1
    if (-not $f) { throw "missing $n" }
    . ([scriptblock]::Create($f.Extent.Text))
}
function IniRun($name, $keys) {
    $d = FakeRun $name $monOn
    @{ spots = @('con_custodial'); iniKeys = @($keys) } | ConvertTo-Json | Set-Content (Join-Path $d 'manifest.json')
    $d
}
$flareOld = IniRun '20990101-000010' 'Lights.FlareStandIns=0'
$plainNow = FakeRun '20990101-000011' $monOn
$flareNow = IniRun '20990101-000012' 'Lights.FlareStandIns=0'
$p = Select-Previous $plainNow ''
Check 'ini keys: a normal run skips the newer FlareStandIns=0 run and takes the plug-on normal run' ($p.prev -eq $onNow)
$p = Select-Previous $flareNow ''
Check 'ini keys: a FlareStandIns=0 run takes only the earlier FlareStandIns=0 run' ($p.prev -eq $flareOld)
$f = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Select-Previous' }, $true) | Select-Object -First 1
. ([scriptblock]::Create($f.Extent.Text.Replace('(Get-RunIniKeys $_.FullName) -eq (Get-RunIniKeys $folder)', '$true')))
$p = Select-Previous $plainNow ''
Check 'ini keys: rule removed, case fails: the normal run takes the FlareStandIns=0 run' ($p.prev -eq $flareOld)
. ([scriptblock]::Create($f.Extent.Text))
$setup = "[Proxy]`r`nEnableDebuggingTelemetry=1`r`nFlareStandIns=1`r`n; [Lights] is set by drive.py`r`n"
$t = Set-IniKey $setup 'Lights' 'FlareStandIns' '0'
Check 'ini keys: FlareStandIns=0 lands in a new [Lights] section; [Proxy] keeps its own lines' (
    (Get-IniKey $t 'Lights' 'FlareStandIns') -eq '0' -and (Get-IniKey $t 'Proxy' 'FlareStandIns') -eq '1' -and
    (Get-IniKey $t 'Proxy' 'EnableDebuggingTelemetry') -eq '1' -and $t.StartsWith($setup.TrimEnd()))
Check 'ini keys: a key under another section is not read as [Lights] (the read-back can fail)' ($null -eq (Get-IniKey $setup 'Lights' 'FlareStandIns'))
$t2 = Set-IniKey "[Lights]`r`nEnable=1`r`nFlareStandIns=1`r`n[Proxy]`r`nFlareStandIns=1`r`n" 'Lights' 'FlareStandIns' '0'
Check 'ini keys: an existing [Lights] key is replaced in place, the next section untouched' (
    $t2 -eq "[Lights]`r`nEnable=1`r`nFlareStandIns=0`r`n[Proxy]`r`nFlareStandIns=1`r`n")
$items = ConvertFrom-IniKeyArg @('Lights.FlareStandIns=0', 'RemixConfig.rtx.volumetrics.enable=False')
Check 'ini keys: arguments split into section, key, value' ((($items | ForEach-Object { "$($_.Section)|$($_.Key)|$($_.Value)" }) -join ';') -eq
    'Lights|FlareStandIns|0;RemixConfig|rtx.volumetrics.enable|False')
# Launch 20261117-183718: one key alone (the launch's own case) was unrolled into its three strings and written as
# "[L] i=g", "[F] l=a", "[0] =" with no FlareStandIns. Set-IniKeys is the launch's path; the old code must fail here.
$one = Set-IniKeys $setup @('Lights.FlareStandIns=0')
Check 'ini keys: one key alone sets [Lights] FlareStandIns=0 and adds no other section' (
    (Get-IniKey $one 'Lights' 'FlareStandIns') -eq '0' -and
    @([regex]::Matches($one, '(?m)^\[([^\]]*)\]') | ForEach-Object { $_.Groups[1].Value }) -join ',' -eq 'Proxy,Lights')
function OldIniKeyArg([string[]]$items) {   # verify_runtime before this fix, word for word
    @(foreach ($s in $items) {
        if ($s -notmatch '^([A-Za-z]+)\.([^=\s]+)=([^\r\n]*)$') { throw "-IniKeys '$s' is not Section.Key=Value" }
        , @($Matches[1], $Matches[2], $Matches[3])
    })
}
$old = $setup; foreach ($i in (OldIniKeyArg @('Lights.FlareStandIns=0'))) { $old = Set-IniKey $old $i[0] $i[1] $i[2] }
Check 'ini keys: old code, case fails: one key alone writes [L] i=g and no FlareStandIns' (
    $null -eq (Get-IniKey $old 'Lights' 'FlareStandIns') -and (Get-IniKey $old 'L' 'i') -eq 'g')
$badArg = $false
try { $null = ConvertFrom-IniKeyArg @('FlareStandIns=0') } catch { $badArg = $_.Exception.Message -like "*is not Section.Key=Value" }
Check 'ini keys: an argument without a section is refused' $badArg
. ([scriptblock]::Create($f.Extent.Text.Replace('if ($LASTEXITCODE -eq 0)', 'if ($true)')))
$p = Select-Previous $onNow $offNew
Check 'rule removed (display state not checked), case fails: the plug-off run is compared' ($p.prev -eq $offNew)
. ([scriptblock]::Create($f.Extent.Text))
# Add-MachineState with the recorded state of launch 20261117-002048 (MG278 alone at 60 Hz) writes plug off into the
# launch's machine_before.json and the run manifest.
$f = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq 'Add-MachineState' }, $true) | Select-Object -First 1
. ([scriptblock]::Create($f.Extent.Text))
function Write-Step($t) { }
function Get-MachineState { [ordered]@{ time = '00:21:10'; display = 'on'; displayWake = $onWake; monitorsOn = 0; monitors = @()
    displays = @([pscustomobject]@{ name = 'ASUS MG278'; primary = $true; width = 2560; height = 1440; x = 0; y = 0; refreshHz = 60.0 }); idleGpuPowerW = 76.31 } }
$mrun = FakeRun '20990101-000006' $null
Add-MachineState $mrun 'l1' (Join-Path $mrun 'con_custodial') $true
$man = Get-Content (Join-Path $mrun 'manifest.json') -Raw | ConvertFrom-Json
$mbj = Get-Content (Join-Path $mrun 'con_custodial\machine_before.json') -Raw | ConvertFrom-Json
Check 'Add-MachineState: plug off in machine_before.json and the manifest' ($mbj.displayState -eq 'plug off' -and $man.displayState.class -eq 'plug off' -and $man.machineBefore.l1.displayState -eq 'plug off')
Remove-Item $ResultsRoot -Recurse -Force
Check 'Add-MachineState records the display state class' ($gateText.Contains("display_state.py') launch `$mb") -and $gateText.Contains("Add-Member displayState"))

# Spot configs written whole (20261117-165257-settings: -Depth 5 wrote worker B's ini triples as "System.Object[]").
foreach ($n in 'Test-JsonCut', 'ConvertTo-SpotJson') {
    $f = $ast.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] -and $a.Name -eq $n }, $true) | Select-Object -First 1
    if (-not $f) { throw "missing $n" }
    . ([scriptblock]::Create($f.Extent.Text))
}
$seeThrough = Get-Content (Join-Path $PSScriptRoot 'variants_con_bright_hall_seethrough.json') -Raw | ConvertFrom-Json
$benchSpot = [pscustomobject]@{ name = 'con_bright_hall'; bench = $seeThrough.bench }   # as verify_runtime builds a variant run
Check 'spot json: the old -Depth 5 write of the see-through spot is caught' ([bool](Test-JsonCut ($benchSpot | ConvertTo-Json -Depth 5 -WarningAction SilentlyContinue)))
Check 'spot json: the see-through spot passes whole' (-not (Test-JsonCut (ConvertTo-SpotJson $benchSpot)))
$cutThrew = $false
try { $null = ConvertTo-SpotJson @{ ini = 'System.Object[]' } } catch { $cutThrew = $_.Exception.Message -like 'spot config cut short*' }
Check 'spot json: a value that became text is refused' $cutThrew
$back = ConvertTo-SpotJson $seeThrough.bench | ConvertFrom-Json
Check 'spot json: the see-through ini triple comes back whole' ((@($back.switchPoses[0].switches[0].ini[0]) -join '|') -eq 'Lights|FlareStandIns|1')
$whole = @(Get-ChildItem $PSScriptRoot -Filter 'variants_*.json' | ForEach-Object { Get-Content $_.FullName -Raw | ConvertFrom-Json }) +
         @((Get-Content (Join-Path $PSScriptRoot 'spots.json') -Raw | ConvertFrom-Json).spots)
$bad = @($whole | Where-Object { $j = ConvertTo-SpotJson $_; ($j | ConvertFrom-Json | ConvertTo-Json -Depth 100 -Compress) -ne ($_ | ConvertTo-Json -Depth 100 -Compress) })
Check "spot json: every variants file and spots.json spot ($($whole.Count)) round-trips unchanged" ($bad.Count -eq 0)

"failures $fail"
exit [int]($fail -gt 0)

