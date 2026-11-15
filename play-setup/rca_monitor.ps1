# Logs system load every 5 s while the game runs, so slowdowns can be matched to RAM, VRAM or CPU pressure.
# usage (detached, before or right after launch):  & "$P\rca_monitor.ps1" -Out $OUT
# Writes $Out\monitor.csv; stops by itself 30 s after Blacklist_game / Blacklist_DX11_game has exited.
param([Parameter(Mandatory)][string]$Out)
$csv = Join-Path $Out 'monitor.csv'
'time,gpu_util_pct,vram_used_mb,vram_total_mb,ram_free_mb,game_private_mb,game_ws_mb,game_cpu_pct,bridge_private_mb,bridge_ws_mb,bridge_cpu_pct,other_top_cpu' |
    Set-Content $csv
$cores = [Environment]::ProcessorCount
$last = @{}
$gone = 0
while ($gone -lt 6) {
  try {
    $now = Get-Date
    $gpu = (& nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>$null) -split ',\s*'
    $free = [int]((Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1024)
    $procs = Get-Process | Where-Object { $_.Id -ne 0 }
    $cpu = @{}
    foreach ($p in $procs) {
        $t = $p.TotalProcessorTime.TotalSeconds
        if ($last.ContainsKey($p.Id)) { $cpu[$p.Id] = [math]::Round(100 * ($t - $last[$p.Id]) / 5 / $cores, 1) }
        $last[$p.Id] = $t
    }
    $game = $procs | Where-Object { $_.ProcessName -match '^Blacklist(_DX11)?_game$' } | Select-Object -First 1
    $bridge = $procs | Where-Object { $_.ProcessName -eq 'NvRemixBridge' } | Select-Object -First 1
    if ($game) { $gone = 0 } else { $gone++ }
    $mb = { param($p, $f) if ($p) { [int]($p.$f / 1MB) } else { '' } }
    $pc = { param($p) if ($p -and $cpu.ContainsKey($p.Id)) { $cpu[$p.Id] } else { '' } }
    $top = $procs | Where-Object { $cpu.ContainsKey($_.Id) -and $_.Id -ne $game.Id -and $_.Id -ne $bridge.Id } |
        Sort-Object { $cpu[$_.Id] } -Descending | Select-Object -First 3 | ForEach-Object { "$($_.ProcessName):$($cpu[$_.Id])" }
    ('{0:HH:mm:ss},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11}' -f $now, $gpu[0], $gpu[1], $gpu[2], $free,
        (& $mb $game 'PrivateMemorySize64'), (& $mb $game 'WorkingSet64'), (& $pc $game),
        (& $mb $bridge 'PrivateMemorySize64'), (& $mb $bridge 'WorkingSet64'), (& $pc $bridge), ($top -join ' ')) |
        Add-Content $csv
  } catch {
    # Keep logging: run 7's monitor died on one bad sample. Record the error and carry on.
    "{0:HH:mm:ss} {1}" -f (Get-Date), $_ | Add-Content (Join-Path $Out 'monitor_errors.txt')
  }
    Start-Sleep -Seconds 5
}
