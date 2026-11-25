---
title: Smart Home Energy & 3D Living Room
emoji: 🏠
colorFrom: blue
colorTo: green
sdk: gradio
sdk_version: 5.49.1
python_version: "3.12"
app_file: app.py
pinned: false
---

# 基於多代理Agent、Matter 與 MQTT 之情境感知智慧家庭能源管理系統

產品展示名稱：**日常之間：家庭能源助理與 3D 智慧客廳**

這個專案以虛擬客廳示範自然語言家庭能源管理：使用者可以安排冷氣、洗衣與照明，查看設備回報、排程預估與家庭增量用電費用。研究題目聚焦在多 Agent 委派、情境感知、數位孿生、設備安全核准與背景控制，將設備操作及狀態回饋整合成可理解、可恢復的操作流程。

系統以**狀態同步型數位孿生（Digital Twin）**連結後端設備資料與 3D 客廳：設備回報經控制服務保存後，前端定期讀取同一份家庭狀態，同步更新場景、設備面板與任務資訊。例如燈泡開關、亮度與色溫會反映在場景燈光；操作須收到設備回報才顯示確認完成。目前同步對象為專案內的虛擬設備端點。

LLM 可串接**地端大型語言模型**：以 vLLM 在本機或區域網路伺服器提供 OpenAI 相容 API，Python Agents 透過可設定的端點與模型名稱使用該服務；同時保留 Azure OpenAI 備援。

前端保留 Vite／React Three Fiber 3D 場景，家庭助理改用 CopilotKit v2 Sidebar；Python Main／Washer／AC／Light Agents、背景控制服務、SQLite、MQTT 與 Matter 保持為設備業務的唯一執行路徑。Copilot Runtime 只轉接 AG-UI 事件，家庭訊息、任務與重播事件仍由 Python／SQLite 管理。

研究問題、系統架構與技術選型整理於[專題總覽](docs/PROJECT_OVERVIEW.md)。

目前實作進度、已知缺口與下一階段順序見[進度盤點與後續規劃](docs/PROGRESS_AND_NEXT_STEPS.md)（2026-11-02）。

P1 純照明規則與 P2 離線量測已提供，可執行 `python -m smart_home_energy.research --repeats 10 --output artifacts/research`，產生獨立 JSON／CSV／Markdown 報告；使用方式及驗收見[離線照明規則](docs/OFFLINE_LIGHTING.md)。

## 主要設計

| 元件 | 責任 |
|---|---|
| React SPA | 3D 客廳、設備抽屜、CopilotKit 對話、用電、偏好、費率與診斷介面 |
| FastAPI（`127.0.0.1:8502`） | 同源 REST／AG-UI API、標準 MCP JSON-RPC 2.0 端點（`/api/mcp`）、對話歷史、SQLite 讀取、Runtime SSE 反向代理 |
| Copilot Runtime（`127.0.0.1:8503`） | `home` Agent discovery、CopilotKit 連線管理與 Python AG-UI Agent 轉接 |
| SmartHomeMCPServer（`mcp_server.py`） | 官方 `mcp` SDK 協定層，依據 `device_capabilities.json` 強制執行 Role-Scoped Tools 與 Resources 權限邊界 |
| Python 常駐服務 | Main 與 5 個設備 Agent（Washer／AC／Light／TV／Curtain）、需求佇列、核准驗證、MQTT／Matter 命令及設備回報 |
| SQLite | 任務與版本、請求、訊息、對話工作、可重播 Agent 事件、設備、能耗及稽核紀錄 |

冷氣和洗衣計畫需各自核准；明確燈泡指令經 Light Agent 入列，收到設備回報後才顯示完成；電視與窗簾可透過階層式子代理或左側控制抽屜即時操作。重新整理或串流重連會從 SQLite 恢復對話與計畫卡，重送相同識別碼不會建立第二筆需求。關閉瀏覽器不會中止 Python 背景工作。

## 快速啟動（安裝與啟動）

需求：Python 3.12+、Node.js 22.15+、Docker Desktop（Linux containers，用於本機 Mosquitto MQTT Broker）。Matter 傳輸另需 Node Matter 依賴。

```powershell
cd D:\lab_project\smart-home-energy

# 1. 建立 Python 虛擬環境並安裝依賴（含 FastAPI、官方 mcp SDK 與測試套件）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[web,test]"

# 2. 建立 .env 設定檔（首次啟動必備，詳見下方「.env 環境變數設定說明」）
if (-not (Test-Path .env)) { Copy-Item .env.example .env }

# 3. 安裝前端依賴並打包 React 3D 客廳靜態檔
npm.cmd ci --prefix web
npm.cmd run build --prefix web

# 4. 一鍵啟動所有背景服務（Mosquitto、虛擬設備、Controller、FastAPI 8502、Copilot Runtime 8503）
.\scripts\start.cmd
# 開啟瀏覽器訪問：http://127.0.0.1:8502

# 5. 展示完成後停止服務：
.\scripts\stop.ps1 -StopBroker
```

啟動腳本預設啟動 Broker、虛擬冷氣／洗衣／燈泡、Controller、FastAPI／React 與 Copilot Runtime。`start.cmd` 會自動使用 PowerShell 7（若未安裝則使用 Windows PowerShell 5.1），並只對這次啟動程序套用 `ExecutionPolicy Bypass`，避免下載檔案的簽章政策阻擋啟動。已有 Broker 時加 `-NoBroker`；選用 Matter 時加 `-Transport matter` 並先執行 `npm.cmd ci --prefix matter`。入口連接埠可用 `-ApiPort` 與 `-RuntimePort` 設定；舊 `-With3D` 參數保留相容，但不再切換介面。

日誌與服務程序紀錄位於 `artifacts/runtime/`。停止腳本會核對程序 ID、執行檔與啟動時間；SQLite 與 Matter 配對資料會保留。設備執行途中停止設備服務時，下次啟動會依原有規則回報需查核的狀態。

## `.env` 環境變數設定說明

啟動腳本 `scripts/start.ps1` 會強制檢查專案根目錄是否存在 `.env`。首次啟動前請先由範本複製：

```powershell
Copy-Item .env.example .env
```

以下為 `.env` 各區塊設定說明（完整範本見 [.env.example](.env.example)）：

### 1. MQTT 與資料庫路徑（必要基礎設定）

預設值可直接搭配本機 Docker Mosquitto（`127.0.0.1:1883`）運作，無須修改即可啟動：

```dotenv
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC=home/energy/ac
HEMS_MQTT_ROOT=home/hems

# 控制端與各虛擬設備必須使用獨立的 SQLite 檔案（HEMS_DB 與 HEMS_DEVICE_DB 不可相同）
HEMS_DB=artifacts/hems.sqlite3
HEMS_DEVICE_DB=artifacts/washer.sqlite3
HEMS_AC_DB=artifacts/ac.sqlite3
HEMS_BULB_DB=artifacts/bulb.sqlite3
```

### 2. AI 語言模型設定（vLLM 主用／Azure OpenAI 備援）

系統採用 **vLLM 優先、Azure OpenAI 備援** 的雙層模型路由：
- **情境 A：無模型模式（預設全留白）**
  若 `VLLM_*` 與 `AZURE_OPENAI_*` 全部留白，系統仍可正常啟動！此時家庭助理支援**無模型確定性快速指令**（如明確冷氣溫度／時間、關閉冷氣、燈泡亮度／色溫調整、電視與窗簾控制），且 3D 客廳所有設備抽屜與排程表單皆可完整操作。
- **情境 B：串接地端或區網 vLLM（推薦）**
  `VLLM_BASE_URL` **必須以 `/v1` 結尾**；若伺服器未啟用 API Key 驗證，`VLLM_API_KEY` 可留白：
  ```dotenv
  VLLM_BASE_URL=http://127.0.0.1:8000/v1
  VLLM_API_KEY=
  VLLM_MODEL=Qwen/Qwen2.5-7B-Instruct
  ```
- **情境 C：串接 Azure OpenAI（可單獨使用或作為 vLLM 的自動備援）**
  `AZURE_OPENAI_ENDPOINT` **必須使用 `https://`**，且三個欄位皆須填寫完整：
  ```dotenv
  AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com
  AZURE_OPENAI_API_KEY=your-api-key
  AZURE_OPENAI_DEPLOYMENT=gpt-4o-mini
  ```
> **注意**：同一組模型設定（vLLM 或 Azure）若不啟用請**整組留白**；若只填寫部分欄位（例如填了 `VLLM_BASE_URL` 卻漏填 `VLLM_MODEL`），Agent 會回報設定不完整以防止誤連線。

### 3. Agent 共用預算與模擬電價參數

單次家庭對話的階層式 Multi-Agent 共用模型請求上限與時間預算（上限為 `15` 次請求、總計 `120` 秒、單次請求 `20` 秒）：

```dotenv
AGENT_MAX_REQUESTS=15
AGENT_MAX_SECONDS=120
AGENT_REQUEST_SECONDS=20

# 洗衣機各階段預設分鐘數、功率（W）與模擬尖離峰電價（NT$/kWh）
WASH_MINUTES=30
RINSE_MINUTES=15
SPIN_MINUTES=15
WASH_POWER_W=200
RINSE_POWER_W=100
SPIN_POWER_W=400
TARIFF_PEAK=6
TARIFF_OFFPEAK=2.5
```

### 4. 設備傳輸協定（MQTT／Matter）

預設走 `mqtt`；若以 `.\scripts\start.ps1 -Transport matter` 啟動 Matter Bridge 模式，會使用下列配對與儲存設定：

```dotenv
HEMS_TRANSPORT=mqtt
HEMS_MATTER_STORAGE=artifacts/matter
HEMS_MATTER_PORT=5540
HEMS_MATTER_DISCRIMINATOR=3840
HEMS_MATTER_PASSCODE=20202021
MATTER_LOG_LEVEL=error
```

## 開發檢查

```powershell
npm.cmd test --prefix web
npm.cmd run build --prefix web
.\.venv\Scripts\python.exe -m pytest
```

測試使用假模型、暫存 SQLite 與假傳輸；真實模型、MQTT 與 Matter 驗收應使用隔離設定。API 文件位於 `http://127.0.0.1:8502/api/docs`。

可另外執行 opt-in 全流程驗收：啟動專用 Broker 於 `127.0.0.1:18884`，並確認 `.env` 已設定真實模型，再執行：

```powershell
docker run --detach --name hems-copilot-e2e --publish 127.0.0.1:18884:1883 --mount "type=bind,source=$((Resolve-Path .\mosquitto\mosquitto.conf).Path),target=/mosquitto/config/mosquitto.conf,readonly" eclipse-mosquitto:2
.venv\Scripts\python.exe tests\copilotkit_live_smoke.py --mqtt-port 18884
docker rm --force hems-copilot-e2e
```

此測試會建立隔離 SQLite／Matter fabric，經 Copilot Runtime 呼叫模型、建立計畫卡，透過版本化 API 修改及核准，最後控制 Matter bridge 後的虛擬冷氣與洗衣設備；不連接實體家電。執行結果寫入 `artifacts/copilotkit-live-smoke.json`。完整驗收紀錄見 [VALIDATION.md](docs/VALIDATION.md)。

## 程式結構

```text
src/smart_home_energy/
  api.py                 FastAPI REST、AG-UI SSE、MCP 路由與 Runtime 同源代理
  mcp_server.py          官方 MCP SDK Server、Role-Scoped Tools／Resources 與 JSON-RPC 2.0 端點
  agent_roles.py         讀取並驗證 device_capabilities.json 角色與設備權責表
  assistant_service.py   對話佇列、訊息持久化、AG-UI 重播
  repository.py          SQLite schema v4、任務版本與持久化事件
  agent.py               Main Agent、vLLM／Azure 與背景 Agent worker
  multi_agent.py         共用預算、Main／5 台設備委派與任務去重
  light_agent.py         Light Agent 照明工具與色溫／亮度解析
  tv.py / curtain.py     TV Agent 與 Curtain Agent 工具、排程與同源 REST API
  control.py             請求核准、設備命令確認與背景恢復
  ac.py / washer.py / bulb.py
                         虛擬設備、遙測與回報
  tariffs.py             台電費率、月基準與增量費用
  device_transport.py / matter_transport.py
                         MQTT／Matter 共用設備傳輸
web/src/                 React SPA、CopilotKit、R3F 場景與家庭管理頁面
web/runtime/server.ts    TypeScript Copilot Runtime 與 Python AgentRunner
matter/                  Node Matter controller 與 bridge
tests/                   Python domain、API、MCP、Agent 與傳輸測試
```

操作說明與驗收紀錄見 [家庭助理操作](docs/ASSISTANT_UI.md)、[3D 智慧客廳](docs/LIVING_ROOM_3D.md)、[第二階段指南](docs/PHASE2_GUIDE.md) 及 [驗收紀錄](docs/VALIDATION.md)。
