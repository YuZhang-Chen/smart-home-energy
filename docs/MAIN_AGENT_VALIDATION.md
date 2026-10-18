# Main Agent 首版驗收紀錄

日期：2026-10-17（Asia/Taipei）。環境：Windows、Python 3.12.13、OpenAI Python SDK 2.54.0、Docker Mosquitto 2、Streamlit。

以下是首版驗收當時的歷史結果。第二階段已增加 Sub Agents、冷氣控制、正式費率與真實 Matter，並實際以 vLLM 完成雙設備提案；使用者另回報連線測試成功。最新測試數、證據及未涵蓋項目以 [PHASE2_VALIDATION.md](PHASE2_VALIDATION.md) 為準。

## 結果

首版程式已完成。模型優先順序調整後重新執行 unittest／AppTest，共 48 項：**46 項通過、2 項真實模型連線測試略過**（約 9.35 秒），`compileall` 通過。首版驗收另已通過 `pip check`、真實 Mosquitto 的洗衣與冷氣復原測試，PowerShell 啟停腳本已實際啟動及清理四個程序。

| 驗證層 | 已驗證範圍 | 結果 |
| --- | --- | --- |
| 純領域 | 60 分鐘／0.225 kWh、大步長跨階段、跨電價邊界、跨夜、安靜時段、無解、UTC 編解碼、60 倍物理結果 | 通過 |
| SQLite | 一個未完成任務、失敗仍佔名額、版本衝突、核准恢復、範圍內改排／超範圍重新核准、UI 去重、事件合併 | 通過 |
| 控制與設備 | 未核准拒絕啟動、過期／錯誤時鐘／boot／費用上限、重複命令、5 秒逾時後查詢、識別碼核對、人工中止、重啟故障 | 通過 |
| 假模型 Main Agent | 多輪工具與結果回饋、提案不會自行核准、工具白名單、vLLM 故障或逾時後沿用 Azure 並保留上下文、格式修復、15 次與 120／20 秒預算、補問、雙服務失敗後表單可用 | 通過 |
| 官方 SDK + 隔離 HTTP | vLLM 正常時不呼叫 Azure；連線失敗、空白回覆、429 或 503 才切至 Azure；Azure `/openai/v1/chat/completions`、部署名稱、vLLM served name、`max_retries=0`、不記錄 Key | 通過；使用 httpx MockTransport，沒有外部請求 |
| Streamlit AppTest | 保留冷氣監測；表單→待核准→核准→取消、重繪不重複提交、不建立執行器、不自動呼叫模型 | 通過 |
| 實際瀏覽器 | 本機頁面、洗衣狀態、表單提案、完整核准範圍、核准、取消與歷史保存 | 通過；驗收任務已取消 |
| 真實 Azure／vLLM | 首版驗收當時的外部端點與實際回覆品質 | 當時未測試；後續使用者回報及本次 vLLM 實測另見第二階段紀錄 |

主測試指令：

```powershell
$env:RUN_LLM_CONNECT_TESTS = '0'
$env:LLM_CONNECT_PROMPT = 'Reply with OK only.'
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
```

本次受限沙箱無法建立 Windows asyncio 本機 socket，且預設暫存資料夾不允許 SQLite 寫入；測試以專案內 `artifacts/tmp` 作為 TEMP／TMP，並在允許本機 socket 的程序中執行。這是驗收環境限制，並非模型服務測試結果。

## 真實 MQTT 與獨立背景程序

執行：

```powershell
.\.venv\Scripts\python.exe tests/washer_smoke.py --recovery
.\.venv\Scripts\python.exe tests/mqtt_smoke.py --recovery
```

洗衣驗收使用獨立測試 topic、控制資料庫與設備資料庫，啟動真正 washer/control 子程序並停用模型。確認：

- 標準一小時行程在 60 倍模式下約 **60.047 個真實秒**完成，累積 **0.225 kWh**；此案例全程尖峰模擬費用 **NT$1.35**。
- 重送相同 command_id 回報原結果，沒有新行程；完整首個任務只記錄一次 start。
- 控制服務中途終止並重啟，依相同行程恢復，不重送舊啟動命令。
- Broker 中途停止 4 秒並重啟，設備持續自主運轉，復線後用電增加且 run_id 相同。
- 下一次行程中重新啟動設備程序，回報 fault，任務保持 failed；人工 reset 取得設備確認後釋放名額。
- 第三次行程經人工確認中止，設備回報 aborted，任務沒有偽裝成 completed。

洗衣 JSON 報告在 `artifacts/washer-smoke.json`，各次資料庫及服務日誌保留於該報告所列目錄。腳本以 `finally` 關閉測試程序，復原模式最後確保 Broker 啟動。

冷氣驗收另確認 Broker 延後啟動、30.02 秒內接收 15 筆樣本、間隔約 **2.0017–2.0145 秒**、錯誤 JSON 後繼續接收、Broker 重啟及 publisher 新 run 清除舊歷史。結果在 `artifacts/mqtt-smoke.json`。

## 瀏覽器與啟停腳本

實際瀏覽器使用獨立 `artifacts/browser-validation/` 資料庫與 topic。已提交預設表單，顯示 22:00–23:00 計畫及 NT$0.5625 模擬費用；核准後顯示已排程及完整核准範圍；最後取消並確認歷史狀態為 cancelled。截圖在 `artifacts/browser-validation/approved-plan.png`（核准時畫面）。全部驗收程序已停止，沒有留下待執行測試任務；原本已啟動的 Broker 保持啟動。

`scripts/start.ps1 -NoBroker`／`scripts/stop.ps1` 已以隔離資料庫實測。Windows 啟動時間的身分檢查使用 UTC ticks 字串，避免 PowerShell JSON 自動日期轉換造成時區誤判。停止時核對 PID、程式路徑與開始時間，不會依程序名稱停止其他工作。

## 已知邊界

- 首版當時沒有真實模型證據；後續實測已另記，不能把假模型測試視為實際 Azure 備援驗收。
- 首版當時只包含虛擬洗衣與模擬電價；第二階段已擴充，完整帳單與硬體家電仍未涵蓋。
- 開始命令有傳輸延遲，實際起訖會再次驗證；貼齊期限或安靜時段邊界的計畫可能需要改排，不能放寬核准限制。
- SQLite 與設備本機狀態保留最後已確認資料，設備程序停機期間不補算完成。UI 關閉不影響背景服務；全部服務停止則需要依恢復規則處理。
