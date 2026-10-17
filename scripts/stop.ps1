param([switch]$StopBroker)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pidFile = Join-Path $projectRoot 'artifacts\runtime\processes.json'
if (Test-Path -LiteralPath $pidFile) {
    $remaining = @()
    foreach ($entry in (Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if ($process) {
            # PID reuse must never stop a different process.
            $sameExecutable = $process.Path -eq $entry.executable
            $sameStart = $process.StartTime.ToUniversalTime().Ticks.ToString() -eq $entry.started_ticks
            if ($sameExecutable -and $sameStart) { Stop-Process -Id $entry.pid }
            else { Write-Warning "略過 PID $($entry.pid)：程序識別與啟動紀錄不符"; $remaining += $entry }
        }
    }
    if ($remaining.Count -gt 0) { $remaining | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8 }
    else { Remove-Item -LiteralPath $pidFile }
}
if ($StopBroker) { docker compose stop mosquitto }
Write-Host '程序已停止；SQLite 紀錄保留。運轉中的設備程序被停止後，下次啟動會回報需人工處理的故障。'
