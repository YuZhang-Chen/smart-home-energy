# P1 純規則與 P2 離線報告

2026-11-02：本輪純規則與離線報告完成。這不是 Controller／設備整合完成，也不是 P0–P2 整體驗收通過。

## 純規則

`smart_home_energy.lighting_rules.evaluate(rule, environment, state)` 回傳不可變 `Evaluation`，包含新狀態、原因、時間窗、下一邊界、選擇性的目標與決策識別碼。呼叫端提供所有時間，模組不讀取環境變數、系統時鐘、資料庫或網路。

`LightingRule` 預設停用，台北時間 06:00–18:00 關燈，晚間 40%／2700K；可修改兩個邊界、亮度、色溫與 presence 開關。關燈目標只含 power=false，開燈使用既有 Matter 單位轉換。相同規則版本、時間窗與目標有穩定決策 ID；這不表示命令已派送或已確認。

`EnvironmentClock` 支援 1／60 倍並有獨立 generation；`switch_clock` 要求規則停用且 generation 更新，回傳已清空倒數的狀態。`Presence.observe` 建立 15 真實分鐘有效的模擬觀測；未知、未來觀測及到期資料皆不能觸發 presence 條件操作。有效無人持續 5 環境分鐘後關燈；連續更新不重置倒數，過期缺口會重置。

`manual_override` 暫停至下一時段邊界，`resume` 清除覆寫並於下次 evaluate 立即重判。`LightingState.to_dict/from_dict` 支援未來持久化；目前沒有 SQLite schema 變更或實際保存服務。

## 執行報告

於已安裝專案的虛擬環境執行：

```powershell
.\.venv\Scripts\python.exe -m smart_home_energy.research --repeats 10 --output artifacts/research
```

每次建立 UUID 子目錄，內含 `results.json`、`summary.csv`、`report.md`。17 個固定情境包含時段邊界、停用、覆寫／恢復、presence、5 分鐘無人、資料過期、加速時鐘、自訂時間窗、時鐘世代、狀態還原、決策穩定性與預期拒絕。

JSON 包含逐次結果與版本／工作區差異（含未追蹤檔案內容）雜湊、Python／相依版本、時鐘模式及情境版本。無法讀取版本時記 unavailable。每次耗時是整個離線規則情境的耗時，百分位使用 nearest-rank，不能解讀為設備、模型或 UI 延遲。

模型與傳輸標為 not_used；未量測指標為 null。成功率只計 passed／failed，已知缺陷、未實作及未執行另外列示；空樣本統計為 null。實際情境失敗時 CLI 回傳 1，參數不合法回傳 2。純規則 scope_status 可為 passed，但 overall_acceptance 固定 incomplete。

## 本次驗收

- Python 完整回歸：118 passed、3 skipped；日誌 `artifacts/p1-p2-pytest.log`。
- 前端正式建置通過；日誌 `artifacts/p1-build.log`，大型 chunk 提示仍存在。
- 17 情境 × 10 次，共 170 次全部通過。報告：`artifacts/research/offline-3298e461006d4a3cb0717a47879f78cb/`。
- CLI 測試封鎖 socket 建立並核對三種報告樣本一致；規則測試另涵蓋無時區／非法數值、presence 缺口、剛滿 5 分鐘與 15 真實分鐘期限。

CHAT-01（SSE）、CHAT-02（對話重啟）仍為已知缺陷；設備仲裁未實作，本輪 MQTT／Matter 未執行。後續先完成燈泡仲裁，再接 Controller、REST 與設定頁。完整待辦見 [PENDING_WORK.md](PENDING_WORK.md)。
