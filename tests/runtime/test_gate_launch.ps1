# Fake cases for verify_runtime.ps1's launch recovery (Nov 16 08:49 run 084921): a game exe the launcher restarts is
# followed, and a lock left by a killed Play script is removed only when this run's Play script wrote it.
# Run: pwsh -File test_gate_launch.ps1 ; exit code 1 on any failure.
param([string]$Gate = (Join-Path (Split-Path $PSScriptRoot) 'verify_runtime.ps1'))
$ErrorActionPreference = 'Stop'
$ast = [Management.Automation.Language.Parser]::ParseFile($Gate, [ref]$null, [ref]$null)
foreach ($n in 'Remove-OwnPlayLock', 'Wait-RestartedGame', 'Get-DisplayProblem') {
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
"failures $fail"
exit [int]($fail -gt 0)

