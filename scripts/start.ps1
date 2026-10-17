param([switch]$NoBroker)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '請先建立 .venv 並執行 pip install -e .' }
$runtimePath = Join-Path $projectRoot 'artifacts\runtime'
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
$pidFile = Join-Path $runtimePath 'processes.json'
if (Test-Path -LiteralPath $pidFile) { throw '已有啟動紀錄，請先執行 scripts/stop.ps1（避免重複服務）' }
if (-not $NoBroker) {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'MQTT Broker 啟動失敗' }
}
$started = @()
try {
    foreach ($name in @('publisher', 'washer', 'control', 'dashboard')) {
        $arguments = @('-m', "smart_home_energy.$name")
        if ($name -eq 'dashboard') {
            $arguments = @('-m', 'streamlit', 'run', 'src/smart_home_energy/dashboard.py', '--server.address', '127.0.0.1', '--server.port', '8501', '--server.headless', 'true', '--browser.gatherUsageStats', 'false')
        }
        $process = Start-Process -FilePath $pythonExe -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath "$name.out.log") -RedirectStandardError (Join-Path $runtimePath "$name.err.log")
        $started += @{ name=$name; pid=$process.Id; executable=$pythonExe; started=$process.StartTime.ToUniversalTime().ToString('o'); started_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString() }
    }
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    Start-Sleep -Seconds 2
    foreach ($entry in $started) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if (-not $process) { throw "$($entry.name) 啟動失敗，請查看 artifacts/runtime 的紀錄" }
    }
    Write-Host '已啟動：http://127.0.0.1:8501；停止請執行 scripts/stop.ps1'
} catch {
    # Save ownership records so stop.ps1 can clean up only these processes.
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    throw
}
