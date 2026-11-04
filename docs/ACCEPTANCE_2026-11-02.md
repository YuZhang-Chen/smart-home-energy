# P0–P2 驗收紀錄

**後續增量：P1 純規則與 P2 離線報告已完成；完整 Python 回歸更新為 118 passed、3 skipped，17 情境各 10 次通過。見 [離線照明驗收](OFFLINE_LIGHTING.md)。下列設備整合與聊天缺口判定維持有效；P0–P2 整體仍未完成。**

日期：2026-11-02（Asia/Taipei）。對照使用者確認的 P0–P2 計畫與當前工作目錄；本紀錄不取代既有歷史驗收。

## 結果

**P0 尚未通過；P1 與 P2 尚未完成，無法進行完整驗收。**

| 項目 | 判定 | 本次證據 |
| --- | --- | --- |
| Python 一般回歸 | 通過 | `python -m pytest tests -q -ra`：83 passed、3 skipped；真實模型連線與家庭 Agent 整合未啟用。 |
| 前端回歸 | 通過 | `npm.cmd test --prefix web`：22 passed，包含計畫卡版本更新與修改入口測試。 |
| 正式建置 | 通過 | `npm.cmd run build --prefix web`；TypeScript、Runtime 檢查及 Vite 通過，有大型 chunk 提示。輸出：`artifacts/acceptance-build.log`。 |
| 燈泡新指令取代 | 未通過 | `bulb_control.py` 在既有動作 active 時直接等待；尚未實作計畫要求的新目標取代及 `superseded` 狀態。 |
| SSE 訊框續接 | 未通過 | 隔離 SQLite 重現：一張計畫卡的 4 個工具訊框均使用 `id: 4`，只有 1 個不同游標；在訊框中間斷線無法依游標逐個續接。 |
| worker 完成與重啟恢復 | 未通過設計門檻 | 靜態檢查：`run_household` 先標記事件完成，worker 後續才分別保存回覆、卡片與工作終態；`recover_events` 只重新排入 running 事件。兩者之間中斷可能留下未完成的對話工作；本次未做故障注入實測。 |
| 新版瀏覽器整體驗收 | 未驗證 | 有計畫卡元件測試；Sidebar 在現有測試中為 mock。本次未實際驗證瀏覽器、375px、雙頁及啟停流程。 |
| MQTT／Matter 復原 | 通過 | 使用獨立 Broker `hems-phase2-acceptance-20261102`（127.0.0.1:18884）重跑 MQTT、HTTP／MQTT 與 Matter 復原測試，三個程序 exit code 均為 0，報告均為 `passed: true`。 |
| P1 時間規則與 presence | 未實作 | 未找到計畫中的照明規則、presence、環境時鐘 API 與控制流程；SQLite 仍為 v4。 |
| P2 固定情境與研究報告 | 未完成 | 尚無計畫要求的重複情境執行與 JSON／CSV／Markdown 指標報告。 |

本次以專案內隔離暫存目錄執行一般測試，Python 使用可建立 Windows 本機 loopback 的環境。Docker 啟動後另執行下列隔離虛擬設備測試；沒有呼叫真實模型或修改家庭資料庫。前端建置最大 JavaScript chunk 約 2,167 kB、gzip 629 kB；尚未量測實際載入時間。

## Docker 啟動後補驗（2026-11-02 13:53，Asia/Taipei）

Docker Engine 29.1.3；使用本機既有 `eclipse-mosquitto:2` 映像。三組測試依序執行，各自建立獨立 SQLite、MQTT 主題與設備程序；Matter 使用獨立 fabric。

| 流程 | 已通過範圍 | 本次獨立資料目錄 |
| --- | --- | --- |
| `tests/phase2_smoke.py --transport mqtt --port 18884 --recovery --broker-container hems-phase2-acceptance-20261102` | 燈泡開關、40% 亮度、暖／冷色溫、關燈調色溫與保留設定；雙設備執行、控制器／Broker 復原、行程完成、正式費率命令、人工中止、設備重啟故障及人工解除 | `artifacts/phase2-72dba20b/` |
| `tests/api_mqtt_smoke.py --port 18884 --recovery --broker-container hems-phase2-acceptance-20261102` | HTTP 提案、未核准不啟動、獨立核准、雙設備執行／完成、斷線資料過期、復線保持 run ID、避免重複啟動及人工中止 | `artifacts/3d-api-smoke-e17fadc7/` |
| `tests/phase2_smoke.py --transport matter --port 18884 --recovery --broker-container hems-phase2-acceptance-20261102` | 上述設備控制與復原；另含衝突重複命令拒絕、Matter controller 重啟、bridge 失聯不繞道 MQTT、bridge 恢復與燈泡再操作 | `artifacts/phase2-bf599e8c/` |

MQTT 與 Matter 的洗衣完成用電均為 0.225 kWh。Matter 日誌中的 `Hems prepare ... Failure(1)` 為衝突重複命令被拒絕的預期結果，對應 `conflicting_duplicate_rejected: true`。

本次原始日誌、三份 JSON 報告及程序結果保存於 `artifacts/acceptance-20261102-docker/`，總摘要為 `summary.json`；重跑前的舊報告另以 `before-` 前綴備份於同目錄。測試專用容器已刪除；查核原有 `smart-home-energy-mosquitto-1` 仍在 127.0.0.1:1883 運作。

本次補驗涵蓋既有傳輸與設備流程，未涵蓋尚未實作的新指令取代、SSE 逐訊框續接、對話完成交易恢復或 P1／P2，因此 P0 整體判定維持尚未通過。

## 後續順序

使用者最新指示暫不實作 SSE 游標與對話重啟恢復，兩項已列入 [待處理工作](PENDING_WORK.md)，不改變本文件的未通過判定。P1 可先開發獨立規則、環境與 REST 設定；設備整合前仍須完成燈泡取代與手動優先。P2 可先建立情境與量測框架，聊天斷線／重啟缺陷必須持續列示，完整驗收結論延後。
