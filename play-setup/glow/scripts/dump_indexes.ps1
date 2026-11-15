# Dump the reflection index (version 7) of every .ass archive through the reader's LeadArchive + Fields.Parse.
param(
    [string]$ReaderPath = 'D:\BlacklistOverhaul\tools\tools\BlacklistMeshExplorer\BlacklistMeshExplorer-v0.41.exe',
    [string]$UmdDir = "D:\Steam\steamapps\common\Tom Clancy's Splinter Cell Blacklist\src\SYSTEM\UMDs",
    [Parameter(Mandatory)] [string]$OutJson
)
$ErrorActionPreference = 'Stop'
$asm = [Reflection.Assembly]::LoadFrom($ReaderPath)
$flags = [Reflection.BindingFlags]'Public,NonPublic,Static,Instance'
$la = $asm.GetType('BlacklistExplorer.LeadArchive')
$fieldsType = $asm.GetType('BlacklistExplorer.Fields')
$parse = $fieldsType.GetMethod('Parse', $flags)
$hash = $asm.GetType('BlacklistExplorer.Binary').GetMethod('Hash', $flags)
function Get-NameHash([string]$s) { $hash.Invoke($null, [object[]]@($s)) }

function Convert-Value($v) {
    if ($null -eq $v) { return $null }
    if ($v -is [byte[]]) { return [Convert]::ToHexString($v, 0, [Math]::Min($v.Length, 64)) }
    if ($v -is [uint32]) { return [int64]$v }
    if ($v -is [bool]) { return $v }
    if ($v -is [uint32[]]) { return @($v) }
    if ($v.GetType() -eq $fieldsType) { return (Convert-Fields $v) }
    if ($v -is [System.Collections.IList]) { return @{ count = $v.Count } }
    return $v.GetType().Name
}
function Convert-Fields($f) {
    $o = [ordered]@{}
    foreach ($k in $f.Keys) { $o.Add(('{0:X8}' -f $k), (Convert-Value $f[$k])) }
    return $o
}
$out = [System.Collections.Generic.List[object]]::new()
foreach ($file in Get-ChildItem -LiteralPath $UmdDir -Filter *.ass -File | Where-Object { $_.Extension -ieq '.ass' } | Sort-Object Name) {
    $entry = [ordered]@{ ass = $file.Name }
    $arc = $la.GetConstructor([type[]]@([string])).Invoke([object[]]@($file.FullName))
    try {
        $u32 = $la.GetMethod('U32', $flags); $rd = $la.GetMethod('Read', $flags)
        $ver = $u32.Invoke($arc, @()); $len = [int]$u32.Invoke($arc, @())
        if ($ver -ne 7) { throw "index version $ver" }
        [byte[]]$blob = $rd.Invoke($arc, [object[]]@($len))
        $idx = $parse.Invoke($null, [object[]]@($blob, 0, -1, 0))
        $entry.top_keys = @($idx.Keys | ForEach-Object { '{0:X8}' -f $_ })
        foreach ($name in 'textures_2d', 'texture_mips', 'atlases_2d') {
            $k = Get-NameHash $name
            if ($idx.ContainsKey($k)) { $entry.Add($name, @($idx[$k] | ForEach-Object { Convert-Fields $_ })) } else { $entry.Add($name, $null) }
        }
    } catch {
        $e = $_.Exception; while ($e.InnerException) { $e = $e.InnerException }
        $entry.error = $e.GetType().FullName + ': ' + $e.Message
    } finally { $la.GetMethod('Dispose', $flags).Invoke($arc, @()) }
    $out.Add($entry)
}
[IO.File]::WriteAllText($OutJson, ($out | ConvertTo-Json -Depth 12 -Compress), (New-Object Text.UTF8Encoding $false))
Write-Host "wrote $($out.Count) indexes"
