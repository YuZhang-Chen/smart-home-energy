# 第二階段驗收紀錄

日期：2026-10-17（Asia/Taipei）。環境：Windows、Python 3.12.13、Node.js 22.14.0、Mosquitto 2、`@matter/main@0.17.9`。操作見 [PHASE2_GUIDE.md](PHASE2_GUIDE.md)。

此文件保留第二階段原驗收結果；後續家庭助理 UI 改版及最新測試見 [ASSISTANT_UI_VALIDATION.md](ASSISTANT_UI_VALIDATION.md)。

## 一般測試

完整 unittest / Streamlit AppTest：67 項，`OK (skipped=2)`，約 13.77 秒，輸出保存於 `artifacts/final-unittest.log`。兩個略過項目分別為真實家庭模型測試與連線測試類別；不是模型連線成功紀錄。一般測試不互動、不呼叫付費模型。`compileall`、`pip check`、三個 Matter 程式的 Node 語法檢查均通過。

```powershell
$env:RUN_LLM_CONNECT_TESTS='0'
$env:RUN_AGENT_INTEGRATION_TESTS='0'
$env:TEMP=Join-Path (Get-Location) 'artifacts/tmp'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests scripts
.\.venv\Scripts\python.exe -m pip check
node --check matter/bridge.mjs
node --check matter/controller.mjs
node --check matter/common.mjs
```

| 範圍 | 證據 |
| --- | --- |
| 遷移與任務 | 舊 SQLite 原地遷移、版本／歷史保留、每設備唯一名額、兩台並存、全設備時鐘切換保護 |
| Agent（假模型） | Main 與兩個子 Agent 多輪工具、共用預算、部分成功、委派重試去重、過期模型工作拒絕、備援保留上下文且沿用 Azure |
| 控制與核准 | 沿用洗衣取消／中止／恢復；未核准、錯誤時鐘／boot、過期版本拒絕；冷氣限制內改排／越界重新核准 |
| 冷氣 | 準時開關、舒適達標／開始時無解、關機回溫、重啟故障、精確積分與費率切換邊界、1 倍與 60 倍一致；舒適偏離／費用提醒不自行停機 |
| 正式費率 | 三方案固定官方資料、累進級距、2,000 度門檻、夏月／離峰日／午夜／跨月、合併負載、報價版本、非時間電價無虛假移時節費 |
| UI | 家庭需求持久化、冷氣表單／獨立核准／取消、費率基準保存、下載失敗保留快照；畫面不建立執行器或呼叫模型 |

## 真實模型：分開記錄

使用者於 2026-10-17 回報 `tests/test_llm_connect.py` 模型連線測試成功。這是使用者回報，與下列本次執行結果分開。

本次明確啟用 `RUN_AGENT_INTEGRATION_TESTS=1`，實際 vLLM 完成家庭需求 → Main 委派 → Washer / AC 多輪工具 → 兩份待核准提案：15 次模型請求、約 71.66 秒，結果 completed，保存於 `artifacts/live-household.json`。使用隔離資料庫與模擬遙測，不自行核准或操作設備。Azure 實際故障切換未在本次對外實測；隔離 HTTP 與假模型測試已驗證。

```powershell
$env:RUN_AGENT_INTEGRATION_TESTS='1'
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_live_household.py -v
```

此命令會使用 `.env` 的真實模型設定並可能計費。連線測試另以 `RUN_LLM_CONNECT_TESTS=1` 明確啟用；未啟用時於讀取互動輸入前略過。

## 真實 MQTT 與 Matter

使用獨立 `hems-phase2-mosquitto` 容器、主機埠 18884、每次獨立 topic／資料庫／Matter storage。原本 1883 的 Broker 不受復原測試影響。測試不使用模型。

```powershell
docker run -d --name hems-phase2-mosquitto -p 127.0.0.1:18884:1883 -v "${PWD}/mosquitto/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" eclipse-mosquitto:2
.\.venv\Scripts\python.exe tests/phase2_smoke.py --transport mqtt --recovery
.\.venv\Scripts\python.exe tests/phase2_smoke.py --transport matter --recovery
# 完成後清理自己建立的測試容器：
docker rm -f hems-phase2-mosquitto
```

兩種模式均實際啟動虛擬洗衣與冷氣程序，Matter 另實際啟動 Node controller 與 bridge 並完成配對。驗證雙設備同時運轉、同 command_id 去重、控制服務狀態恢復、Broker 中斷後相同 run 繼續、洗衣 0.225 kWh、冷氣設定 26.5°C 與結束關機、正式費率快照命令、人工中止、設備重啟故障及人工解除。

Matter 另驗證同一命令 ID 搭配不同上下文被拒絕、原持久化結果未被覆寫，controller 重啟保留配對，停用 bridge 後連線變為失效、新任務不能啟動，底層 MQTT 設備仍在而沒有直接控制；恢復 bridge 後重新讀取狀態成功。衝突命令測試預期出現 Matter Failure 狀態。這是實際 Matter 通訊測試，並非以 MQTT 冒充 Matter。

本次 Matter 的雙設備完成檢查約 70.75 個真實秒、洗衣用電 0.225 kWh；MQTT 約 69.42 秒、同為 0.225 kWh。時間包含查核與復原步驟，不能當成設備或模型效能基準。

報告：`artifacts/phase2-mqtt-smoke.json` 與 `artifacts/phase2-matter-smoke.json`，各報告含本次隔離目錄及檢查結果。測試結束會關閉自己啟動的設備／Node 程序；復原模式最後恢復測試 Broker。

## 瀏覽器、啟停與來源

以 `start.ps1 -NoBroker -Transport matter -DashboardPort 8505 -RuntimeDirectory artifacts/script-validation/runtime` 實際啟動五個程序。瀏覽器提交冷氣表單、確認計畫／達標時間／估價、獨立核准，畫面顯示已排程；最後取消並確認無未完成冷氣任務。截圖：`artifacts/script-validation/approved-ac.png`。

`stop.ps1` 以相同 RuntimeDirectory 清理相符程序與子程序；查核後沒有該測試的 Python／Node 程序。SQLite 與驗收輸出保留。

最終已移除本次建立的隔離測試 Broker，原本 `smart-home-energy-mosquitto-1` 的 1883 服務保持運作，沒有留下待執行的 UI 測試任務。

台電 JSON 已保存原始內容、來源、實施日、下載日期及 SHA256。2026 日曆為官方 PDF 索引表格核對，下載原 PDF 失敗的限制已記在資料 provenance，沒有宣稱下載成功；2027 官方 PDF 已下載、解析紅色日期、渲染並目視查核。測試包含 2026-04-06 不能當成台電離峰日的反例。

## 實測邊界

端到端證據分為真實模型提出計畫、UI 核准及真實設備／Matter 執行三層；沒有宣稱一個無人介入測試直接從付費模型自動核准到實體設備。設備均為教學模擬；單機 Matter、日曆月增量費用已驗證，跨主機控制器、硬體設備、完整帳單與實際抄表週期尚未涵蓋。
