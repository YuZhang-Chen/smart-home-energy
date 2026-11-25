#!/usr/bin/env bash
set -euo pipefail

cd /app

# 若容器內尚未提供 .env，從 .env.example 建立預設檔，方便無須掛載檔案也能直接在雲端 PaaS 啟動
if [ ! -f .env ]; then
    cp .env.example .env
fi

mkdir -p artifacts/runtime

export MQTT_HOST="${MQTT_HOST:-127.0.0.1}"
export MQTT_PORT="${MQTT_PORT:-1883}"
export MQTT_TOPIC="${MQTT_TOPIC:-home/energy/ac}"
export HEMS_MQTT_ROOT="${HEMS_MQTT_ROOT:-home/hems}"
export HEMS_TRANSPORT="${HEMS_TRANSPORT:-mqtt}"
export PORT="${PORT:-7860}"
export HEMS_HOST="${HEMS_HOST:-0.0.0.0}"
export HEMS_COPILOT_RUNTIME_PORT="${HEMS_COPILOT_RUNTIME_PORT:-8503}"
export HEMS_PYTHON_API_URL="http://127.0.0.1:${PORT}"
export HEMS_COPILOT_RUNTIME_URL="http://127.0.0.1:${HEMS_COPILOT_RUNTIME_PORT}"

echo "[1/4] 啟動內建 Mosquitto MQTT Broker (${MQTT_HOST}:${MQTT_PORT})..."
mosquitto -c /app/mosquitto/mosquitto.conf -d
sleep 1

echo "[2/4] 啟動虛擬設備 (AC / Washer / Bulb) 與背景控制服務 (Controller)..."
python -m smart_home_energy.ac > artifacts/runtime/ac.out.log 2> artifacts/runtime/ac.err.log &
python -m smart_home_energy.washer > artifacts/runtime/washer.out.log 2> artifacts/runtime/washer.err.log &
python -m smart_home_energy.bulb > artifacts/runtime/bulb.out.log 2> artifacts/runtime/bulb.err.log &
python -m smart_home_energy.control > artifacts/runtime/control.out.log 2> artifacts/runtime/control.err.log &

echo "[3/4] 啟動 Copilot Runtime (127.0.0.1:${HEMS_COPILOT_RUNTIME_PORT})..."
(cd /app/web && node node_modules/tsx/dist/cli.mjs runtime/server.ts > /app/artifacts/runtime/copilot-runtime.out.log 2> /app/artifacts/runtime/copilot-runtime.err.log) &

echo "[4/4] 啟動 FastAPI 與 3D 智慧客廳入口 (${HEMS_HOST}:${PORT})..."
exec python -m smart_home_energy.api --host "${HEMS_HOST}" --port "${PORT}"
