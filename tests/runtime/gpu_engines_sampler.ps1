# GPU use per program once per interval for one launch, appended to -Out as read, until the process is killed
# (coordinator, Nov 17: the plug-on against plug-off frame time is GPU time inside Remix's ray-tracing passes, and the
# one unrecorded thing that could carry it is GPU time other programs use, such as dwm.exe or the NVIDIA overlay).
# Reads the Windows counter "\GPU Engine(*)\Utilization Percentage" (what Task Manager shows; reading it changes no
# setting), sums it per process id and engine type, and writes one row per (pid, engine type) above 0.005 %, plus one
# "_sample" row per sample: pid -1, engtype "all", percent = the sum over every instance, instances = how many counter
# instances the sample had. A sample with 0 instances, or no _sample row at all, means NOT RECORDED, not 0 % (read by
# runtime\gpu_engines.py). Columns: time,pid,process,engtype,percent,instances
# Per sample it also writes, for each watched program that is running (game, bridge, dwm), a row with engtype
# "_running" and percent 1 when the sample had counter instances for its pid, 0 when not; gpu_engines.py reads a
# running program without instances as NOT RECORDED, never 0 %.
# Get-Counter -Continuous expands the (*) instance list once when it starts, so a program started later is never
# counted (Nov 17, 20261117-114305: the game and bridge had no rows at all). Expanding it every sample costs about one
# CPU core (measured Nov 17 11:54: 26 s CPU over 25 s), so instead the continuous read restarts when the set of running
# watched programs changes, and again up to 3 times 10 s apart while a running watched program has no instances (a
# program's GPU instances appear only once it creates its device). Each restart costs about 2 s CPU.
# Started by verify_runtime.ps1 with powershell.exe (Windows PowerShell 5.1).
param([Parameter(Mandatory)][string]$Out, [int]$IntervalSeconds = 1,
      [string[]]$Watch = @('Blacklist_game', 'Blacklist_DX11_game', 'NvRemixBridge', 'dwm'))
$counter = '\GPU Engine(*)\Utilization Percentage'
if (-not (Test-Path -LiteralPath $Out)) { [IO.File]::WriteAllText($Out, "time,pid,process,engtype,percent,instances`r`n") }
$names = @{}
function Get-Name([int]$id) {
    if (-not $names.ContainsKey($id)) {
        $p = Get-Process -Id $id -ErrorAction SilentlyContinue
        $names[$id] = if ($p) { $p.ProcessName } else { '?' }
    }
    $names[$id]
}
function Get-Watched { @(Get-Process -Name $watch -ErrorAction SilentlyContinue | Sort-Object Id) }
$retries = 3
while ($true) {
    $startSet = (Get-Watched | ForEach-Object Id) -join ','
    $started = Get-Date
    try {
        Get-Counter -Counter $counter -SampleInterval $IntervalSeconds -Continuous -ErrorAction Stop | ForEach-Object {
            $time = $_.Timestamp.ToString('HH:mm:ss.fff')
            $sum = @{}; $total = 0.0; $n = 0
            foreach ($s in $_.CounterSamples) {
                $n++
                if ($s.InstanceName -match '^pid_(\d+)_.*_engtype_(.+)$') {
                    $k = "$($Matches[1])|$($Matches[2])"
                    $sum[$k] = [double]$sum[$k] + $s.CookedValue
                    $total += $s.CookedValue
                }
            }
            $sb = [Text.StringBuilder]::new()
            foreach ($k in $sum.Keys) {
                if ($sum[$k] -lt 0.005) { continue }
                $id, $eng = $k -split '\|', 2
                [void]$sb.AppendFormat([Globalization.CultureInfo]::InvariantCulture, "{0},{1},{2},{3},{4:0.###},{5}`r`n",
                    $time, $id, (Get-Name ([int]$id)), $eng, $sum[$k], '')
            }
            $seen = @{}; foreach ($k in $sum.Keys) { $seen[($k -split '\|', 2)[0]] = $true }
            $now = Get-Watched
            $unseen = 0
            foreach ($p in $now) {
                $got = [int]$seen.ContainsKey([string]$p.Id)
                if (-not $got) { $unseen++ }
                [void]$sb.AppendFormat("{0},{1},{2},_running,{3},`r`n", $time, $p.Id, $p.ProcessName, $got)
            }
            [void]$sb.AppendFormat([Globalization.CultureInfo]::InvariantCulture, "{0},-1,_sample,all,{1:0.###},{2}`r`n", $time, $total, $n)
            [IO.File]::AppendAllText($Out, $sb.ToString())
            $nowSet = ($now | ForEach-Object Id) -join ','
            if ($nowSet -ne $startSet) { $retries = 3; throw 'restart' }
            if ($unseen -and $retries -gt 0 -and ((Get-Date) - $started).TotalSeconds -ge 10) { $retries--; throw 'restart' }
        }
    } catch {
        if ("$_" -ne 'restart') {
            # The counter set can fail while a process with GPU instances exits mid-sample; note it and read again.
            [IO.File]::AppendAllText($Out, "$((Get-Date).ToString('HH:mm:ss.fff')),-1,_error,all,,0`r`n")
            Start-Sleep -Seconds $IntervalSeconds
        }
    }
}
