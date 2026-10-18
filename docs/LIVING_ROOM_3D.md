# 3D 智慧客廳

React Three Fiber 的本機客廳入口保留原有 Streamlit 家庭助理。冷氣、洗衣機連接既有虛擬設備；計畫經使用者核准後，由原本的控制服務透過 MQTT 或 Matter 執行。

## 安裝與啟動

需求與原專案相同，前端另需 Node.js 22.12 以上（建議 Node 22 LTS）。首次在專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web]"
npm.cmd ci --prefix web
npm.cmd run build --prefix web
.\scripts\start.ps1 -With3D
```

開啟 [3D 智慧客廳](http://127.0.0.1:8502)。原有[家庭助理](http://127.0.0.1:8501)保留。程式僅監聽本機，沒有對外部署。

Matter 模式使用 `./scripts/start.ps1 -With3D -Transport matter`，原有 Matter 安裝與配對前提仍適用。若已有啟動紀錄，先以原本的 `stop.ps1` 結束舊服務，再重新啟動；運轉中任務請先在介面完成停止，避免設備重啟被記為故障。

`-LivingRoomPort 8502` 可以調整新入口的連接埠，不可與 `-DashboardPort` 相同。頁面頂端助理連結會跟隨 `-DashboardPort`；單獨啟動 API 時，可用 `HEMS_DASHBOARD_PORT` 設定，預設為 8501。

停止全部程序：

```powershell
.\scripts\stop.ps1 -StopBroker
```

API 程序與其他服務共用啟動紀錄、程序識別核對與日誌目錄。未指定 `-With3D` 時，原有啟動方式不變。

## 操作

- 拖曳旋轉、滾輪縮放，或以觸控旋轉／雙指縮放。右下角按鈕重設 45° 視角。
- 點擊場景家電或下方六個設備按鈕；設備清單支援 Tab、Enter、Space。
- 冷氣設定時段、目標溫度及舒適範圍；洗衣設定最早開始、最晚完成及可跨夜的安靜時段。時間均為 Asia/Taipei，預設時間來自後端邏輯時鐘。
- 「計算並提出計畫」只產生提案。核對費率估算、起訖與條件後，按核准才會排程執行；可在允許狀態修改、取消，運轉中停止需再次確認。
- 狀態未知或故障可重新查核；人工確認設備後再解除。失聯不代表已停止。
- 冷氣、洗衣機動畫必須有新鮮且任務／運轉識別碼相符的回報。超過十個真實秒未回報會停止動畫、保留數值並標記過期。能源、費用與進度都直接使用後端數據。
- 燈泡是本頁展示：開關會改變 3D 光源，重新整理後恢復開啟，不新增 MQTT 設備。電視、窗簾、電風扇僅提供場景資訊；點擊右側電風扇或設備清單的電風扇按鈕會選取同一項設備。電風扇維持靜態模型，尚未串接設備服務。
- WebGL 無法使用時仍可使用 HTML 設備清單與面板。系統設定減少動態效果時，停用連續設備動畫與燈光過渡。

## 前端開發

```powershell
# 一個視窗：API，可獨立啟動；不會自動啟動控制器或模擬器
.\.venv\Scripts\python.exe -m smart_home_energy.api
# 另一個視窗：Vite，/api 代理到 127.0.0.1:8502
npm.cmd run dev --prefix web
```

只有場景與燈泡展示不依賴 Broker。要計算、核准及執行計畫，仍需既有 `ac`、`washer` 與 `control` 服務。不要重複啟動同一個 API；`start.ps1 -With3D` 已包含 API。

API 文件位於 [本機 API 文件](http://127.0.0.1:8502/api/docs)。`GET /api/home` 提供時鐘、偏好、行程、設備回報、目前／最近任務。`POST /api/requests` 僅接受計畫建立／修改、核准、取消、中止、查核與解除異常，回傳 202 代表入列，不能當成設備動作成功。以 `GET /api/requests/{request_id}` 查詢處理結果，再以設備回報確認執行。

同一次提交的重試使用相同請求識別碼，衝突識別碼或舊任務版本回傳 409。後端拒絕非法條件與未允許的請求種類；正式費率報價過期等領域錯誤由既有控制器回報。前端不直接操作 MQTT 或資料庫。

## 檢查

```powershell
npm.cmd run build --prefix web
npm.cmd test --prefix web
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

API 測試使用暫存 SQLite 與假傳輸，不讀寫日常任務；MQTT 驗收也須使用隔離資料庫與主題，避免干擾既有展示。

完整 HTTP／MQTT 驗收使用專用容器（PowerShell，專案根目錄）：

```powershell
docker run -d --name hems-phase2-3d-test -p 127.0.0.1:18884:1883 --mount "type=bind,source=$((Get-Location).Path)\mosquitto\mosquitto.conf,target=/mosquitto/config/mosquitto.conf,readonly" eclipse-mosquitto:2
.\.venv\Scripts\python.exe tests/api_mqtt_smoke.py --recovery --broker-container hems-phase2-3d-test
docker rm -f hems-phase2-3d-test
```

驗收使用獨立 SQLite、MQTT 主題與自動選取的 API 連接埠，啟動真實 `washer`、`ac`、`control`、`api` 程序。檢查未核准不啟動、雙設備運轉、Broker 斷線後回報過期、重新連線保留原行程、自然完成、請求去重及人工中止。只重啟明確指定的 `hems-phase2-*` 測試容器；結果保存在 `artifacts/3d-api-mqtt-smoke.json`，所有測試子程序會在結束時清理。

### 2026-10-18 驗證結果

- 正式前端建置成功；前端 8 項互動測試通過。
- Python 共 81 項測試：79 項通過，2 項需另行啟用的真實模型測試略過。
- Docker Mosquitto 上的 HTTP／MQTT 完整流程通過，含 Broker 中斷超過十秒與原行程恢復。
- `start.ps1 -With3D` 與 `stop.ps1` 實測包含五個服務的啟停；API／前端、Streamlit 健康檢查皆為 HTTP 200，自訂助理連接埠可正確連結。
- 瀏覽器確認 3D 模型渲染、直接點擊洗衣機、旋轉不誤選、浮動名稱、燈泡開關及表單產生待核准卡片。375px 寬度改為單欄，無橫向溢位。
- 畫面保存在 `artifacts/living-room-desktop.png`、`artifacts/living-room-mobile.png`；Python 完整輸出在 `artifacts/3d-python-tests.log`。此輪新增介面的端到端驗收使用 MQTT；Matter 沿用既有傳輸與控制流程，未在本輪另做完整配對測試。
