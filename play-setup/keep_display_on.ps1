Add-Type -Namespace K -Name E -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint f);'
for ($i = 0; $i -lt 720; ++$i) { [K.E]::SetThreadExecutionState([uint32]2147483650) | Out-Null; Start-Sleep 5 }
