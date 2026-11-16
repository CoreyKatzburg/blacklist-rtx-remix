# One nvidia-smi line per interval, appended to -Out as it is read, until the process is killed.
# nvidia-smi -lms writes through a 4 KB buffer, to -f and to a redirected stdout alike, so killing it at the end of
# a launch lost the last ~20 s of samples (Nov 16). Here each sample is its own nvidia-smi call, and a kill loses at
# most the call in flight.
param([Parameter(Mandatory)][string]$Out, [Parameter(Mandatory)][string]$Query, [int]$IntervalMs = 1000)
$format = 'csv'
while ($true) {
    $t = [Diagnostics.Stopwatch]::StartNew()
    $lines = & nvidia-smi "--query-gpu=$Query" "--format=$format" 2>&1
    [IO.File]::AppendAllText($Out, (($lines | ForEach-Object { "$_" }) -join "`r`n") + "`r`n")
    $format = 'csv,noheader'
    $left = $IntervalMs - [int]$t.ElapsedMilliseconds
    if ($left -gt 0) { Start-Sleep -Milliseconds $left }
}
