$ErrorActionPreference = 'Stop'
$repository = (Resolve-Path (Join-Path $PSScriptRoot '..')).ProviderPath
Set-Location -LiteralPath $repository
[Environment]::CurrentDirectory = $repository
$launcherPath = Join-Path $repository 'hy2qianyi.ps1'
$launcher = [ScriptBlock]::Create([IO.File]::ReadAllText($launcherPath, [Text.Encoding]::UTF8))
. $launcher

$pythonHash = (Get-FileHash hy2qianyi.py -Algorithm SHA256).Hash.ToLowerInvariant()
$launcherHash = (Get-FileHash hy2qianyi.ps1 -Algorithm SHA256).Hash.ToLowerInvariant()
$readme = Get-Content README.md -Raw -Encoding UTF8
if ($pythonHash -ne $Hy2QianyiPythonHash -or -not $readme.Contains($launcherHash)) {
    throw 'Published bootstrap checksums do not match.'
}

$bootstrap = [regex]::Match($readme, '(?ms)^```powershell\r?\n(?<script>.*?)^```\r?$').Groups['script'].Value
$download = '$body = $client.DownloadData(''https://raw.githubusercontent.com/liucong552-art/hy2qianyi/main/hy2qianyi.ps1'')'
if (-not $bootstrap.Contains($download)) { throw 'Could not locate the README launcher download.' }
# Feed the released bytes, including the BOM, through the same README bootstrap.
$offlineBootstrap = $bootstrap.Replace($download, '$body = [IO.File]::ReadAllBytes($launcherPath)')
& {
    function Read-Host { throw 'Reached the first launcher prompt.' }
    try {
        & ([ScriptBlock]::Create($offlineBootstrap))
        throw 'The README bootstrap did not launch the migration prompt.'
    }
    catch {
        if ($_.Exception.Message -cne 'Reached the first launcher prompt.') { throw }
    }
}
Write-Host 'The README bootstrap runs the released launcher from its downloaded bytes.'

$script:PortPrompts = [Collections.Generic.List[string]]::new()
$script:EndpointAnswers = [Collections.Generic.Queue[string]]::new()
foreach ($answer in @('192.0.2.10', '65536', '22022', '192.0.2.11', '', '192.0.2.12:22023', '')) {
    $script:EndpointAnswers.Enqueue($answer)
}
function Read-Host([string] $Prompt) {
    $script:PortPrompts.Add($Prompt)
    return $script:EndpointAnswers.Dequeue()
}
$sourceChoice = Read-Hy2QianyiEndpoint 'Source' 'Source IPv4'
$targetChoice = Read-Hy2QianyiEndpoint 'Target' 'Target IPv4'
$prefilledChoice = Read-Hy2QianyiEndpoint 'Other' 'Other IPv4'
if ($sourceChoice.Port -ne 22022 -or $targetChoice.Port -ne 22 -or $prefilledChoice.Port -ne 22023 -or
    $script:PortPrompts[1] -notmatch '^Source SSH .*22' -or
    $script:PortPrompts[4] -notmatch '^Target SSH .*22' -or $script:EndpointAnswers.Count -ne 0) {
    throw 'The Windows launcher did not prompt for each SSH port.'
}
Write-Host 'Windows launcher handles per-host SSH ports and the default port.'

$script:Python = (Get-Command python.exe -CommandType Application | Select-Object -First 1).Source
$remote = Get-Hy2QianyiRemoteCommand 'import-ssh'
$expectedArgs = @('with spaces', 'with "quotes"', 'path\', '', $remote)
$process = Start-Hy2QianyiProcess $script:Python (@('-B', '-c', 'import json,sys; print(json.dumps(dict(arguments=sys.argv[1:])))') + $expectedArgs) $true
try {
    $process.StandardInput.BaseStream.Dispose()
    $actual = ($process.StandardOutput.ReadToEnd() | ConvertFrom-Json).arguments
    $process.WaitForExit()
    if ($process.ExitCode -ne 0 -or $actual.Count -ne $expectedArgs.Count) {
        throw "The native argument list has $($actual.Count) entries, expected $($expectedArgs.Count), with exit code $($process.ExitCode)."
    }
    for ($i = 0; $i -lt $actual.Count; $i++) {
        if ($actual[$i] -cne $expectedArgs[$i]) { throw "Native argument $i was modified." }
    }
}
finally { Close-Hy2QianyiProcess $process }
Write-Host 'Windows native process argument quoting passed.'

$script:Actions = [Collections.Generic.List[string]]::new()
$script:SourceFails = $false

function Start-Hy2QianyiSsh($Endpoint, [string] $Key, [string] $Action) {
    $script:Actions.Add($Action)
    # The frame codec is portable. Stub the Linux-only lock module when
    # importing the VPS implementation into the Windows test process.
    $fixture = "import sys, types; sys.modules['fcntl'] = types.ModuleType('fcntl'); import hy2qianyi; "
    if ($Action -eq 'export-ssh') {
        $program = $fixture + 'hy2qianyi.write_ssh_frame(sys.stdout.buffer, bytes(range(256)) * 1024)'
        if ($script:SourceFails) { $program += '; sys.exit(17)' }
    }
    else {
        $program = $fixture + 'assert hy2qianyi.read_ssh_frame(sys.stdin.buffer) == bytes(range(256)) * 1024'
    }
    return (Start-Hy2QianyiProcess $script:Python @('-B', '-c', $program) ($Action -eq 'export-ssh'))
}

$source = @{ Address = '192.0.2.10'; Port = 22 }
$target = @{ Address = '192.0.2.11'; Port = 22 }
Invoke-Hy2QianyiTransfer $source $target 'unused fixture key'
if (($script:Actions -join ',') -ne 'export-ssh,import-ssh') { throw 'Wrong relay order.' }
Write-Host 'Windows PowerShell delivered the complete binary snapshot without text conversion.'

$script:Actions.Clear()
$script:SourceFails = $true
$failed = $false
try { Invoke-Hy2QianyiTransfer $source $target 'unused fixture key' }
catch {
    $failed = $_.Exception.Message -like '*17*'
    if (-not $failed) { throw }
}
if (-not $failed -or ($script:Actions -join ',') -ne 'export-ssh') {
    throw 'A failed source must never start the target SSH command.'
}
Write-Host 'Windows relay stops before the target when export fails.'
