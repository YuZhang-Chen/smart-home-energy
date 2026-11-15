param(
    [switch]$NoBroker,
    [ValidateSet('mqtt', 'matter')][string]$Transport = 'mqtt',
    [ValidateRange(1, 65535)][int]$ApiPort = 8502,
    [ValidateRange(1, 65535)][int]$RuntimePort = 8503,
    [string]$RuntimeDirectory = 'artifacts/runtime',
    [switch]$With3D
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

function Test-TcpPort {
    param([string]$HostName, [int]$Port, [int]$TimeoutMs = 500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect($HostName, $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) {
            return $false
        }
        $client.EndConnect($pending)
        return $client.Connected
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Write-RecentLog {
    param([string]$Name)
    foreach ($suffix in @('err.log', 'out.log')) {
        $path = Join-Path $runtimePath ($Name + '.' + $suffix)
        if (Test-Path -LiteralPath $path) {
            Write-Host ("===== {0} =====" -f $path) -ForegroundColor Yellow
            Get-Content -LiteralPath $path -Tail 30
        }
    }
}

$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    throw '找不到 .venv\Scripts\python.exe。請先建立虛擬環境並安裝專案依賴。'
}

$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    throw '找不到 Node.js。請安裝 Node.js 22.15 以上版本後重新開啟 PowerShell。'
}
$nodeExe = $nodeCommand.Source

if ($ApiPort -eq $RuntimePort) {
    throw 'FastAPI 與 Copilot Runtime 必須使用不同連接埠。'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot '.env'))) {
    throw '找不到專案根目錄 .env。請先依 .env.example 建立 .env 並填入 MQTT 設定。'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\dist\index.html'))) {
    throw '找不到 web\dist\index.html。請先執行 npm.cmd ci --prefix web 及 npm.cmd run build --prefix web。'
}
if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'web\node_modules\tsx\dist\cli.mjs'))) {
    throw '找不到 Copilot Runtime 依賴。請先執行 npm.cmd ci --prefix web。'
}

$runtimePath = [IO.Path]::GetFullPath((Join-Path $projectRoot $RuntimeDirectory))
New-Item -ItemType Directory -Path $runtimePath -Force | Out-Null
$pidFile = Join-Path $runtimePath 'processes.json'

# Refuse to create duplicate resident services. Ignore stale PID records safely.
if (Test-Path -LiteralPath $pidFile) {
    $existingEntries = @()
    try {
        $existingEntries = @(ConvertFrom-Json -InputObject (Get-Content -LiteralPath $pidFile -Raw))
    }
    catch {
        $existingEntries = @()
    }
    $alreadyRunning = $false
    foreach ($entry in $existingEntries) {
        $process = Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
        if ($process) {
            $sameExecutable = $false
            $sameStart = $false
            try {
                $sameExecutable = ($process.Path -eq $entry.executable)
                $sameStart = ($process.StartTime.ToUniversalTime().Ticks.ToString() -eq [string]$entry.started_ticks)
            }
            catch {
                $sameExecutable = $false
                $sameStart = $false
            }
            if ($sameExecutable -and $sameStart) { $alreadyRunning = $true }
        }
    }
    if ($alreadyRunning) {
        throw '偵測到本專案服務已在執行。若要重新啟動，請先執行 .\scripts\stop.ps1。'
    }
    Remove-Item -LiteralPath $pidFile -Force
}

# Validate Python dependencies and load only the non-secret MQTT/Matter settings
# needed by this launcher. The .env file itself is never printed or copied.
& $pythonExe -c 'import fastapi, uvicorn, httpx, ag_ui.core, paho.mqtt.client, openai, dotenv'
if ($LASTEXITCODE -ne 0) {
    throw 'Python 依賴不完整。請執行 .venv\Scripts\python.exe -m pip install -e ".[web]"。'
}

$configScript = @'
import json, os
from smart_home_energy.config import Settings, HemsSettings
settings = Settings.from_env()
hems = HemsSettings.from_env()
values = {
    "MQTT_HOST": settings.mqtt_host,
    "MQTT_PORT": settings.mqtt_port,
    "HEMS_MQTT_ROOT": hems.mqtt_root,
}
for key, value in os.environ.items():
    if key.startswith("HEMS_MATTER_"):
        values[key] = value
print(json.dumps(values, ensure_ascii=False))
'@
$configScriptPath = Join-Path $runtimePath 'read_runtime_config.py'
Set-Content -LiteralPath $configScriptPath -Value $configScript -Encoding ASCII
$configOutput = & $pythonExe $configScriptPath
if ($LASTEXITCODE -ne 0) {
    throw '無法讀取 .env 的必要設定。請檢查 MQTT_HOST、MQTT_PORT 與 MQTT_TOPIC。'
}
try {
    $publicConfig = ($configOutput -join "`n") | ConvertFrom-Json
}
catch {
    throw 'Python 設定檢查沒有回傳有效 JSON，請檢查 .env 設定。'
}
$mqttHost = [string]$publicConfig.MQTT_HOST
$mqttPort = [int]$publicConfig.MQTT_PORT

$env:HEMS_TRANSPORT = $Transport
$env:HEMS_PYTHON_API_URL = "http://127.0.0.1:$ApiPort"
$env:HEMS_COPILOT_RUNTIME_PORT = [string]$RuntimePort
$env:HEMS_COPILOT_RUNTIME_URL = "http://127.0.0.1:$RuntimePort"

if ($Transport -eq 'matter') {
    foreach ($property in $publicConfig.PSObject.Properties) {
        [Environment]::SetEnvironmentVariable($property.Name, [string]$property.Value, 'Process')
    }
    if (-not $env:HEMS_MATTER_STORAGE) {
        $env:HEMS_MATTER_STORAGE = Join-Path $projectRoot 'artifacts\matter'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $projectRoot 'matter\node_modules\@matter\main\package.json'))) {
        throw '找不到 Matter 依賴。請先執行 npm.cmd ci --prefix matter。'
    }
}

# Start the local Mosquitto broker unless one is already listening.
if (-not (Test-TcpPort -HostName $mqttHost -Port $mqttPort)) {
    if ($NoBroker) {
        throw "NoBroker 已指定，但 MQTT Broker $mqttHost`:$mqttPort 無法連線。"
    }
    if ($mqttPort -ne 1883 -or $mqttHost -notin @('127.0.0.1', 'localhost', '::1')) {
        throw "MQTT Broker $mqttHost`:$mqttPort 無法連線；自動 Docker 啟動只支援本機 1883。"
    }
    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCommand) {
        throw '找不到 Docker。請啟動 Docker Desktop，或先自行啟動 MQTT Broker。'
    }
    & $dockerCommand.Source compose up -d mosquitto
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker 無法啟動 Mosquitto。請確認 Docker Desktop 已啟動。'
    }
    $brokerReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-TcpPort -HostName $mqttHost -Port $mqttPort) { $brokerReady = $true; break }
        Start-Sleep -Seconds 1
    }
    if (-not $brokerReady) { throw 'Mosquitto 已要求啟動，但 30 秒內仍無法連線。' }
}

$entries = @(
    @{ name = 'ac'; executable = $pythonExe; arguments = @('-m', 'smart_home_energy.ac'); directory = $projectRoot },
    @{ name = 'washer'; executable = $pythonExe; arguments = @('-m', 'smart_home_energy.washer'); directory = $projectRoot },
    @{ name = 'bulb'; executable = $pythonExe; arguments = @('-m', 'smart_home_energy.bulb'); directory = $projectRoot }
)
if ($Transport -eq 'matter') {
    $entries += @{ name = 'matter-bridge'; executable = $nodeExe; arguments = @('matter/bridge.mjs'); directory = $projectRoot }
}
$entries += @(
    @{ name = 'control'; executable = $pythonExe; arguments = @('-m', 'smart_home_energy.control'); directory = $projectRoot },
    @{ name = 'api'; executable = $pythonExe; arguments = @('-m', 'smart_home_energy.api', '--port', [string]$ApiPort); directory = $projectRoot },
    @{ name = 'copilot-runtime'; executable = $nodeExe; arguments = @('node_modules/tsx/dist/cli.mjs', 'runtime/server.ts'); directory = (Join-Path $projectRoot 'web') }
)

$started = @()
try {
    foreach ($entry in $entries) {
        $stdoutLog = Join-Path $runtimePath ($entry.name + '.out.log')
        $stderrLog = Join-Path $runtimePath ($entry.name + '.err.log')
        $process = Start-Process -FilePath $entry.executable -ArgumentList $entry.arguments `
            -WorkingDirectory $entry.directory -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $stdoutLog -RedirectStandardError $stderrLog
        $startedAt = $process.StartTime.ToUniversalTime()
        $started += @{
            name = $entry.name
            pid = $process.Id
            executable = $entry.executable
            started = $startedAt.ToString('o')
            started_ticks = $startedAt.Ticks.ToString()
        }
        $started | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $pidFile -Encoding UTF8
    }

    $apiReady = $false
    $runtimeReady = $false
    $diagnosticsReady = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (-not $apiReady) { $apiReady = Test-TcpPort -HostName '127.0.0.1' -Port $ApiPort }
        if (-not $runtimeReady) { $runtimeReady = Test-TcpPort -HostName '127.0.0.1' -Port $RuntimePort }
        if ($apiReady -and $runtimeReady) {
            try {
                $diagnostics = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/diagnostics" -f $ApiPort) -TimeoutSec 2
                $diagnosticsReady = ($diagnostics.services.controller -eq 'online' -and
                                     $diagnostics.services.copilot_runtime -eq 'online')
            }
            catch {
                $diagnosticsReady = $false
            }
            if ($diagnosticsReady) { break }
        }
        Start-Sleep -Seconds 1
    }

    $missingProcesses = @()
    foreach ($entry in $started) {
        if (-not (Get-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue)) {
            $missingProcesses += [string]$entry.name
        }
    }
    if ($missingProcesses.Count -gt 0) {
        foreach ($name in $missingProcesses) { Write-RecentLog -Name $name }
        throw ("以下服務程序已停止：" + ($missingProcesses -join ', '))
    }
    if (-not $apiReady -or -not $runtimeReady -or -not $diagnosticsReady) {
        foreach ($name in @('control', 'api', 'copilot-runtime', 'bulb')) { Write-RecentLog -Name $name }
        throw '服務未能完成啟動驗證（API／Runtime／Controller），請查看 artifacts\runtime 日誌。'
    }

    Write-Host ''
    Write-Host '智慧家庭系統已啟動。' -ForegroundColor Green
    Write-Host ("網頁：http://127.0.0.1:{0}" -f $ApiPort) -ForegroundColor Cyan
    Write-Host ("Copilot Runtime：http://127.0.0.1:{0}" -f $RuntimePort) -ForegroundColor Cyan
    Write-Host '停止服務：.\scripts\stop.ps1'
}
catch {
    foreach ($entry in $started) {
        Stop-Process -Id ([int]$entry.pid) -ErrorAction SilentlyContinue
    }
    throw
}
