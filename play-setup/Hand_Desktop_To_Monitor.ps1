# Hands this Windows session from Remote Desktop to the PC's own monitor (the physical console),
# so the desktop keeps running at the monitor's resolution instead of being disconnected.
# Run it from inside the Remote Desktop session; it needs administrator rights (one UAC prompt).
# The Remote Desktop connection drops right after; the desktop stays signed in on the monitor.
$session = (Get-Process -Id $PID).SessionId
Write-Host "Handing session $session to the console..."
& "$env:SystemRoot\System32\tscon.exe" $session /dest:console
if ($LASTEXITCODE) {
    Write-Host "tscon failed (exit code $LASTEXITCODE). Was the UAC prompt approved?" -ForegroundColor Yellow
    Read-Host 'Press Enter to close' | Out-Null
}
