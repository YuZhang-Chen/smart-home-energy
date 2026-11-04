# 3D 智慧客廳與家庭助理

React Three Fiber 場景與 CopilotKit v2 Sidebar 共用一個 Vite React SPA。冷氣、洗衣機與燈泡繼續使用既有 Python Agent、Controller、SQLite 與 MQTT／Matter；CopilotKit Runtime 以 AG-UI 轉接，不執行第二套設備邏輯。

## 安裝與啟動

需求為 Python 3.12+、Node.js 22.15+ 與 Docker Desktop。Matter 模式另需 `matter` 目錄的 Node 相依套件。首次在專案根目錄執行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[web,test]"
npm.cmd ci --prefix web
npm.cmd run build --prefix web
.\scripts\start.ps1
```

開啟 [家庭入口](http://127.0.0.1:8502)。React 與 FastAPI 共用 8502；FastAPI 將 `/api/copilotkit/*` 同源代理至本機 Copilot Runtime 8503。兩個入口只綁定本機。

Matter 模式執行 `npm.cmd ci --prefix matter` 後使用 `.\scripts\start.ps1 -Transport matter`。已有 Broker 時可加 `-NoBroker`。`-With3D` 保留作為相容參數，現在預設即啟動 React、API 與 Runtime。

可用 `-ApiPort`、`-RuntimePort` 修改服務連接埠；Runtime 的 Python Agent URL 會隨 API 連接埠同步調整。停止全部服務：

```powershell
.\scripts\stop.ps1 -StopBroker
```

程序紀錄與服務日誌位於 `artifacts/runtime/`。停止腳本先核對程序 ID、執行檔與啟動時間，再停止本次啟動的程序；SQLite 與 Matter 配對資料會保留。

## 操作

- 拖曳旋轉、滾輪縮放，或以觸控旋轉／雙指縮放；右下按鈕重設 45° 視角。
- 場景下方控制晝夜展示時間。此展示時鐘只改變自然光與天空，不會自動操作設備。
- 點擊場景設備或設備清單會開啟左側操作抽屜。冷氣與洗衣表單、燈泡控制沿用原有 REST 請求與設備回報。
- 右側家庭助理可輸入多設備需求。冷氣與洗衣計畫各自核准；燈泡操作要等設備狀態回報後才顯示完成。
- 設備動畫只使用新鮮且任務／運轉識別碼吻合的後端回報。超過十秒未回報會停止動畫、保留數值並標示過期。
- 行動版設備抽屜與家庭助理互斥；Escape 可關閉抽屜並將焦點交還觸發按鈕。375px 寬度維持單欄。

頂部導覽包含客廳、用電紀錄、偏好設定與系統診斷。用電頁讀取最近 900 筆功率樣本、家庭已核准負載費用與近期任務；偏好頁包含冷氣／洗衣預設、費率方案、逐月基準、費率來源更新與展示時鐘；診斷頁顯示服務、請求、Agent、工具、設備命令與稽核紀錄。

## HTTP／AG-UI 介面

API 文件：[http://127.0.0.1:8502/api/docs](http://127.0.0.1:8502/api/docs)。

- `GET /api/assistant`：單一家庭對話訊息、工作狀態與已保存計畫卡。
- `POST /api/assistant/ag-ui`：驗證 AG-UI `RunAgentInput`、持久化使用者訊息與工作，回傳 SSE。
- `GET /api/assistant/runs/{id}/events?after=N`：依 SQLite 事件序號重播及續接串流。
- `GET /api/tasks/{id}`：查詢任務目前版本，核准前後端皆驗證版本。
- `GET /api/energy`、`/api/settings`、`/api/diagnostics`：用電、偏好／費率／時鐘與診斷讀取模型。
- CopilotKit `/api/copilotkit/*`：由 FastAPI 反向代理到 8503 Runtime，前端維持同源連線。

`POST /api/requests` 回傳 202 只代表請求入列；以 `GET /api/requests/{request_id}` 查詢處理結果，並以設備狀態回報確認實際執行。背景工作不依賴瀏覽器連線持續執行。

## 開發與驗收

開發時分別啟動既有 Broker／設備／Control，接著執行 API、Runtime 與 Vite：

```powershell
.\.venv\Scripts\python.exe -m smart_home_energy.api --port 8502
npm.cmd run runtime --prefix web
npm.cmd run dev --prefix web
```

正式檢查：

```powershell
npm.cmd test --prefix web
npm.cmd run build --prefix web
.\.venv\Scripts\python.exe -m pytest
```

測試以假模型、暫存 SQLite 與假傳輸為主。真實模型及 MQTT／Matter 驗收應使用隔離設定，避免干擾日常任務。
