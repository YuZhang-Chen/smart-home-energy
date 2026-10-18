# 家庭助理與虛擬家電控制

新增 [3D 智慧客廳](docs/LIVING_ROOM_3D.md)：React Three Fiber 場景可旋轉、縮放，點選冷氣與洗衣機設定計畫、核准並查看進度；燈泡可切換場景照明。完成前端安裝與建置後，以 `.\scripts\start.ps1 -With3D` 啟動，開啟 http://127.0.0.1:8502，原有 Streamlit 入口保留。

首頁直接說「幫我開冷氣，順便安排洗衣」，或點快捷按鈕。助理提出計畫後，在同一張卡確認、查看進度與停止；設備入口為「冷氣」「洗衣機」「用電紀錄」。最短流程見 [家庭助理操作](docs/ASSISTANT_UI.md)。

單台電腦、零硬體成本：Main Agent 委派 Washer / AC Agents，分別提出、核准並執行虛擬洗衣與冷氣計畫。vLLM 主用，故障時才使用 Azure OpenAI 備援；沒有模型時可由表單計算排程。設備為教學模擬，費用可選模擬費率或台電三種住宅方案的增量估算，通訊可選 MQTT 或真實 Matter 橋接。

第二階段操作、費率假設及 Matter 設定見 [操作指南](docs/PHASE2_GUIDE.md)，測試與實際模型／Matter 證據見 [驗收紀錄](docs/PHASE2_VALIDATION.md)。

安裝依賴後，可在專案根目錄一次啟動四個背景程序（需要已啟動的 Docker Desktop）：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
.\scripts\start.ps1
# 開啟 http://127.0.0.1:8501，輸入需求或點快捷按鈕
# 完成展示後：
.\scripts\stop.ps1 -StopBroker
```

Matter 模式另需 Node.js 22+，先執行 `npm.cmd ci --prefix matter`，再以 `.\scripts\start.ps1 -Transport matter` 啟動。直接在首頁輸入跨設備需求，洗衣與冷氣各自核准。

背景日誌位於 `artifacts/runtime/`。停止腳本保留 SQLite，運轉途中停止設備程序會在下次啟動回報故障。洗衣任務取消、中止及人工解除請先在畫面操作。詳細流程見 [洗衣操作指南](docs/MAIN_AGENT_GUIDE.md)，測試範圍見 [Main Agent 驗收紀錄](docs/MAIN_AGENT_VALIDATION.md)。

## 啟動

需求：Python 3.12 以上、已啟動的 Docker Desktop（Linux containers）。所有 PowerShell 視窗都先切換至專案根目錄。

首次安裝（已有 `.venv` 可略過建立步驟）：

```powershell
cd D:\lab_project\smart-home-energy
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

專案已提供本機用 `.env`。若是新取得的原始碼，先執行 `Copy-Item .env.example .env`；已有 `.env` 時保留原設定。

**視窗一：啟動 MQTT Broker。**

```powershell
docker compose up -d
docker compose logs -f mosquitto
```

**視窗二：啟動可控冷氣模擬器。** 它同時發布監測資料；請勿再啟動舊 publisher。

```powershell
.\.venv\Scripts\python.exe -m smart_home_energy.ac
```

**另開兩個視窗：啟動虛擬洗衣機與控制服務。** 僅需冷氣監測時可略過。

```powershell
.\.venv\Scripts\python.exe -m smart_home_energy.washer
```

```powershell
.\.venv\Scripts\python.exe -m smart_home_energy.control
```

**最後一個視窗：啟動儀表板。**

```powershell
.\.venv\Scripts\python.exe -m streamlit run src\smart_home_energy\dashboard.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

開啟 [http://127.0.0.1:8501](http://127.0.0.1:8501)。冷氣初始約 30°C、待機 0 W，核准時段內才開機。網頁每兩秒更新一次，接收與重繪可能相差一個更新週期。上述分別啟動的指令為 MQTT 模式；Matter 請使用啟動腳本。

另開視窗可查看原始 MQTT 訊息，不必在 Windows 額外安裝 Mosquitto：

```powershell
docker compose exec mosquitto mosquitto_sub -h 127.0.0.1 -t home/energy/ac -v
```

停止時，在各程序視窗按 `Ctrl+C`，接著執行 `docker compose down`。Broker 日誌視窗的 `Ctrl+C` 只會停止查看日誌。關閉瀏覽器本身不會停止洗衣排程。

## 程式結構

```text
src/smart_home_energy/
  config.py          共用環境設定
  models.py          遙測資料、JSON 編解碼與輸入驗證
  simulator.py       純運算冷氣模型與用電積分
  publisher.py       單調時鐘排程、發布與生命週期
  mqtt_transport.py  Paho 連線、訂閱、重連及訊息入口
  store.py           執行緒安全的最新資料與有界歷史紀錄
  dashboard.py       Streamlit 畫面與定期刷新
  domain.py          洗衣行程、共享邏輯時鐘、模擬費率及確定性排程
  repository.py      SQLite 交易、版本、核准、請求、事件與工具紀錄
  washer.py          虛擬洗衣設備、MQTT 命令去重及自主運轉
  control.py         背景排程、命令確認、狀態核對與恢復
  agent.py           Main Agent JSON 工具迴圈、Azure／vLLM 與預算
  hems_ui.py         自然語言／表單、核准、取消／中止及紀錄
  service_lock.py    防止同一資料庫啟動重複服務
  multi_agent.py     共用預算、批次委派、父子工作與版本去重
  ac_domain.py       舒適計畫、核准範圍及精確用電積分
  ac.py              可控冷氣、自主關機、回溫及相容監測
  ac_control.py      冷氣背景排程、狀態查核及費用提醒
  tariffs.py         台電住宅級距／時間電價、快照及合併增量費用
  device_transport.py 共用設備傳輸介面
  matter_transport.py Node Matter controller 的程序與回饋管理
  home_ui.py         設備次要入口、手動預約、偏好與費率設定
  assistant_ui.py    對話、快捷操作、同頁確認與執行卡片
  assistant_defaults.py 常用偏好與輸入驗證
matter/             鎖定版本的真實 Matter controller、bridge 與擴充群集
mosquitto/           Broker 設定
tests/              運算、資料、傳輸、畫面與真實 Broker 驗收
```

資料只有一個來源：模擬器。儀表板訂閱 MQTT，不另外產生隨機數據或重新計算累積用電。背景 MQTT callback 不操作 Streamlit，畫面從鎖保護的不可變快照讀取資料。`st.cache_resource` 保留訂閱端，正常重繪及不同瀏覽器工作階段共用一條訂閱連線。

## 模擬模型與 MQTT 介面

舊 `publisher` 的獨立監測模型持續開啟，初始室溫 30°C、目標 27°C：

```text
T_next = 27 + (T_previous - 27) × exp(-elapsed_seconds / 600)
P = clamp(100 + 300 × (T - 27), 100, 1200)           # W
E_next = E_previous + P_previous × elapsed_seconds / 3_600_000  # kWh
```

這是教學用模型，沒有室外熱源、人體活動或真實冷氣效率校正。時間差來自單調時鐘，排程延遲時仍按實際秒數積分；內部不四捨五入，只在畫面格式化。

預設 topic 是 `home/energy/ac`，使用 MQTT 3.1.1、QoS 0，並保留最新訊息。訊息欄位固定如下：

```json
{
  "run_id": "每次模擬器啟動產生的新識別碼",
  "sequence": 0,
  "timestamp": "2026-10-16T11:00:00Z",
  "device_id": "ac-1",
  "temperature_c": 30.0,
  "power_w": 1000.0,
  "energy_kwh": 0.0
}
```

舊 publisher 每次啟動歸零；可控 `ac` 保存狀態，用電為本次任務累積值，重啟產生新 boot ID，運轉中重啟回報故障。可控冷氣以精確積分計算用電、結束自主關機並回溫。只同時執行一個冷氣資料來源。MQTT 時間戳使用 UTC，網頁顯示 Asia/Taipei。監測圖表最多 900 筆，任務與設備歷史另由 SQLite 保存。

MQTT 設定集中於專案根目錄的 `.env`，啟動時透過 `python-dotenv` 載入，程式內不另外設定預設值。

| 設定 | 範例值 | 用途 |
| --- | --- | --- |
| `MQTT_HOST` | `127.0.0.1` | Broker 位址 |
| `MQTT_PORT` | `1883` | Broker 連接埠 |
| `MQTT_TOPIC` | `home/energy/ac` | 精確發布與訂閱主題，不能包含 `+`、`#` |

改用其他 Broker 或 topic 時，直接編輯 `.env`，再重新啟動模擬器與儀表板：

```dotenv
MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC=home/energy/ac
```

已存在的程序環境變數優先於 `.env`。缺少必要設定時會顯示明確錯誤；`.env` 已由 `.gitignore` 排除，原始碼保留 `.env.example` 作為範本。

Compose 的主機連接埠固定為 1883；若更換本機連接埠，也要同步修改 Compose 的 port mapping。預設匿名 Broker 只透過主機 `127.0.0.1:1883` 存取，設定唯讀掛載，不持久化訊息。

## 斷線與錯誤行為

- Broker 尚未啟動或中途重啟：Paho 自動重連，延遲逐步增加至最多 10 秒，成功後重新訂閱。
- 離線時模擬器持續計算用電，但不排隊補送遙測；恢復後下一個週期發布最新快照。
- 初次沒有資料：顯示等待狀態及 `—`，不顯示假的零值。
- 資料時間戳超過 10 秒：標示過期並保留最後數值；舊的保留訊息也適用此規則。
- JSON 格式或資料型別錯誤：只在 MQTT 入口記錄警告並略過，後續有效訊息正常處理。
- 設定不合法：模擬器提供明確錯誤後結束，儀表板顯示設定錯誤。

例外捕捉集中在外部訊息與設定入口；模擬運算不吞掉例外，連線重試交由 Paho，模擬器結束時以 `finally` 關閉 MQTT。

## 測試

不需要 Broker 的運算、資料、傳輸與 Streamlit AppTest：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

真實 Broker 驗收（先啟動 Broker，測試使用獨立的 `home/energy/ac/smoke` 主題）：

```powershell
.\.venv\Scripts\python.exe tests\mqtt_smoke.py
```

包含 Broker 延後啟動及重啟的完整驗收，會停止並重新啟動此專案的 Broker，請在示範前執行：

```powershell
.\.venv\Scripts\python.exe tests\mqtt_smoke.py --recovery
```

測試至少連續接收 30 秒，檢查約兩秒的資料間隔、用電增加、錯誤訊息後繼續接收、模擬器重啟與歷史歸零。完整模式另檢查離線期間用電持續增加及沒有補送佇列。成功結果寫入 `artifacts/mqtt-smoke.json`。

若出現 Docker named pipe／daemon 連線錯誤，先確認 Docker Desktop 已啟動且使用 Linux containers；若 1883 被佔用，停止原有 Broker 或同步調整 Compose 和 `MQTT_PORT`。

本次開發的測試環境、實測間隔及畫面核對結果，請見 [PoC 驗收紀錄](docs/VALIDATION.md)。

台電三方案增量費用與尚未支援的完整帳單範圍，見 [電價功能紀錄](docs/TARIFF_ROADMAP.md)。第二階段 MQTT／Matter 復原測試使用獨立測試 Broker，指令見 [驗收紀錄](docs/PHASE2_VALIDATION.md)。

Agent 與排程的已確認方向，以及預留的 Azure OpenAI 設定，請見 [整合決策紀錄](docs/INTEGRATION_DECISIONS.md)。

首版規格與驗收清單，請見 [Main Agent 實作計畫](docs/IMPLEMENTATION_PLAN.md)。

首版已實作 MQTT 虛擬洗衣控制、Main Agent、模擬費用與 SQLite。Matter、冷氣自動控制、實際電價及 Sub Agents 為後續工作。真實 Azure／vLLM 需填妥 `.env` 後另行驗證，目前驗收使用假模型。
