$qqBotRoot = Split-Path -Parent $PSScriptRoot
$qqBotPidFile = Join-Path $qqBotRoot '.tmp\server.pid'
if (-not (Test-Path -LiteralPath $qqBotPidFile)) {
    Write-Output 'No managed local server PID was recorded.'
    exit 0
}
$qqBotProcessId = [int](Get-Content -LiteralPath $qqBotPidFile -Raw)
$qqBotProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $qqBotProcessId"
$qqBotExpectedExe = Join-Path $qqBotRoot '.venv\Scripts\python.exe'
if ($qqBotProcess -and $qqBotProcess.ExecutablePath -eq $qqBotExpectedExe -and $qqBotProcess.CommandLine -match 'uvicorn\s+qqbot\.app:app') {
    Stop-Process -Id $qqBotProcessId
    Write-Output 'Stopped the managed QQ bot local preview server.'
} else {
    Write-Output 'The recorded PID no longer belongs to this QQ bot preview server; nothing was stopped.'
}
