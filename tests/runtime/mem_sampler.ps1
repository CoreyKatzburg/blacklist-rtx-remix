# Memory and C: drive once per interval for one launch, appended to -Out as read, until the process is killed
# (coordinator, Nov 16: C: free space swings by GBs during launches because the page file on C: grows with committed
# memory; worker C lines these samples up against multi-second freezes). Columns:
#   commitMB / commitLimitMB  memory all programs hold, and what RAM plus the current page file can hold
#   freeRamMB                 RAM not in use (Available MBytes)
#   pagefileMB / pagefileUsedMB  size of the page files on C: now, and how much of them holds paged-out memory
#   hardFaultsPerSec          pages read back from disk per second (Pages Input/sec, Resource Monitor's hard faults)
#   cFreeMB                   free space on C:
# Started by verify_runtime.ps1 with powershell.exe (Windows PowerShell 5.1).
param([Parameter(Mandatory)][string]$Out, [int]$IntervalSeconds = 2)
$counters = '\Memory\Committed Bytes', '\Memory\Commit Limit', '\Memory\Available MBytes', '\Memory\Pages Input/sec'
if (-not (Test-Path -LiteralPath $Out)) {
    [IO.File]::WriteAllText($Out, "time,commitMB,commitLimitMB,freeRamMB,pagefileMB,pagefileUsedMB,hardFaultsPerSec,cFreeMB`r`n")
}
Get-Counter -Counter $counters -SampleInterval $IntervalSeconds -Continuous | ForEach-Object {
    $v = @{}
    foreach ($s in $_.CounterSamples) { $v[($s.Path -split '\\')[-1]] = $s.CookedValue }
    $pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | Where-Object Name -like 'c:*')
    $line = '{0},{1},{2},{3},{4},{5},{6},{7}' -f $_.Timestamp.ToString('HH:mm:ss.fff'),
        [int]($v['committed bytes'] / 1MB), [int]($v['commit limit'] / 1MB), [int]$v['available mbytes'],
        [int](($pf | Measure-Object AllocatedBaseSize -Sum).Sum), [int](($pf | Measure-Object CurrentUsage -Sum).Sum),
        [math]::Round($v['pages input/sec'], 1), [int]([IO.DriveInfo]::new('C').AvailableFreeSpace / 1MB)
    [IO.File]::AppendAllText($Out, "$line`r`n")
}
