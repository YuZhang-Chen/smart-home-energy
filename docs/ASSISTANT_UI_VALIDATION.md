# 家庭助理介面驗收

日期：2026-10-17。此紀錄接續 [第二階段驗收](PHASE2_VALIDATION.md)，操作見 [ASSISTANT_UI.md](ASSISTANT_UI.md)。

## 自動測試

完整 unittest／Streamlit AppTest：75 項、`OK (skipped=2)`，約19.75秒。一般測試不呼叫付費模型；真實家庭模型與連線測試兩個明確啟用項目略過。輸出：`artifacts/ui-redesign-unittest.log`。

新增／更新的驗證包含：

- 三個設備入口名稱、快捷按鈕只保存一次需求、等待背景Agent時停用重複提交、畫面不直接呼叫模型或設備。
- 未指定時間的雙子Agent工具流程、冷氣27°C／一小時與洗衣24小時／安靜時段預設、兩張卡個別核准。
- 冷氣提案經過10分鐘才確認仍以確認時間重算起訖；明確預約不改時間；模擬設備仍在核准結束時間自主關機。
- 冷氣修改會清除舊提案並暫停舊排程，對話可由持久化請求／事件還原。
- 關閉冷氣第一次點擊不送出停止，第二次確認才送出命令，之後依設備回報判定。
- 常用偏好驗證與保存；正式費率未填基準時不默認零；切換月份並新增基準不覆寫前一月份。
- 完成／中止任務後再提出需求，不把遙測中的上次任務當成未完成任務；錯誤引用已結束任務時，模型收到可修正的工具錯誤，舊任務與核准不會被改寫。
- 原有洗衣、費率、遷移、Agent預算／備援、MQTT與控制回歸均保留。

```powershell
$env:RUN_AGENT_INTEGRATION_TESTS='0'
$env:RUN_LLM_CONNECT_TESTS='0'
$env:TEMP=Join-Path (Get-Location) 'artifacts/tmp'
$env:TMP=$env:TEMP
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q src tests
.\.venv\Scripts\python.exe -m pip check
```

## 真實瀏覽器、模型與 Matter

獨立使用 `artifacts/ui-redesign/` 的控制／設備資料庫、MQTT topic與Matter storage，UI埠8505、Matter埠5558。原本任務資料庫不變。

1. 實際瀏覽器輸入「幫我開冷氣，順便幫我安排洗衣。」未另外指定日期、期限或溫度。
2. 真實 **vLLM** 完成 Main 委派及兩個子Agent多輪工具，10次請求、57.375秒，兩份提案皆為待核准，沒有補問時間。Azure故障切換本次未再次對外測試，沿用既有測試證據。
3. 同一個畫面顯示冷氣「確認後開啟、60分鐘、27°C」與洗衣「次日07:00–08:00、避開23:00–07:00」。後者在24小時期限內，預估費用NT$0.5625。
4. 在冷氣卡按確認後，經真正的 Matter controller → bridge → MQTT虛擬設備開機，冷氣卡顯示運轉中、室溫下降與用電增加；洗衣仍為待核准，沒有自行開始。
5. 按「關閉冷氣」後確認設備仍在運轉；再按「確認停止」，Matter回報aborted、功率為零。最後取消未核准洗衣，確認無未完成的測試任務。

連續操作時另發現模型誤用上次已結束任務，已修正 Main 讀取上下文與可修正的委派錯誤；回歸測試保證不改寫舊任務。修正後再從快捷按鈕提出冷氣需求，真實vLLM以6次請求、25.172秒完成新提案。最後保留這一份未核准的冷氣計畫供使用者試用，沒有再次開機。

機器可讀摘要：`artifacts/ui-redesign/validation.json`。devices 是步驟4的快照；matter_stop_confirmed、washer_cancelled、active_tasks_after_initial_cleanup 記錄首輪清理，pending_review_tasks=1 記錄交付時的待確認計畫。日誌位於同目錄的runtime及matter-controller.log。

截圖：`proposed.png`（對話與提案）、`running.png`（冷氣運轉、洗衣待確認）、`ready.png`（交付時可確認的冷氣卡）。已以桌面側欄約847px寬度目視核對，未在本次工具環境另外調整瀏覽器至手機尺寸，未宣稱手機實測完成。

UI與服務保留於 [本機8505](http://127.0.0.1:8505) 供操作，配對與紀錄持久化；停止指令見操作文件。測試使用真實模型及Matter，設備本身仍為虛擬家電。

## 邊界

本次改版沒有新增離線替代策略，也沒有變更原有「每設備個別核准」與運轉中設定不可任意修改的限制。費率缺基準、設備失聯、版本已過期等仍需解決後才能執行；不會為簡化介面而繞過驗證。
