# CopilotKit 遷移驗收

2026-11-02 的目前工作目錄驗收與 MQTT／Matter 復原重驗，見 [P0–P2 驗收紀錄](ACCEPTANCE_2026-11-02.md)。以下保留 2026-10-22 的歷史結果。

驗收日期：2026-10-22（Asia/Taipei）。遷移驗收另補上真實 vLLM、隔離 MQTT Broker 與 Matter bridge 的端到端測試；沒有操作實體家庭設備。

## 自動測試

- Python：`python -m pytest tests -q`，**83 passed、3 skipped**。略過項目需要明確啟用真實模型或長流程 Agent 整合。
- React：`npm.cmd test --prefix web`，**21 tests passed**。
- 前端正式建置：`npm.cmd run build --prefix web`，TypeScript、Runtime 型別檢查及 Vite 建置通過。
- Runtime 型別檢查：`npm.cmd run check:runtime --prefix web` 通過。

測試使用專案 `.venv`、假模型與隔離 SQLite。Windows 系統暫存目錄不可寫時，測試暫存改放在 `artifacts/test-tmp/`。

## 本機 HTTP 與 SSE

以暫存資料庫在本機 18502／18503 啟動 FastAPI 與 Copilot Runtime，確認：

- Runtime `/api/copilotkit/info` 可探索 `home` Agent，並透過 FastAPI `/api/copilotkit/info` 同源代理讀取。
- 由同源 `/api/copilotkit/agent/home/run` 送出 AG-UI 工作，收到 `text/event-stream`、`RUN_STARTED` 及排隊進度事件。
- 使用者訊息與工作 ID 寫入 Python／SQLite；重新查詢 `/api/assistant` 可恢復訊息及目前執行狀態。
- `/api/diagnostics` 回報 Copilot Runtime 在線。隔離驗收未啟動背景 Controller，因此其狀態如預期顯示離線。

此煙霧測試驗證代理與串流傳輸；Agent 工作只送入隔離佇列，沒有呼叫真實模型或設備。

## CopilotKit、真實模型與 Matter 端到端

以 `.env` 中設定的 vLLM 及隔離資料庫、MQTT topic、Matter fabric 執行 `tests/copilotkit_live_smoke.py`，完整通過：

- Copilot Runtime discovery、FastAPI 同源代理、AG-UI SSE、Python 對話與 Agent 狀態持久化。
- vLLM 經 Main Agent 分別委派 Washer／AC Agent，產生兩張各自待核准的提案卡；核准前 command queue 為空。
- 透過版本化 `schedule_form`／`schedule_ac_form` 修改計畫，再逐一經 REST 核准；驗證修改後版本及費用上限。
- 兩項設備命令經 Matter controller／bridge 傳輸到隔離虛擬設備。冷氣與洗衣收到回報並完成，`/api/home` 讀回相同 run ID 與設備狀態。

詳細機器可讀摘要：`artifacts/copilotkit-live-smoke.json`；本次實際執行目錄：`artifacts/copilot-live-861a464536/`。先啟動隔離 Broker（`127.0.0.1:18884`），再重跑：

```powershell
docker run --detach --name hems-copilot-e2e --publish 127.0.0.1:18884:1883 --mount "type=bind,source=$((Resolve-Path .\mosquitto\mosquitto.conf).Path),target=/mosquitto/config/mosquitto.conf,readonly" eclipse-mosquitto:2
.venv\Scripts\python.exe tests\copilotkit_live_smoke.py --mqtt-port 18884
docker rm --force hems-copilot-e2e
```

此 opt-in 測試會呼叫已設定的真實模型並透過 React 所用的 REST 核准路徑執行虛擬 Matter 設備；不連接實體冷氣、洗衣機或燈泡。

## MQTT 與 Matter 設備驗收

- `tests/api_mqtt_smoke.py --port 18884 --recovery --broker-container hems-phase2-copilot-20261022` 通過：API 提案／核准、三設備回報、Broker 中斷與復原、冷氣／洗衣完成、避免重複啟動，以及人工中止。
- `tests/phase2_smoke.py --transport mqtt --recovery --broker-container hems-phase2-copilot-20261022` 通過燈泡控制、雙設備運轉、Broker 復原、行程完成、人工中止與故障復歸。
- `tests/phase2_smoke.py --transport matter --recovery --broker-container hems-phase2-copilot-20261022` 通過相同 Matter 實測，另驗證 Matter controller／bridge 重啟、失聯偵測、拒絕竄改重複命令及無 MQTT 旁路。兩台設備用電回報為 0.225 kWh。

上述傳輸測試使用本機隔離 Broker 和專案內的 Matter／設備模擬端點，覆蓋真實協定堆疊與服務流程；它們沒有連到實體 Matter 或 MQTT 家電。先前完整的歷史驗收另見 [第二階段驗收](PHASE2_VALIDATION.md) 及 [Main Agent 驗收](MAIN_AGENT_VALIDATION.md)。
