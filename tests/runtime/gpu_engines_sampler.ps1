# GPU use per program once per interval for one launch, appended to -Out as read, until the process is killed
# (coordinator, Nov 17: the plug-on against plug-off frame time is GPU time inside Remix's ray-tracing passes, and the
# one unrecorded thing that could carry it is GPU time other programs use, such as dwm.exe or the NVIDIA overlay).
# Reads the Windows counter "\GPU Engine(*)\Utilization Percentage" (what Task Manager shows; reading it changes no
# setting), sums it per process id and engine type, and writes one row per (pid, engine type) above 0.005 %, plus one
# "_sample" row per sample: pid -1, engtype "all", percent = the sum over every instance, instances = how many counter
# instances the sample had. A sample with 0 instances, or no _sample row at all, means NOT RECORDED, not 0 % (read by
# runtime\gpu_engines.py). Columns: time,pid,process,engtype,percent,instances
# Started by verify_runtime.ps1 with powershell.exe (Windows PowerShell 5.1).
param([Parameter(Mandatory)][string]$Out, [int]$IntervalSeconds = 1)
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
while ($true) {
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
            [void]$sb.AppendFormat([Globalization.CultureInfo]::InvariantCulture, "{0},-1,_sample,all,{1:0.###},{2}`r`n", $time, $total, $n)
            [IO.File]::AppendAllText($Out, $sb.ToString())
        }
    } catch {
        # The counter set can fail while a process with GPU instances exits mid-sample; note it and read again.
        [IO.File]::AppendAllText($Out, "$((Get-Date).ToString('HH:mm:ss.fff')),-1,_error,all,,0`r`n")
        Start-Sleep -Seconds $IntervalSeconds
    }
}
