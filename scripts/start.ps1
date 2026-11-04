param([switch]$NoBroker, [ValidateSet('mqtt','matter')][string]$Transport='mqtt',
      [ValidateRange(1,65535)][int]$ApiPort=8502, [ValidateRange(1,65535)][int]$RuntimePort=8503,
      [string]$RuntimeDirectory='artifacts/runtime', [switch]$With3D)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$nodeExe = (Get-Command node -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '請先建立 .venv 並執行 pip install -e ".[web]"' }
if ($ApiPort -eq $RuntimePort) { throw 'FastAPI 與 Copilot Runtime 必須使用不同連接埠' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web/dist/index.html'))) { throw '請先執行 npm.cmd ci --prefix web 及 npm.cmd run build --prefix web' }
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web/node_modules/tsx/dist/cli.mjs'))) { throw '請先執行 npm.cmd ci --prefix web' }
& $pythonExe -c 'import fastapi, uvicorn, httpx, ag_ui.core'
if ($LASTEXITCODE -ne 0) { throw '請先執行 .venv/Scripts/python.exe -m pip install -e ".[web]"' }
$runtimePath = [IO.Path]::GetFullPath((Join-Path $projectRoot $RuntimeDirectory))
New-Item -ItemType Directory -Force -Path $runtimePath | Out-Null
$pidFile = Join-Path $runtimePath 'processes.json'
if (Test-Path -LiteralPath $pidFile) { throw '已有啟動紀錄，請先執行 scripts/stop.ps1（避免重複服務）' }
if (-not $NoBroker) {
    docker compose up -d
    if ($LASTEXITCODE -ne 0) { throw 'MQTT Broker 啟動失敗' }
}
$started = @()
$env:HEMS_TRANSPORT = $Transport
$env:HEMS_PYTHON_API_URL = "http://127.0.0.1:$ApiPort"
$env:HEMS_COPILOT_RUNTIME_PORT = "$RuntimePort"
$env:HEMS_COPILOT_RUNTIME_URL = "http://127.0.0.1:$RuntimePort"
if ($Transport -eq 'matter') {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'matter/node_modules/@matter/main/package.json'))) { throw '請先執行 npm.cmd ci --prefix matter' }
    $publicConfig = & $pythonExe -c 'import json,os; from smart_home_energy.config import Settings,HemsSettings; s=Settings.from_env(); h=HemsSettings.from_env(); print(json.dumps(dict(MQTT_HOST=s.mqtt_host,MQTT_PORT=str(s.mqtt_port),HEMS_MQTT_ROOT=h.mqtt_root,**{k:v for k,v in os.environ.items() if k.startswith("HEMS_MATTER_")})))'
    if ($LASTEXITCODE -ne 0) { throw '無法讀取 Matter / MQTT 設定' }
    $config = $publicConfig | ConvertFrom-Json
    foreach ($property in $config.PSObject.Properties) { [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process') }
    if (-not $env:HEMS_MATTER_STORAGE) { $env:HEMS_MATTER_STORAGE = Join-Path $projectRoot 'artifacts/matter' }
}
try {
    $entries = @(
        @{ name='ac'; executable=$pythonExe; arguments=@('-m','smart_home_energy.ac'); directory=$projectRoot },
        @{ name='washer'; executable=$pythonExe; arguments=@('-m','smart_home_energy.washer'); directory=$projectRoot },
        @{ name='bulb'; executable=$pythonExe; arguments=@('-m','smart_home_energy.bulb'); directory=$projectRoot }
    )
    if ($Transport -eq 'matter') { $entries += @{ name='matter-bridge'; executable=$nodeExe; arguments=@('matter/bridge.mjs'); directory=$projectRoot } }
    $entries += @(
        @{ name='control'; executable=$pythonExe; arguments=@('-m','smart_home_energy.control'); directory=$projectRoot },
        @{ name='api'; executable=$pythonExe; arguments=@('-m','smart_home_energy.api','--port',"$ApiPort"); directory=$projectRoot },
        @{ name='copilot-runtime'; executable=$nodeExe; arguments=@('node_modules/tsx/dist/cli.mjs','runtime/server.ts'); directory=(Join-Path $projectRoot 'web') }
    )
    foreach ($entry in $entries) {
        $process = Start-Process -FilePath $entry.executable -ArgumentList $entry.arguments -WorkingDirectory $entry.directory -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath "$($entry.name).out.log") -RedirectStandardError (Join-Path $runtimePath "$($entry.name).err.log")
        $started += @{ name=$entry.name; pid=$process.Id; executable=$entry.executable; started=$process.StartTime.ToUniversalTime().ToString('o'); started_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString() }
    }
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    Start-Sleep -Seconds 2
    foreach ($entry in $started) {
        if (-not (Get-Process -Id $entry.pid -ErrorAction SilentlyContinue)) { throw "$($entry.name) 啟動失敗，請查看 $runtimePath" }
    }
    Write-Host "已啟動（$Transport）：http://127.0.0.1:$ApiPort（React／FastAPI），Copilot Runtime：127.0.0.1:$RuntimePort；停止請執行 scripts/stop.ps1。"
} catch {
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    throw
}
