param([switch]$NoBroker, [ValidateSet('mqtt','matter')][string]$Transport='mqtt',
      [ValidateRange(1,65535)][int]$DashboardPort=8501, [string]$RuntimeDirectory='artifacts/runtime')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '請先建立 .venv 並執行 pip install -e .' }
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
if ($Transport -eq 'matter') {
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'matter/node_modules/@matter/main/package.json'))) { throw '請先執行 npm.cmd ci --prefix matter' }
    $nodeExe = (Get-Command node -ErrorAction Stop).Source
    $publicConfig = & $pythonExe -c 'import json,os; from smart_home_energy.config import Settings,HemsSettings; s=Settings.from_env(); h=HemsSettings.from_env(); print(json.dumps(dict(MQTT_HOST=s.mqtt_host,MQTT_PORT=str(s.mqtt_port),HEMS_MQTT_ROOT=h.mqtt_root,**{k:v for k,v in os.environ.items() if k.startswith("HEMS_MATTER_")})))'
    if ($LASTEXITCODE -ne 0) { throw '無法讀取 Matter / MQTT 設定' }
    $config = $publicConfig | ConvertFrom-Json
    foreach ($property in $config.PSObject.Properties) { [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process') }
    if (-not $env:HEMS_MATTER_STORAGE) { $env:HEMS_MATTER_STORAGE = Join-Path $projectRoot 'artifacts/matter' }
}
try {
    $names = @('ac', 'washer')
    if ($Transport -eq 'matter') { $names += 'matter-bridge' }
    $names += @('control','dashboard')
    foreach ($name in $names) {
        $executable = $pythonExe
        $arguments = @('-m', "smart_home_energy.$name")
        if ($name -eq 'matter-bridge') { $executable = $nodeExe; $arguments = @('matter/bridge.mjs') }
        if ($name -eq 'dashboard') {
            $arguments = @('-m', 'streamlit', 'run', 'src/smart_home_energy/dashboard.py', '--server.address', '127.0.0.1', '--server.port', "$DashboardPort", '--server.headless', 'true', '--browser.gatherUsageStats', 'false')
        }
        $process = Start-Process -FilePath $executable -ArgumentList $arguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimePath "$name.out.log") -RedirectStandardError (Join-Path $runtimePath "$name.err.log")
        $started += @{ name=$name; pid=$process.Id; executable=$executable; started=$process.StartTime.ToUniversalTime().ToString('o'); started_ticks=$process.StartTime.ToUniversalTime().Ticks.ToString() }
    }
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    Start-Sleep -Seconds 2
    foreach ($entry in $started) {
        $process = Get-Process -Id $entry.pid -ErrorAction SilentlyContinue
        if (-not $process) { throw "$($entry.name) 啟動失敗，請查看 artifacts/runtime 的紀錄" }
    }
    Write-Host "已啟動（$Transport）：http://127.0.0.1:$DashboardPort；Matter 首次配對需數十秒，詳見 $runtimePath；停止請執行 scripts/stop.ps1（相同 RuntimeDirectory）"
} catch {
    # Save ownership records so stop.ps1 can clean up only these processes.
    $started | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
    throw
}
